"""Replay engine scans at journal strategy_decision timestamps."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from algoflat.bus.event_bus import EventBus
from algoflat.config import AppConfig, get_config
from algoflat.contract_selector.selector import ContractUniverse
from algoflat.contract_selector.strike_picker import candidate_instruments
from algoflat.features.chain_intel import build_chain_snapshot
from algoflat.features.engine import FeatureEngine
from algoflat.journal.analytics import StrategyLearner
from algoflat.market_data.engine import MarketDataEngine
from algoflat.market_data.vwap import session_vwap
from algoflat.models.events import Candle, CandleInterval, Instrument, OptionState
from algoflat.option_data.greeks import compute_greeks
from algoflat.quality.gate import QualityGate
from algoflat.regime.classifier import RegimeClassifier
from algoflat.replay.loader import DayJournal
from algoflat.replay.market_state import bar_for_ts, bar_fraction, candles_as_of, spot_from_bar
from algoflat.replay.scheduler import ReplaySchedule
from algoflat.scanner.library import build_strategy_scanners
from algoflat.strategy.router import StrategyRouter
from algoflat.trading.entry_confirmation import (
    EntryConfirmationTracker,
    in_open_gap_entry_window,
)
from algoflat.validator.engine import RuleValidator
from algoflat.validator.premium_behaviour import PremiumBehaviourTracker

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class SessionReplayResult:
    day: date
    underlying: str
    schedule_source: str
    scans_run: int
    signals_seen: int
    validator_passed: int
    decisions: list[dict[str, Any]] = field(default_factory=list)
    skips: dict[str, int] = field(default_factory=dict)


def _load_snap(day: date, underlying: str) -> dict[str, Any] | None:
    from day_backtest import _repair_empty_universe, load_day_snap  # noqa: E402

    snap = load_day_snap(day, underlying)
    if not snap:
        return None
    snap["universe"] = _repair_empty_universe(snap["universe"], day, underlying)
    return snap


def run_session_replay(
    journal: DayJournal,
    schedule: ReplaySchedule,
    *,
    config: AppConfig | None = None,
    live_parity: bool = True,
    option_m1_from_snap: bool = False,
) -> SessionReplayResult | None:
    """Run router+validator at each journal scan timestamp using day snap data."""
    cfg = config or get_config()
    ul = journal.underlying
    day = journal.day

    from algoflat.symbols_util import apply_active_underlying  # noqa: E402

    apply_active_underlying(cfg, ul)

    snap = _load_snap(day, ul)
    if snap is None:
        return None

    all_candles = snap["all_candles"]
    m1_all = all_candles.get(CandleInterval.M1) or []
    m3_all = all_candles.get(CandleInterval.M3) or []
    m5_all = all_candles.get(CandleInterval.M5) or []
    if len(m1_all) < 30:
        return None

    universe: ContractUniverse = snap["universe"]
    option_series: dict[str, list[Candle]] = snap["option_series"]
    prior_m5: list[Candle] = snap.get("prior_m5") or []

    expiry_d = day
    if universe.atm_ce and universe.atm_ce.expiry_date:
        expiry_d = universe.atm_ce.expiry_date.date()

    from day_backtest import (  # noqa: E402
        LOT_FALLBACK,
        _fake_inst,
        _find_inst,
        _premium_path,
        _round_strike,
    )
    from algoflat.broker.factory import create_live_broker  # noqa: E402

    bus = EventBus(max_size=10)
    broker = create_live_broker(cfg)
    md = MarketDataEngine(cfg, broker, bus)
    features_eng = FeatureEngine(cfg, md)
    if prior_m5:
        features_eng.set_prior_day(
            max(c.high for c in prior_m5),
            min(c.low for c in prior_m5),
            prior_m5[-1].close,
        )

    regime_clf = RegimeClassifier(cfg)
    quality = QualityGate(cfg)
    learner = StrategyLearner.from_config(
        ROOT / "reports" / "strategy_learner.json", cfg.strategy
    )
    learner.begin_session(day)
    quality.set_learner(learner)
    scanners = build_strategy_scanners(cfg)
    router = StrategyRouter(cfg, scanners, quality)
    validator = RuleValidator(cfg)
    validator.reset_session()
    entry_confirm = EntryConfirmationTracker.from_config(
        cfg.strategy.get("entry_confirmation") or {}
    )
    pb_cfg = cfg.validator.get("premium_responsive") or {}
    premium_tracker = PremiumBehaviourTracker(max_samples=int(pb_cfg.get("max_samples", 12)))

    step = Decimal(str(cfg.symbols["strike_step"]))
    strike_cfg = cfg.strategy.get("strike_selection") or {}
    atm_band = int(strike_cfg.get("atm_band_steps", 1))
    strike_modes = list(strike_cfg.get("modes") or ["atm"])
    otm_steps = [int(s) for s in (strike_cfg.get("otm_steps") or [])]
    lot_size = LOT_FALLBACK
    if universe.atm_pe:
        lot_size = universe.atm_pe.lot_size
    elif universe.atm_ce:
        lot_size = universe.atm_ce.lot_size

    entry_start = time.fromisoformat(str(cfg.validator.get("entry_start_time", "10:00")))
    entry_end = time.fromisoformat(str(cfg.validator.get("entry_end_time", "15:15")))
    opt_ex = str(cfg.symbols.get("exchange_options", "NFO"))

    honest = not live_parity
    prior_oi: dict[str, int] = {}
    decisions: list[dict[str, Any]] = []
    skips: dict[str, int] = {}
    signals_seen = 0
    validator_passed = 0
    scans_run = 0

    def _validator_option_m1(token: str | None) -> list[Candle] | None:
        if live_parity and not option_m1_from_snap:
            return None
        if not token:
            return None
        return option_series.get(token)

    def _band_opt_states(
        uni: ContractUniverse,
        *,
        when: datetime,
        spot: Decimal,
        bar_frac: Decimal,
        bar: Candle,
    ) -> dict[str, OptionState | None]:
        states: dict[str, OptionState | None] = {}
        seen: set[str] = set()
        for side in ("CE", "PE"):
            for inst in candidate_instruments(
                uni,
                side,
                band_steps=atm_band,
                otm_steps=otm_steps,
                modes=strike_modes,
                step=step,
            ):
                if inst.token in seen:
                    continue
                seen.add(inst.token)
                prem, _ = _premium_path(
                    when=when,
                    spot=spot,
                    side=inst.option_type,
                    strike=inst.strike,
                    expiry=expiry_d,
                    option_series=option_series,
                    token=inst.token,
                    bar_frac=bar_frac,
                    bar=bar,
                    honest=honest,
                )
                vol = oi = None
                if inst.token in option_series:
                    for c in option_series[inst.token]:
                        if c.ts <= when:
                            vol = c.volume
                            oi = c.oi
                        else:
                            break
                states[inst.token] = OptionState(
                    instrument_token=inst.token,
                    tsym=inst.tsym,
                    ltp=prem,
                    bid=prem - Decimal("0.5"),
                    ask=prem + Decimal("0.5"),
                    spread_pct=Decimal("1"),
                    volume=vol,
                    oi=oi,
                )
        return states

    for event in schedule.events:
        ts_ist = event.ts if event.ts.tzinfo else event.ts.replace(tzinfo=IST)
        ts = ts_ist.astimezone(timezone.utc)
        t_clock = ts_ist.time()
        bar = bar_for_ts(m1_all, ts)
        if bar is None:
            skips["no_bar"] = skips.get("no_bar", 0) + 1
            continue
        scans_run += 1

        spot = spot_from_bar(bar, ts, use_intra_bar=live_parity)
        bar_frac = bar_fraction(bar, ts)
        m1, m3, m5 = candles_as_of(
            m1_all, m3_all, m5_all, ts, spot=spot, bar=bar, honest=honest
        )
        md._candles[CandleInterval.M1] = m1  # type: ignore[attr-defined]
        md._candles[CandleInterval.M3] = m3  # type: ignore[attr-defined]
        md._candles[CandleInterval.M5] = m5  # type: ignore[attr-defined]
        md._spot_ltp = spot  # type: ignore[attr-defined]

        atm = _round_strike(spot, step)
        ce = _find_inst(universe, "CE", atm) or _fake_inst(
            "CE", atm, lot_size, underlying=ul, exchange=opt_ex
        )
        pe = _find_inst(universe, "PE", atm) or _fake_inst(
            "PE", atm, lot_size, underlying=ul, exchange=opt_ex
        )
        uni = ContractUniverse(
            spot=spot,
            atm_strike=atm,
            expiry_symbol=universe.expiry_symbol,
            instruments=universe.instruments,
            atm_ce=ce,
            atm_pe=pe,
        )
        band_states = _band_opt_states(
            uni, when=ts, spot=spot, bar_frac=bar_frac, bar=bar
        )
        chain = build_chain_snapshot(uni, band_states, prior_oi=prior_oi)
        features_eng.set_chain_snapshot(chain)
        for tok, st in band_states.items():
            if st is not None and st.oi is not None:
                prior_oi[tok] = int(st.oi)

        features_eng.is_expiry_day = expiry_d == day
        features_eng.expiry_date = expiry_d
        features_eng.session_date = day

        vwap_hint = session_vwap(m1)
        prefer_pe = vwap_hint is not None and spot < vwap_hint
        ctx_inst = pe if prefer_pe else ce
        ctx_st = band_states.get(ctx_inst.token) if ctx_inst else None
        opt_ctx: dict[str, Any] = {}
        if ctx_st and ctx_st.ltp is not None:
            opt_ctx["ltp"] = float(ctx_st.ltp)
            opt_ctx["spread_pct"] = float(ctx_st.spread_pct or 1)
            opt_ctx["volume"] = ctx_st.volume
            opt_ctx["oi"] = ctx_st.oi
            g = compute_greeks(
                spot=float(spot),
                strike=float(ctx_inst.strike),
                premium=float(ctx_st.ltp),
                option_type=ctx_inst.option_type,
                expiry=expiry_d,
                now=ts,
            )
            opt_ctx["delta"] = g.delta
            opt_ctx["gamma"] = g.gamma
            opt_ctx["iv"] = g.iv
        features_eng.set_option_context(opt_ctx)

        for _tok, _st in band_states.items():
            if _st is not None and _st.ltp is not None:
                premium_tracker.record(_tok, float(spot), float(_st.ltp), ts)

        feat = features_eng.compute()
        feat = feat.model_copy(update={"ts": ts, "nifty_spot": spot})
        dte = max(0, (expiry_d - day).days)
        regime = regime_clf.classify(
            feat,
            m1,
            m5,
            is_expiry_day=(expiry_d == day),
            days_to_expiry=dte,
            now=ts_ist,
        )

        if t_clock < entry_start or t_clock > entry_end:
            continue
        if len(m1) < 20 or len(m3) < 5:
            continue

        options = {s.name: band_states for s in scanners}
        decision, signal = router.route(
            feat,
            regime,
            uni,
            options,
            is_expiry_day=(expiry_d == day),
            learner=learner,
            session_date=day,
            expiry_date=expiry_d,
        )
        if signal is None:
            entry_confirm.consider(None)
            key = (decision.selected_reason or "no_signal")[:60]
            skips[key] = skips.get(key, 0) + 1
            continue

        confirmed, wait_reason = entry_confirm.consider(
            signal,
            now=ts,
            in_open_gap_window=in_open_gap_entry_window(
                ts,
                entry_start=str(cfg.validator.get("entry_start_time", "09:30")),
                open_window_minutes=int(
                    (cfg.strategy.get("entry_confirmation") or {}).get(
                        "open_window_minutes", 15
                    )
                ),
            ),
        )
        if confirmed is None:
            if wait_reason:
                skips[wait_reason[:60]] = skips.get(wait_reason[:60], 0) + 1
            continue
        signal = confirmed
        signals_seen += 1

        side = signal.side
        pick = signal.scanner_metadata.get("strike_pick") or {}
        strike = Decimal(str(pick.get("strike", atm)))
        inst: Instrument | None = next(
            (i for i in uni.instruments if i.token == signal.instrument_token),
            None,
        )
        if inst is None:
            inst = _find_inst(uni, side, strike) or _fake_inst(
                side, strike, lot_size, token=signal.instrument_token, tsym=signal.tsym
            )
        opt_st = band_states.get(inst.token) if inst else None
        if opt_st is None and inst is not None:
            prem0, _ = _premium_path(
                when=ts,
                spot=spot,
                side=side,
                strike=strike,
                expiry=expiry_d,
                option_series=option_series,
                token=inst.token,
                bar_frac=bar_frac,
                bar=bar,
                honest=honest,
            )
            opt_st = OptionState(
                instrument_token=inst.token,
                tsym=inst.tsym,
                ltp=prem0,
                bid=prem0 - Decimal("0.5"),
                ask=prem0 + Decimal("0.5"),
                spread_pct=Decimal("1"),
            )

        v = validator.validate(
            signal,
            opt_st,
            has_open_for_token=False,
            in_cooldown=False,
            kill_switch=False,
            is_expiry_day=(expiry_d == day),
            now=ts,
            premium_tracker=premium_tracker,
            regime_primary=regime.primary,
            option_m1=_validator_option_m1(inst.token if inst else None),
            spot_m1=m1,
        )
        would_enter = v.passed
        if would_enter:
            validator_passed += 1

        decisions.append(
            {
                "ts_ist": ts_ist.strftime("%Y-%m-%d %H:%M:%S"),
                "ts_epoch": ts.timestamp(),
                "setup_type": signal.setup_type,
                "side": side,
                "tsym": signal.tsym,
                "confidence": int(signal.confidence or 0),
                "validator_passed": v.passed,
                "rejection_reasons": list(v.rejection_reasons),
                "entered": would_enter,
                "router_reason": decision.selected_reason,
            }
        )

    return SessionReplayResult(
        day=day,
        underlying=ul,
        schedule_source=schedule.source,
        scans_run=scans_run,
        signals_seen=signals_seen,
        validator_passed=validator_passed,
        decisions=decisions,
        skips=skips,
    )
