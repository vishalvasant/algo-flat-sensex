"""Full journal replay — entries at scan times, exits between scans."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
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
from algoflat.models.events import (
    Candle,
    CandleInterval,
    CandidateSignal,
    FeatureSnapshot,
    Instrument,
    OptionState,
)
from algoflat.option_data.greeks import compute_greeks
from algoflat.position.exit_rules import should_queue_reversal_flip
from algoflat.quality.gate import QualityGate
from algoflat.regime.classifier import RegimeClassifier
from algoflat.replay.loader import DayJournal
from algoflat.replay.market_state import bar_for_ts, bar_fraction, candles_as_of, spot_from_bar
from algoflat.replay.scheduler import ReplaySchedule
from algoflat.replay.session import SessionReplayResult, _load_snap
from algoflat.replay.timeline import ExitTick, ScanTick, build_timeline
from algoflat.risk.greeks_sizing import greeks_confirmation_multiplier
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
class ReplayOpenTrade:
    strategy: str
    side: str
    strike: Decimal
    entry_ts: datetime
    entry_spot: Decimal
    entry_premium: Decimal
    lot_size: int
    lots: int
    confidence: int = 0
    mfe: Decimal = Decimal("0")
    tsym: str = ""
    token: str = ""
    entry_meta: dict = field(default_factory=dict)


@dataclass
class ReplayClosedTrade:
    strategy: str
    side: str
    strike: Decimal
    entry_ts: datetime
    exit_ts: datetime
    entry_premium: Decimal
    exit_premium: Decimal
    lots: int
    lot_size: int
    pnl: Decimal
    exit_reason: str
    tsym: str = ""
    confidence: int = 0


@dataclass
class FullSessionReplayResult(SessionReplayResult):
    entries: int = 0
    closed: list[ReplayClosedTrade] = field(default_factory=list)
    realized_pnl: Decimal = Decimal("0")


def run_full_session_replay(
    journal: DayJournal,
    schedule: ReplaySchedule,
    *,
    config: AppConfig | None = None,
    live_parity: bool = True,
    option_m1_from_snap: bool = False,
) -> FullSessionReplayResult | None:
    """Replay entries at journal scans and evaluate exits between scans."""
    cfg = config or get_config()
    ul = journal.underlying
    day = journal.day

    from algoflat.symbols_util import apply_active_underlying, capital_for  # noqa: E402
    from day_backtest import (  # noqa: E402
        LOT_FALLBACK,
        _exit_decision,
        _fake_inst,
        _find_inst,
        _fit_lots,
        _premium_path,
        _round_strike,
    )

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

    bus = EventBus(max_size=10)
    from algoflat.broker.factory import create_live_broker  # noqa: E402

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

    exit_cfg = cfg.position_exit
    cooldown_min = int(cfg.risk.get("cooldown_after_exit_minutes", 3))
    entry_start = time.fromisoformat(str(cfg.validator.get("entry_start_time", "10:00")))
    entry_end = time.fromisoformat(str(cfg.validator.get("entry_end_time", "15:15")))
    force_exit_t = time.fromisoformat(str(cfg.risk.get("force_exit_time", "15:15")))
    max_concurrent = int(cfg.risk.get("max_concurrent_positions_per_index", 0))
    max_trades_day = int(cfg.risk.get("max_trades_per_day", 0))
    scan_sec = int(cfg.runtime.get("scan_interval_seconds", 30))
    exit_sec = int(
        cfg.runtime.get(
            "backtest_exit_interval_seconds",
            cfg.runtime.get("rest_quote_poll_interval_market_seconds", 2),
        )
    )
    exit_sec = max(1, min(exit_sec, scan_sec))
    opt_ex = str(cfg.symbols.get("exchange_options", "NFO"))
    honest = not live_parity

    starting = capital_for(cfg, ul)
    available = starting
    deployed = Decimal("0")
    realized = Decimal("0")

    prior_oi: dict[str, int] = {}
    decisions: list[dict[str, Any]] = []
    skips: dict[str, int] = {}
    signals_seen = 0
    validator_passed = 0
    scans_run = 0
    entries = 0
    open_trades: list[ReplayOpenTrade] = []
    closed_trades: list[ReplayClosedTrade] = []
    cooldown_until: dict[str, datetime] = {}
    global_cooldown_until: datetime | None = None
    pending_flips: list[dict[str, str]] = []

    timeline = build_timeline(
        schedule,
        m1_all,
        day=day,
        exit_sec=exit_sec,
        honest=honest,
        session_end=force_exit_t,
    )

    def _validator_option_m1(token: str | None) -> list[Candle] | None:
        if live_parity and not option_m1_from_snap:
            return None
        if not token:
            return None
        return option_series.get(token)

    def _mark_premium(
        *,
        when: datetime,
        spot: Decimal,
        side: str,
        strike: Decimal,
        token: str | None,
        bar_frac: Decimal,
        bar: Candle,
    ) -> tuple[Decimal, str]:
        return _premium_path(
            when=when,
            spot=spot,
            side=side,
            strike=strike,
            expiry=expiry_d,
            option_series=option_series,
            token=token,
            bar_frac=bar_frac,
            bar=bar,
            honest=honest,
        )

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
                prem, _ = _mark_premium(
                    when=when,
                    spot=spot,
                    side=inst.option_type,
                    strike=inst.strike,
                    token=inst.token,
                    bar_frac=bar_frac,
                    bar=bar,
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

    def _apply_market(
        ts: datetime,
        spot: Decimal,
        bar: Candle,
        bar_frac: Decimal,
    ) -> tuple[Any, Any, list[Candle], list[Candle], list[Candle], ContractUniverse, dict]:
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
        ts_ist = ts.astimezone(IST)
        regime = regime_clf.classify(
            feat,
            m1,
            m5,
            is_expiry_day=(expiry_d == day),
            days_to_expiry=dte,
            now=ts_ist,
        )
        return feat, regime, m1, m3, m5, uni, band_states

    def _close_trade(
        ot: ReplayOpenTrade,
        *,
        ts: datetime,
        prem: Decimal,
        reason: str,
        regime_primary: str | None,
    ) -> None:
        nonlocal available, deployed, realized, global_cooldown_until, open_trades
        pnl = (prem - ot.entry_premium) * ot.lot_size * ot.lots
        premium_freed = ot.entry_premium * ot.lot_size * ot.lots
        deployed = max(Decimal("0"), deployed - premium_freed)
        available = available + premium_freed + pnl
        realized += pnl
        closed_trades.append(
            ReplayClosedTrade(
                strategy=ot.strategy,
                side=ot.side,
                strike=ot.strike,
                entry_ts=ot.entry_ts,
                exit_ts=ts,
                entry_premium=ot.entry_premium,
                exit_premium=prem,
                lots=ot.lots,
                lot_size=ot.lot_size,
                pnl=pnl.quantize(Decimal("0.01")),
                exit_reason=reason,
                tsym=ot.tsym,
                confidence=ot.confidence,
            )
        )
        if ot.token:
            cooldown_until[ot.token] = ts + timedelta(minutes=cooldown_min)
        global_cooldown_until = ts + timedelta(minutes=cooldown_min)
        learner.record_trade(ot.strategy, pnl, confidence=ot.confidence, exit_reason=reason)
        if pnl < 0:
            validator.record_session_loss(
                ot.strategy,
                is_expiry_day=(expiry_d == day),
                exit_reason=reason,
            )
        if should_queue_reversal_flip(
            exit_reason=reason,
            option_side=ot.side,
            market_data=md,
            exit_cfg=exit_cfg,
        ):
            pending_flips.append(
                {"side": "PE" if ot.side == "CE" else "CE", "from_side": ot.side}
            )

    def _evaluate_exits(
        ts: datetime,
        spot: Decimal,
        bar: Candle,
        bar_frac: Decimal,
        *,
        force: bool,
    ) -> None:
        nonlocal open_trades
        if not open_trades:
            return
        feat, regime, _m1, _m3, _m5, _uni, _band = _apply_market(ts, spot, bar, bar_frac)
        vwap = feat.session_vwap
        still_open: list[ReplayOpenTrade] = []
        for ot in open_trades:
            prem, _src = _mark_premium(
                when=ts,
                spot=spot,
                side=ot.side,
                strike=ot.strike,
                token=ot.token or None,
                bar_frac=bar_frac,
                bar=bar,
            )
            ot.mfe = max(ot.mfe, prem - ot.entry_premium)
            should, reason = _exit_decision(
                side=ot.side,
                entry_premium=ot.entry_premium,
                entry_ts=ot.entry_ts,
                now=ts,
                current_premium=prem,
                mfe=ot.mfe,
                spot=spot,
                vwap=vwap,
                force_exit=force,
                cfg=exit_cfg,
                market_data=md,
                regime_primary=regime.primary,
                confidence=ot.confidence,
                setup_type=ot.strategy,
                entry_meta=ot.entry_meta,
            )
            if force and not reason:
                should, reason = True, "session_end"
            if should and reason:
                _close_trade(ot, ts=ts, prem=prem, reason=reason, regime_primary=regime.primary)
            else:
                still_open.append(ot)
        open_trades = still_open

    def _try_enter(
        *,
        signal,
        inst: Instrument,
        opt_st: OptionState | None,
        when: datetime,
        spot: Decimal,
        bar_frac: Decimal,
        bar: Candle,
        ts_ist: datetime,
        strike_pick: dict | None,
    ) -> bool:
        nonlocal available, deployed, entries, open_trades
        if max_trades_day > 0 and (len(closed_trades) + len(open_trades)) >= max_trades_day:
            skips["max_trades_per_day"] = skips.get("max_trades_per_day", 0) + 1
            return False
        if global_cooldown_until is not None and when < global_cooldown_until:
            skips["global_cooldown"] = skips.get("global_cooldown", 0) + 1
            return False
        token = inst.token
        if token and token in cooldown_until and when < cooldown_until[token]:
            skips["instrument_cooldown"] = skips.get("instrument_cooldown", 0) + 1
            return False
        if any(o.token == token and o.side == signal.side for o in open_trades if token):
            skips["already_open_same_token"] = skips.get("already_open_same_token", 0) + 1
            return False
        if max_concurrent > 0 and len(open_trades) >= max_concurrent:
            skips["max_concurrent"] = skips.get("max_concurrent", 0) + 1
            return False

        prem, _ = _mark_premium(
            when=when,
            spot=spot,
            side=signal.side,
            strike=inst.strike,
            token=token,
            bar_frac=bar_frac,
            bar=bar,
        )
        equity = starting + realized
        lots, premium_cost = _fit_lots(
            risk_cfg=cfg.risk,
            confidence=int(signal.confidence or 0),
            entry_ltp=prem,
            lot_size=inst.lot_size,
            available=available,
            deployed=deployed,
            equity=equity,
            strike_pick=strike_pick,
        )
        if lots < 1:
            skips["capital_or_deploy_cap"] = skips.get("capital_or_deploy_cap", 0) + 1
            return False

        greeks_mult = Decimal("1")
        if strike_pick:
            stub = CandidateSignal(
                ts=when,
                setup_type=signal.setup_type,
                side=signal.side,
                instrument_token=token,
                tsym=inst.tsym,
                strategy_version="replay",
                feature_snapshot=FeatureSnapshot(ts=when, nifty_spot=spot),
                scanner_metadata={"strike_pick": strike_pick},
            )
            greeks_mult = greeks_confirmation_multiplier(stub, None, cfg.risk)
        entry_meta = {
            "greeks_mult": float(greeks_mult),
            "volume": (strike_pick or {}).get("volume"),
            "oi": (strike_pick or {}).get("oi"),
        }

        available -= premium_cost
        deployed += premium_cost
        open_trades.append(
            ReplayOpenTrade(
                strategy=signal.setup_type,
                side=signal.side,
                strike=inst.strike,
                entry_ts=when,
                entry_spot=spot,
                entry_premium=prem,
                lot_size=inst.lot_size,
                lots=lots,
                confidence=int(signal.confidence or 0),
                tsym=inst.tsym,
                token=token,
                entry_meta=entry_meta,
            )
        )
        entries += 1
        return True

    last_bar = m1_all[-1]
    is_last_tick = False

    for idx, (kind, payload) in enumerate(timeline):
        is_last_tick = idx == len(timeline) - 1
        if kind == "exit":
            tick: ExitTick = payload  # type: ignore[assignment]
            ts_ist = tick.ts.astimezone(IST)
            force = ts_ist.time() >= force_exit_t or is_last_tick
            _evaluate_exits(
                tick.ts, tick.spot, tick.bar, tick.bar_frac, force=force
            )
            continue

        scan: ScanTick = payload  # type: ignore[assignment]
        ts = scan.ts
        ts_ist = ts.astimezone(IST)
        t_clock = ts_ist.time()
        bar = bar_for_ts(m1_all, ts) or last_bar
        spot = spot_from_bar(bar, ts, use_intra_bar=live_parity)
        bar_frac = bar_fraction(bar, ts)
        scans_run += 1

        feat, regime, m1, m3, m5, uni, band_states = _apply_market(
            ts, spot, bar, bar_frac
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
        strike = Decimal(str(pick.get("strike", uni.atm_strike)))
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
            prem0, _ = _mark_premium(
                when=ts,
                spot=spot,
                side=side,
                strike=strike,
                token=inst.token,
                bar_frac=bar_frac,
                bar=bar,
            )
            opt_st = OptionState(
                instrument_token=inst.token,
                tsym=inst.tsym,
                ltp=prem0,
                bid=prem0 - Decimal("0.5"),
                ask=prem0 + Decimal("0.5"),
                spread_pct=Decimal("1"),
            )

        has_open = any(o.token == inst.token for o in open_trades if inst.token)
        in_cd = (
            global_cooldown_until is not None and ts < global_cooldown_until
        ) or (
            inst.token in cooldown_until and ts < cooldown_until[inst.token]
        )
        v = validator.validate(
            signal,
            opt_st,
            has_open_for_token=has_open,
            in_cooldown=in_cd,
            kill_switch=False,
            is_expiry_day=(expiry_d == day),
            now=ts,
            premium_tracker=premium_tracker,
            regime_primary=regime.primary,
            option_m1=_validator_option_m1(inst.token if inst else None),
            spot_m1=m1,
        )
        would_enter = v.passed
        entered = False
        if would_enter:
            validator_passed += 1
            entered = _try_enter(
                signal=signal,
                inst=inst,
                opt_st=opt_st,
                when=ts,
                spot=spot,
                bar_frac=bar_frac,
                bar=bar,
                ts_ist=ts_ist,
                strike_pick=pick,
            )

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
                "entered": entered,
                "router_reason": decision.selected_reason,
            }
        )

    # Force-close any remaining positions at session end
    if open_trades and m1_all:
        last_ts = m1_all[-1].ts + timedelta(seconds=59)
        last_spot = m1_all[-1].close
        _evaluate_exits(
            last_ts,
            last_spot,
            m1_all[-1],
            Decimal("1"),
            force=True,
        )

    return FullSessionReplayResult(
        day=day,
        underlying=ul,
        schedule_source=schedule.source,
        scans_run=scans_run,
        signals_seen=signals_seen,
        validator_passed=validator_passed,
        decisions=decisions,
        skips=skips,
        entries=entries,
        closed=closed_trades,
        realized_pnl=realized.quantize(Decimal("0.01")),
    )
