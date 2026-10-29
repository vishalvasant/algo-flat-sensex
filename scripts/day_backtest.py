#!/usr/bin/env python3
"""Replay today's session with current strategy router and estimate paper P&L.

Usage (from repo root):
  CONFIG_DIR=./config PYTHONPATH=trading-engine/src \\
    trading-engine/.venv/bin/python scripts/day_backtest.py
  trading-engine/.venv/bin/python scripts/day_backtest.py --date 2026-07-16
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading-engine" / "src"))
os.chdir(ROOT)
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
# Quiet scanner/router logs — live-parity ticks are dense.
os.environ.setdefault("STRUCTLOG_LEVEL", "WARNING")

import logging

logging.basicConfig(level=logging.WARNING)
for _name in ("algoflat", "httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

from algoflat.broker.auth import ensure_session
from algoflat.broker.base import BrokerAdapter
from algoflat.broker.factory import create_live_broker
from algoflat.bus.event_bus import EventBus
from algoflat.broker.credentials import FlattradeConfig
from algoflat.config import AppConfig, get_config
from algoflat.contract_selector.resolve import resolve_side_contract
from algoflat.contract_selector.expiry import format_expiry_tag, next_weekly_expiry_dates
from algoflat.contract_selector.selector import ContractSelector, ContractUniverse
from algoflat.contract_selector.strike_picker import (
  atm_band_instruments,
  candidate_instruments,
  effective_quote_band_steps,
)
from algoflat.features.chain_intel import build_chain_snapshot
from algoflat.features.engine import FeatureEngine
from algoflat.market_intelligence.context import MarketIntelligenceContext
from algoflat.market_intelligence.engine import MarketIntelligenceEngine
from algoflat.journal.analytics import StrategyLearner
from algoflat.market_data.engine import MarketDataEngine
from algoflat.market_data.vwap import session_vwap
from algoflat.models.events import (
    Bias,
    CandidateSignal,
    Candle,
    CandleInterval,
    FeatureSnapshot,
    Instrument,
    OptionState,
)
from algoflat.option_data.greeks import DEFAULT_RATE, _bs_price, compute_greeks, years_to_expiry
from algoflat.position.exit_rules import evaluate_momentum_exit, should_queue_reversal_flip
from algoflat.quality.gate import QualityGate
from algoflat.regime.classifier import RegimeClassifier
from algoflat.risk.engine import fit_lots_to_capital, lots_for_confidence
from algoflat.risk.greeks_sizing import greeks_confirmation_multiplier
from algoflat.scanner.library import build_strategy_scanners
from algoflat.strategy.router import StrategyRouter
from algoflat.trading.entry_confirmation import (
    EntryConfirmationTracker,
    in_open_gap_entry_window,
)
from algoflat.validator.engine import RuleValidator
from algoflat.validator.premium_behaviour import PremiumBehaviourTracker

IST = ZoneInfo("Asia/Kolkata")
ASSUMED_IV = 0.14  # ~14% for ATM weekly premium path when option candles missing
LOT_FALLBACK = 65
SNAP_ROOT = ROOT / "reports" / "snaps"


def _resolve_lot_size(
    inst: Instrument | None,
    *,
    tsym: str = "",
    fallback: int = LOT_FALLBACK,
) -> int:
    """NIFTY options: 1 lot = 65 qty (ignore bogus metadata lot_size=1)."""
    from algoflat.symbols_util import lot_size_for_tsym

    if inst is not None:
        return lot_size_for_tsym(inst.tsym, metadata_lot_size=inst.lot_size)
    if tsym:
        return lot_size_for_tsym(tsym, metadata_lot_size=fallback)
    return lot_size_for_tsym("NIFTY", metadata_lot_size=fallback)


def _premium_deploy(premium: Decimal, lot_size: int, lots: int) -> Decimal:
    """Capital required: premium per unit × lot size × number of lots."""
    return premium * Decimal(lot_size) * Decimal(lots)


def _snap_dir(day: date, underlying: str = "NIFTY") -> Path:
    return SNAP_ROOT / day.isoformat() / underlying.upper()


def _snap_dirs_to_try(day: date, underlying: str = "NIFTY") -> list[Path]:
    """Per-underlying snap dir, then legacy flat layout (pre dual-index paths)."""
    ul = underlying.upper()
    legacy = SNAP_ROOT / day.isoformat()
    dirs = [_snap_dir(day, ul)]
    if legacy != dirs[0]:
        dirs.append(legacy)
    return dirs


def _candle_to_dict(c: Candle) -> dict[str, Any]:
    return {
        "instrument_token": c.instrument_token,
        "ts": c.ts.isoformat(),
        "open": str(c.open),
        "high": str(c.high),
        "low": str(c.low),
        "close": str(c.close),
        "volume": c.volume,
        "interval": c.interval.value,
        "vwap": str(c.vwap) if c.vwap is not None else None,
        "oi": c.oi,
    }


def _candle_from_dict(d: dict[str, Any]) -> Candle:
    return Candle(
        instrument_token=str(d["instrument_token"]),
        ts=datetime.fromisoformat(d["ts"]),
        open=Decimal(str(d["open"])),
        high=Decimal(str(d["high"])),
        low=Decimal(str(d["low"])),
        close=Decimal(str(d["close"])),
        volume=d.get("volume"),
        interval=CandleInterval(d["interval"]),
        vwap=Decimal(str(d["vwap"])) if d.get("vwap") not in (None, "") else None,
        oi=d.get("oi"),
    )


def _instrument_to_dict(i: Instrument) -> dict[str, Any]:
    return i.model_dump(mode="json")


def _instrument_from_dict(d: dict[str, Any]) -> Instrument:
    return Instrument.model_validate(d)


def save_day_snap(
    day: date,
    *,
    underlying: str,
    all_candles: dict[CandleInterval, list[Candle]],
    prior_day: date | None,
    prior_m5: list[Candle],
    universe: ContractUniverse,
    option_series: dict[str, list[Candle]],
    cash_m1: list[Candle] | None = None,
    fut_m1: list[Candle] | None = None,
) -> Path:
    """Persist full-day 1m/3m/5m spot + option series for reproducible replay.

    This is NOT tick data — Flattrade finest history is 1-minute TPSeries.
    """
    out = _snap_dir(day, underlying)
    out.mkdir(parents=True, exist_ok=True)
    meta = {
        "day": day.isoformat(),
        "underlying": underlying.upper(),
        "saved_at": datetime.now(tz=IST).isoformat(),
        "source": "flattrade_tpseries",
        "note": (
            "Finest available historical bars (1m OHLC + intvwap/oi when present). "
            "Not tick-perfect — broker does not expose historical ticks."
        ),
        "spot_bars": {k.value: len(v) for k, v in all_candles.items()},
        "option_tokens": len(option_series),
        "option_bars_total": sum(len(v) for v in option_series.values()),
        "prior_day": prior_day.isoformat() if prior_day else None,
        "universe_instruments": len(universe.instruments),
        "expiry_symbol": universe.expiry_symbol,
        "atm_strike": str(universe.atm_strike),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    spot_payload = {
        interval.value: [_candle_to_dict(c) for c in bars]
        for interval, bars in all_candles.items()
    }
    (out / "spot.json").write_text(json.dumps(spot_payload))
    (out / "prior_day.json").write_text(
        json.dumps(
            {
                "date": prior_day.isoformat() if prior_day else None,
                "m5": [_candle_to_dict(c) for c in prior_m5],
            }
        )
    )
    uni_payload = {
        "spot": str(universe.spot),
        "atm_strike": str(universe.atm_strike),
        "expiry_symbol": universe.expiry_symbol,
        "instruments": [_instrument_to_dict(i) for i in universe.instruments],
        "atm_ce_token": universe.atm_ce.token if universe.atm_ce else None,
        "atm_pe_token": universe.atm_pe.token if universe.atm_pe else None,
        "subscription_keys": list(universe.subscription_keys),
    }
    (out / "universe.json").write_text(json.dumps(uni_payload, indent=2))
    opt_payload = {
        token: [_candle_to_dict(c) for c in bars]
        for token, bars in option_series.items()
    }
    (out / "options.json").write_text(json.dumps(opt_payload))
    if cash_m1:
        (out / "cash.json").write_text(
            json.dumps([_candle_to_dict(c) for c in cash_m1])
        )
    if fut_m1:
        (out / "fut.json").write_text(
            json.dumps([_candle_to_dict(c) for c in fut_m1])
        )
    print(
        f"  Snap saved → {out}  "
        f"(spot 1m={meta['spot_bars'].get('1m', 0)}  "
        f"options={meta['option_tokens']} tokens / "
        f"{meta['option_bars_total']} bars)"
    )
    return out


def _normalize_universe_lot_sizes(
    universe: ContractUniverse,
    *,
    underlying: str = "NIFTY",
) -> ContractUniverse:
    """Ensure NIFTY instruments use exchange lot size (65) — DB/scrip rows often store 1."""
    from algoflat.symbols_util import lot_size_for_tsym

    fixed: list[Instrument] = []
    for inst in universe.instruments:
        ls = lot_size_for_tsym(inst.tsym, metadata_lot_size=inst.lot_size)
        fixed.append(inst if ls == inst.lot_size else inst.model_copy(update={"lot_size": ls}))

    def _fix_ref(ref: Instrument | None) -> Instrument | None:
        if ref is None:
            return None
        ls = lot_size_for_tsym(ref.tsym, metadata_lot_size=ref.lot_size)
        return ref if ls == ref.lot_size else ref.model_copy(update={"lot_size": ls})

    return ContractUniverse(
        spot=universe.spot,
        atm_strike=universe.atm_strike,
        expiry_symbol=universe.expiry_symbol,
        instruments=fixed,
        atm_ce=_fix_ref(universe.atm_ce),
        atm_pe=_fix_ref(universe.atm_pe),
        subscription_keys=list(universe.subscription_keys),
    )


def _repair_empty_universe(
    universe: ContractUniverse,
    day: date,
    underlying: str,
) -> ContractUniverse:
    """Reuse instruments from a nearby snap when universe.json was wiped."""
    if universe.instruments:
        return universe
    target_exp = universe.expiry_symbol
    # Search ±45 calendar days for a snap with matching expiry (historical replay).
    for offset in range(-45, 46):
        d = day + timedelta(days=offset)
        if d.weekday() >= 5:
            continue
        snap = load_day_snap(d, underlying)
        if not snap:
            continue
        cand = snap["universe"]
        if not cand.instruments:
            continue
        if target_exp and cand.expiry_symbol and cand.expiry_symbol != target_exp:
            continue
        print(
            f"  WARN: empty universe — reusing instruments from {d.isoformat()} snap "
            f"expiry={cand.expiry_symbol} count={len(cand.instruments)}"
        )
        return ContractUniverse(
            spot=universe.spot,
            atm_strike=universe.atm_strike,
            expiry_symbol=cand.expiry_symbol or universe.expiry_symbol,
            instruments=cand.instruments,
            atm_ce=cand.atm_ce,
            atm_pe=cand.atm_pe,
            subscription_keys=cand.subscription_keys,
        )
    prior = _prior_snap_universe(day, underlying, lookback=45)
    if prior is None or not prior.instruments:
        return universe
    print(
        f"  WARN: empty universe — reusing instruments from prior snap "
        f"expiry={prior.expiry_symbol} count={len(prior.instruments)}"
    )
    return ContractUniverse(
        spot=universe.spot,
        atm_strike=universe.atm_strike,
        expiry_symbol=prior.expiry_symbol or universe.expiry_symbol,
        instruments=prior.instruments,
        atm_ce=prior.atm_ce,
        atm_pe=prior.atm_pe,
        subscription_keys=prior.subscription_keys,
    )


def _ensure_universe(
    universe: ContractUniverse,
    day: date,
    spot: Decimal,
    config: AppConfig,
    *,
    underlying: str,
) -> ContractUniverse:
    """Repair from nearby snaps; fall back to BS synthetic chain when broker data is missing."""
    repaired = _repair_empty_universe(universe, day, underlying)
    if not repaired.instruments:
        return _synthetic_universe(spot, day, config, underlying=underlying)
    return repaired


def _prior_snap_universe(day: date, underlying: str, *, lookback: int = 8) -> ContractUniverse | None:
    """Reuse instruments from a recent snap when broker no longer lists expired weeklies."""
    d = day - timedelta(days=1)
    for _ in range(lookback):
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        prior = load_day_snap(d, underlying)
        if prior and prior["universe"].instruments:
            return prior["universe"]
        d -= timedelta(days=1)
    return None


def load_day_snap(day: date, underlying: str = "NIFTY") -> dict[str, Any] | None:
    """Load a previously saved day snap. Returns None if incomplete."""
    required = ("meta.json", "spot.json", "prior_day.json", "universe.json", "options.json")
    out: Path | None = None
    for candidate in _snap_dirs_to_try(day, underlying):
        if all((candidate / name).exists() for name in required):
            out = candidate
            break
    if out is None:
        return None
    spot_raw = json.loads((out / "spot.json").read_text())
    all_candles: dict[CandleInterval, list[Candle]] = {}
    for key, rows in spot_raw.items():
        all_candles[CandleInterval(key)] = [_candle_from_dict(r) for r in rows]
    prior_raw = json.loads((out / "prior_day.json").read_text())
    prior_day = (
        date.fromisoformat(prior_raw["date"]) if prior_raw.get("date") else None
    )
    prior_m5 = [_candle_from_dict(r) for r in prior_raw.get("m5") or []]
    uni_raw = json.loads((out / "universe.json").read_text())
    instruments = [_instrument_from_dict(i) for i in uni_raw.get("instruments") or []]
    by_token = {i.token: i for i in instruments}
    atm_ce = by_token.get(uni_raw.get("atm_ce_token") or "")
    atm_pe = by_token.get(uni_raw.get("atm_pe_token") or "")
    universe = ContractUniverse(
        spot=Decimal(str(uni_raw["spot"])),
        atm_strike=Decimal(str(uni_raw["atm_strike"])),
        expiry_symbol=uni_raw.get("expiry_symbol"),
        instruments=instruments,
        atm_ce=atm_ce,
        atm_pe=atm_pe,
        subscription_keys=list(uni_raw.get("subscription_keys") or []),
    )
    universe = _normalize_universe_lot_sizes(universe, underlying=underlying)
    opt_raw = json.loads((out / "options.json").read_text())
    option_series = {
        token: [_candle_from_dict(r) for r in rows] for token, rows in opt_raw.items()
    }
    meta = json.loads((out / "meta.json").read_text())
    cash_m1: list[Candle] = []
    fut_m1: list[Candle] = []
    if (out / "cash.json").exists():
        cash_m1 = [_candle_from_dict(r) for r in json.loads((out / "cash.json").read_text())]
    if (out / "fut.json").exists():
        fut_m1 = [_candle_from_dict(r) for r in json.loads((out / "fut.json").read_text())]
    print(
        f"  Snap loaded ← {out}  "
        f"(saved {meta.get('saved_at', '?')}; "
        f"1m={len(all_candles.get(CandleInterval.M1, []))}  "
        f"options={len(option_series)})"
    )
    return {
        "all_candles": all_candles,
        "cash_m1": cash_m1 or None,
        "fut_m1": fut_m1 or None,
        "prior_day": prior_day,
        "prior_m5": prior_m5,
        "universe": universe,
        "option_series": option_series,
        "meta": meta,
    }


@dataclass
class OpenTrade:
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
    premium_source: str = "bs"
    tsym: str = ""
    token: str = ""
    entry_meta: dict = field(default_factory=dict)


@dataclass
class ClosedTrade:
    strategy: str
    side: str
    strike: Decimal
    entry_ts: datetime
    exit_ts: datetime
    entry_spot: Decimal
    exit_spot: Decimal
    entry_premium: Decimal
    exit_premium: Decimal
    lots: int
    lot_size: int
    pnl: Decimal
    exit_reason: str
    confidence: int
    premium_source: str
    tsym: str = ""


@dataclass
class DayStats:
    signals_seen: int = 0
    entries: int = 0
    skips: dict[str, int] = field(default_factory=dict)
    closed: list[ClosedTrade] = field(default_factory=list)


def _round_strike(spot: Decimal, step: Decimal) -> Decimal:
    return (spot / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step


def _bs_premium(
    spot: Decimal,
    strike: Decimal,
    side: str,
    when: datetime,
    expiry: date,
    iv: float = ASSUMED_IV,
) -> Decimal:
    t = years_to_expiry(expiry, now=when)
    px = _bs_price(float(spot), float(strike), t, DEFAULT_RATE, iv, side)
    return Decimal(str(round(max(px, 0.05), 2)))


def _premium_at(
    *,
    when: datetime,
    spot: Decimal,
    side: str,
    strike: Decimal,
    expiry: date,
    option_series: dict[str, list[Candle]],
    token: str | None,
) -> tuple[Decimal, str]:
    if token and token in option_series:
        bars = option_series[token]
        # last bar at or before when
        best: Candle | None = None
        for c in bars:
            if c.ts <= when:
                best = c
            else:
                break
        if best is not None and best.close > 0:
            return best.close, "option_candle"
    return _bs_premium(spot, strike, side, when, expiry), "bs"


def _exit_decision(
    *,
    side: str,
    entry_premium: Decimal,
    entry_ts: datetime,
    now: datetime,
    current_premium: Decimal,
    mfe: Decimal,
    spot: Decimal,
    vwap: Decimal | None,
    force_exit: bool,
    cfg: dict,
    market_data: MarketDataEngine,
    regime_primary: str | None = None,
    confidence: int = 0,
    setup_type: str = "",
    entry_meta: dict | None = None,
) -> tuple[bool, str | None]:
    """Thin wrapper — same exit engine as live (`evaluate_momentum_exit`)."""
    # Ensure MD mirrors replay spot for VWAP-flip checks inside exit_rules.
    market_data._spot_ltp = spot  # type: ignore[attr-defined]
    decision = evaluate_momentum_exit(
        option_side=side,
        entry_price=entry_premium,
        entry_ts=entry_ts,
        current_ltp=current_premium,
        mfe_points=mfe,
        market_data=market_data,
        cfg=cfg,
        force_exit=force_exit,
        regime_primary=regime_primary,
        confidence=confidence or None,
        setup_type=setup_type or None,
        entry_meta=entry_meta,
        now=now,
    )
    return decision.should_exit, decision.reason


def _fit_lots(
    *,
    risk_cfg: dict,
    confidence: int,
    entry_ltp: Decimal,
    lot_size: int,
    available: Decimal,
    deployed: Decimal,
    equity: Decimal,
    strike_pick: dict | None = None,
) -> tuple[int, Decimal]:
    """Delegate to live RiskEngine sizing (backtest ≡ production)."""
    greeks_mult = Decimal("1")
    if strike_pick:
        stub = CandidateSignal(
            ts=datetime.now(tz=timezone.utc),
            setup_type="backtest",
            side="CE",
            instrument_token="0",
            tsym="BT",
            strategy_version="bt",
            feature_snapshot=FeatureSnapshot(ts=datetime.now(tz=timezone.utc)),
            scanner_metadata={"strike_pick": strike_pick},
        )
        greeks_mult = greeks_confirmation_multiplier(stub, None, risk_cfg)
    return fit_lots_to_capital(
        risk_cfg,
        confidence=confidence,
        entry_ltp=entry_ltp,
        lot_size=lot_size,
        available=available,
        deployed=deployed,
        equity=equity,
        greeks_mult=greeks_mult,
    )


def _slice_upto(bars: list[Candle], ts: datetime) -> list[Candle]:
    return [c for c in bars if c.ts <= ts]


def _slice_before(bars: list[Candle], ts: datetime) -> list[Candle]:
    return [c for c in bars if c.ts < ts]


def _slice_closed_period(
    bars: list[Candle], when: datetime, period_min: int
) -> list[Candle]:
    """Only bars whose period has fully ended before ``when`` (no forming HTF bar)."""
    out: list[Candle] = []
    for c in bars:
        if c.ts + timedelta(minutes=period_min) <= when:
            out.append(c)
    return out


def _honest_spot(
    bar: Candle, when: datetime, prior_m1: list[Candle]
) -> Decimal:
    """Spot LTP without peeking at the current minute's close/VWAP."""
    minute_end = bar.ts + timedelta(minutes=1)
    if when >= minute_end:
        return bar.close
    closed = [c for c in prior_m1 if c.ts < bar.ts]
    if closed:
        return closed[-1].close
    return bar.open


def _honest_premium_path(
    *,
    when: datetime,
    bar: Candle,
    spot: Decimal,
    side: str,
    strike: Decimal,
    expiry: date,
    option_series: dict[str, list[Candle]],
    token: str | None,
) -> tuple[Decimal, str]:
    """Option mark using last closed 1m candle — no intra-minute close/VWAP peek."""
    minute_end = bar.ts + timedelta(minutes=1)
    mark_when = when if when >= minute_end else bar.ts - timedelta(seconds=1)
    prem, src = _premium_at(
        when=mark_when,
        spot=spot,
        side=side,
        strike=strike,
        expiry=expiry,
        option_series=option_series,
        token=token,
    )
    if when >= minute_end and token and token in option_series:
        for c in option_series[token]:
            cts = c.ts if c.ts.tzinfo else c.ts.replace(tzinfo=timezone.utc)
            if cts == bar.ts and c.close > 0:
                return c.close, "option_candle_closed"
    return prem, src


def _scan_waypoints(bar: Candle, scan_sec: int) -> list[tuple[datetime, Decimal]]:
    """Approximate live scans inside a 1m bar (open → mid → … toward close).

    Live engine scans every ``scan_interval_seconds`` on ticking LTP; a 1m-only
    backtest under-counts entries. We synthesize intra-bar spots from OHLC.
    """
    return [
        (ts, spot)
        for ts, spot, _allow in _live_parity_waypoints(bar, exit_sec=scan_sec, scan_sec=scan_sec)
    ]


def _bar_path_price(
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    bar_frac: Decimal,
    bar_vwap: Decimal | None = None,
) -> Decimal:
    """Intra-bar path without fake H/L visits.

    Flattrade has no historical ticks — finest data is 1m OHLC (+ intvwap).
    Use O → (bar VWAP or mid) → C so we don't invent adverse spikes from high/low.
    """
    frac = max(Decimal("0"), min(Decimal("1"), bar_frac))
    mid = bar_vwap if bar_vwap is not None else (high + low) / Decimal("2")
    if frac <= Decimal("0.5"):
        return open_ + (mid - open_) * (frac * 2)
    return mid + (close - mid) * ((frac - Decimal("0.5")) * 2)


def _ohlc_path_price(
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    bar_frac: Decimal,
) -> Decimal:
    """Compat alias — prefers mid path (no extreme thrash)."""
    return _bar_path_price(open_, high, low, close, bar_frac, None)


def _live_parity_waypoints(
    bar: Candle,
    *,
    exit_sec: int,
    scan_sec: int,
    prior_m1: list[Candle] | None = None,
    honest: bool = False,
) -> list[tuple[datetime, Decimal, bool]]:
    """Fine exit ticks + coarser entry scans from 1m snap (not true ticks).

    Path: open → bar VWAP/mid → close. Entries every scan_interval_seconds.
    """
    exit_sec = max(1, int(exit_sec))
    scan_sec = max(exit_sec, int(scan_sec))
    points: list[tuple[datetime, Decimal, bool]] = []
    end = bar.ts + timedelta(minutes=1)
    t = bar.ts
    while t < end:
        frac = Decimal(str((t - bar.ts).total_seconds() / 60.0))
        if honest:
            spot = _honest_spot(bar, t, prior_m1 or [])
        else:
            spot = _bar_path_price(
                bar.open, bar.high, bar.low, bar.close, frac, bar.vwap
            )
        sec = int((t - bar.ts).total_seconds())
        allow_entry = (sec % scan_sec) == 0
        points.append((t, spot, allow_entry))
        t += timedelta(seconds=exit_sec)
    last_ts = bar.ts + timedelta(seconds=59)
    if not points or points[-1][0] < last_ts:
        points.append((last_ts, bar.close, True))
    return points


def _forming_m1(bar: Candle, when: datetime, spot: Decimal, *, honest: bool = False) -> Candle:
    """Synthetic forming 1m bar so bias/setup see current scan price as close."""
    if honest:
        return Candle(
            instrument_token=bar.instrument_token,
            ts=bar.ts,
            open=bar.open,
            high=max(bar.open, spot),
            low=min(bar.open, spot),
            close=spot,
            volume=bar.volume,
            interval=CandleInterval.M1,
        )
    return Candle(
        instrument_token=bar.instrument_token,
        ts=bar.ts,
        open=bar.open,
        high=max(bar.open, bar.high, spot),
        low=min(bar.open, bar.low, spot),
        close=spot,
        volume=bar.volume,
        interval=CandleInterval.M1,
    )


def _option_bid_ask_spread(
    prem: Decimal,
    *,
    token: str | None,
    when: datetime,
    option_series: dict[str, list[Candle]],
) -> tuple[Decimal, Decimal, Decimal]:
    """Estimate bid/ask like liquid live quotes (not bar high−low).

    1m OHLC range is traded volatility, not the bid–ask. Using it as spread
    false-triggers ``abnormal_spread`` / ``spread_too_wide``. Snap has no
    bid/ask, so we synthesize a tight market (~0.8% of LTP) matching typical
    NIFTY weekly ATM quotes — same formula live uses: (ask−bid)/ask×100.
    """
    del token, when, option_series  # reserved for future snap bid/ask fields
    if prem <= 0:
        return prem, prem, Decimal("0")
    # Full spread ≈ 0.8% of LTP (under validator max_spread_pct default 1.5).
    half = max(Decimal("0.05"), (prem * Decimal("0.004")).quantize(Decimal("0.05")))
    bid = max(Decimal("0.05"), prem - half)
    ask = prem + half
    spread_pct = ((ask - bid) / ask * Decimal("100")).quantize(Decimal("0.01"))
    return bid, ask, spread_pct


def _premium_path(
    *,
    when: datetime,
    spot: Decimal,
    side: str,
    strike: Decimal,
    expiry: date,
    option_series: dict[str, list[Candle]],
    token: str | None,
    bar_frac: Decimal,
    bar: Candle | None = None,
    honest: bool = False,
) -> tuple[Decimal, str]:
    """Option premium at an intra-bar tick using that minute's option OHLC path."""
    if honest and bar is not None:
        return _honest_premium_path(
            when=when,
            bar=bar,
            spot=spot,
            side=side,
            strike=strike,
            expiry=expiry,
            option_series=option_series,
            token=token,
        )
    if token and token in option_series:
        series = option_series[token]
        # Candle ts is usually minute start; pick the bar covering ``when``.
        minute = when.replace(second=0, microsecond=0)
        best: Candle | None = None
        for c in series:
            cts = c.ts
            if cts.tzinfo is None:
                cts = cts.replace(tzinfo=timezone.utc)
            if cts <= minute:
                best = c
            else:
                break
        if best is not None and best.close > 0:
            px = _bar_path_price(
                best.open, best.high, best.low, best.close, bar_frac, best.vwap
            )
            return max(px, Decimal("0.05")), "option_candle"
    return _bs_premium(spot, strike, side, when, expiry), "bs"


def _fake_inst(
    side: str,
    strike: Decimal,
    lot: int,
    *,
    underlying: str = "NIFTY",
    exchange: str = "NFO",
    token: str = "",
    tsym: str = "",
) -> Instrument:
    from algoflat.symbols_util import lot_size_for_tsym

    ul = underlying.upper()
    ls = lot_size_for_tsym(tsym or f"{ul}{strike}{side}", metadata_lot_size=lot)
    return Instrument(
        exchange=exchange,
        token=token or f"SIM-{side}-{strike}",
        tsym=tsym or f"{ul}{strike}{side}",
        underlying=ul,
        expiry_date=None,
        strike=strike,
        option_type=side,
        lot_size=ls,
        is_atm=True,
        in_band=True,
    )


def _synthetic_universe(
    spot: Decimal,
    day: date,
    config: AppConfig,
    *,
    underlying: str = "NIFTY",
) -> ContractUniverse:
    """Calendar weekly expiry + ATM band when broker/scripmaster lack expired series."""
    from algoflat.contract_selector.expiry import (
        format_expiry_tag,
        next_weekly_expiry_dates,
        parse_expiry_tag,
    )

    ul = underlying.upper()
    sym = config.symbols
    step = Decimal(str(sym.get("strike_step", 50)))
    steps = int(sym.get("atm_strike_steps", 5))
    exchange = str(sym.get("exchange_options", "NFO"))
    lot = LOT_FALLBACK
    atm = _round_strike(spot, step)
    cal = next_weekly_expiry_dates(
        underlying=ul, as_of=day, count=1, include_today_if_expiry=True
    )
    expiry_tag = format_expiry_tag(cal[0]) if cal else None
    expiry_dt: datetime | None = None
    if expiry_tag:
        parsed = parse_expiry_tag(expiry_tag)
        if parsed:
            expiry_dt = datetime.combine(parsed, time(15, 30), tzinfo=IST)
    instruments: list[Instrument] = []
    atm_ce: Instrument | None = None
    atm_pe: Instrument | None = None
    for offset in range(-steps, steps + 1):
        strike = atm + (step * offset)
        is_atm = offset == 0
        for side in ("CE", "PE"):
            tsym = f"{ul}{expiry_tag or ''}{side[0]}{int(strike)}"
            token = f"syn_{expiry_tag}_{int(strike)}{side}"
            inst = Instrument(
                exchange=exchange,
                token=token,
                tsym=tsym,
                underlying=ul,
                expiry_date=expiry_dt,
                strike=strike,
                option_type=side,
                lot_size=lot,
                is_atm=is_atm,
                in_band=True,
            )
            instruments.append(inst)
            if is_atm and side == "CE":
                atm_ce = inst
            if is_atm and side == "PE":
                atm_pe = inst
    print(
        f"  Synthetic universe expiry={expiry_tag} atm={atm} "
        f"instruments={len(instruments)} (BS premiums — expired chain unavailable)"
    )
    return ContractUniverse(
        spot=spot,
        atm_strike=atm,
        expiry_symbol=expiry_tag,
        instruments=instruments,
        atm_ce=atm_ce,
        atm_pe=atm_pe,
    )


def _session_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(9, 15), tzinfo=IST).astimezone(timezone.utc)
    end = datetime.combine(day, time(15, 30), tzinfo=IST).astimezone(timezone.utc)
    now = datetime.now(tz=timezone.utc)
    if end > now:
        end = now
    return start, end


async def _discover_spot_tokens_from_db(
    conn: Any,
    start: datetime,
    end: datetime,
    *,
    min_bars: int = 30,
) -> list[str]:
    """Futures/index tokens on session day (avg close at index scale, not option premiums)."""
    ranked = await _discover_spot_tokens_ranked(conn, start, end, min_bars=min_bars)
    ranked.sort(key=lambda x: -x[2])
    return [tok for tok, _, _ in ranked]


async def _discover_spot_tokens_ranked(
    conn: Any,
    start: datetime,
    end: datetime,
    *,
    min_bars: int = 30,
) -> list[tuple[str, float, int]]:
    rows = await conn.fetch(
        """
        SELECT instrument_token, AVG(close)::float AS avg_close, COUNT(*)::int AS cnt
        FROM candles_1m
        WHERE ts >= $1 AND ts <= $2
        GROUP BY instrument_token
        HAVING COUNT(*) >= $3
        """,
        start,
        end,
        min_bars,
    )
    ranked: list[tuple[str, float, int]] = []
    for row in rows:
        avg = float(row["avg_close"] or 0)
        if avg >= 5000:
            ranked.append((str(row["instrument_token"]), avg, int(row["cnt"])))
    ranked.sort(key=lambda x: x[1])
    return ranked


async def _m1_series_for_token(
    md: MarketDataEngine,
    conn: Any,
    token: str,
    start: datetime,
    end: datetime,
) -> list[Candle]:
    rows = await md.candles_from_db_for_tokens(
        conn, [token], CandleInterval.M1, start, end
    )
    return list(rows or [])


def _price_at(series: list[Candle], when: datetime) -> Decimal | None:
    if not series:
        return None
    when_utc = when.astimezone(timezone.utc) if when.tzinfo else when.replace(tzinfo=timezone.utc)
    best: Candle | None = None
    for c in series:
        cts = c.ts if c.ts.tzinfo else c.ts.replace(tzinfo=timezone.utc)
        if cts <= when_utc:
            best = c
        else:
            break
    return best.close if best is not None else None


def _extend_fut_from_cash(
    cash: list[Candle],
    fut: list[Candle],
    *,
    fut_token: str,
) -> list[Candle]:
    """When futures M1 stops early in DB, synthesize rest from cash + last basis."""
    if not cash:
        return fut
    if not fut:
        return cash
    last_fut = fut[-1]
    cash_at = _price_at(cash, last_fut.ts) or cash[-1].close
    basis = last_fut.close - cash_at
    if basis == 0:
        return fut
    extended = list(fut)
    last_ts = last_fut.ts
    for c in cash:
        cts = c.ts if c.ts.tzinfo else c.ts.replace(tzinfo=timezone.utc)
        lts = last_ts if last_ts.tzinfo else last_ts.replace(tzinfo=timezone.utc)
        if cts <= lts:
            continue
        extended.append(
            Candle(
                instrument_token=fut_token,
                ts=c.ts,
                open=c.open + basis,
                high=c.high + basis,
                low=c.low + basis,
                close=c.close + basis,
                volume=c.volume,
                interval=CandleInterval.M1,
                vwap=(c.vwap + basis) if c.vwap is not None else None,
                oi=c.oi,
            )
        )
    return extended


def _session_driver_m1(
    stitched: list[Candle],
    cash: list[Candle],
    fut: list[Candle],
) -> list[Candle]:
    """Pick longest session M1 for bar loop (full day over partial futures)."""
    candidates = [stitched, cash, fut]
    return max(candidates, key=len)


async def _discover_option_tokens_from_db(
    conn: Any,
    start: datetime,
    end: datetime,
    *,
    underlying: str,
    expiry_date: date | None,
    min_bars: int = 30,
) -> list[str]:
    """Option 1m tokens for the session (premium-scale closes)."""
    if expiry_date is not None:
        rows = await conn.fetch(
            """
            SELECT c.instrument_token, COUNT(*)::int AS cnt
            FROM candles_1m c
            JOIN instruments i ON i.token = c.instrument_token
            WHERE c.ts >= $1 AND c.ts <= $2
              AND i.underlying = $3
              AND i.option_type IN ('CE', 'PE')
              AND i.expiry_date = $4
            GROUP BY c.instrument_token
            HAVING COUNT(*) >= $5
            """,
            start,
            end,
            underlying.upper(),
            expiry_date,
            min_bars,
        )
        if rows:
            return [str(r["instrument_token"]) for r in rows]

    rows = await conn.fetch(
        """
        SELECT instrument_token, AVG(close)::float AS avg_close, COUNT(*)::int AS cnt
        FROM candles_1m
        WHERE ts >= $1 AND ts <= $2
        GROUP BY instrument_token
        HAVING COUNT(*) >= $3
        """,
        start,
        end,
        min_bars,
    )
    out: list[str] = []
    for row in rows:
        avg = float(row["avg_close"] or 0)
        if 0 < avg < 5000:
            out.append(str(row["instrument_token"]))
    return out


async def _universe_from_db(
    conn: Any,
    *,
    underlying: str,
    expiry_date: date,
    mid_spot: Decimal,
) -> ContractUniverse | None:
    from algoflat.symbols_util import lot_size_for_tsym

    rows = await conn.fetch(
        """
        SELECT exchange, token, tsym, underlying, expiry_date, strike,
               option_type, lot_size, tick_size, is_atm, in_band
        FROM instruments
        WHERE underlying = $1 AND expiry_date = $2
        ORDER BY strike, option_type
        """,
        underlying.upper(),
        expiry_date,
    )
    if not rows:
        return None
    instruments: list[Instrument] = []
    atm_ce = atm_pe = None
    step = Decimal("50")
    atm_strike = _round_strike(mid_spot, step)
    for row in rows:
        inst = Instrument(
            exchange=row["exchange"],
            token=str(row["token"]),
            tsym=row["tsym"],
            underlying=row["underlying"],
            expiry_date=datetime.combine(row["expiry_date"], time(0), tzinfo=IST),
            strike=Decimal(str(row["strike"])),
            option_type=row["option_type"],
            lot_size=lot_size_for_tsym(
                row["tsym"], metadata_lot_size=int(row["lot_size"] or 0) or None
            ),
            tick_size=Decimal(str(row["tick_size"])) if row["tick_size"] is not None else None,
        )
        instruments.append(inst)
        if row["is_atm"] and row["option_type"] == "CE":
            atm_ce = inst
        if row["is_atm"] and row["option_type"] == "PE":
            atm_pe = inst
    if atm_ce is None:
        atm_ce = next((i for i in instruments if i.option_type == "CE" and i.strike == atm_strike), None)
    if atm_pe is None:
        atm_pe = next((i for i in instruments if i.option_type == "PE" and i.strike == atm_strike), None)
        if atm_pe is None:
            atm_pe = next((i for i in instruments if i.option_type == "PE"), None)
    if atm_ce is None:
        atm_ce = next((i for i in instruments if i.option_type == "CE"), None)
    exp_tag = format_expiry_tag(expiry_date)
    return ContractUniverse(
        spot=mid_spot,
        atm_strike=atm_strike,
        expiry_symbol=exp_tag,
        instruments=instruments,
        atm_ce=atm_ce,
        atm_pe=atm_pe,
    )


async def _stitched_interval_from_db(
    md: MarketDataEngine,
    conn: Any,
    tokens: list[str],
    interval: CandleInterval,
    start: datetime,
    end: datetime,
    *,
    preferred_token: str,
) -> list[Candle]:
    by_token: dict[str, list[Candle]] = {}
    for tok in tokens:
        rows = await md.candles_from_db_for_tokens(conn, [tok], interval, start, end)
        if rows:
            by_token[tok] = rows
    return md.stitch_contract_candles(by_token, preferred_token=preferred_token)


async def _load_session_from_db(
    day: date,
    *,
    underlying: str,
    config: AppConfig,
) -> dict[str, Any] | None:
    """Load session spot + option 1m from Postgres (same tables live engine writes)."""
    from algoflat.db.connection import close_pool, init_pool
    from algoflat.features.indicators import aggregate_from_m5

    ul = underlying.upper()
    start, end = _session_bounds(day)
    cal = next_weekly_expiry_dates(
        underlying=ul, as_of=day, count=1, include_today_if_expiry=True
    )
    if not cal:
        return None
    expiry_d = cal[0]
    expected_exp = format_expiry_tag(expiry_d)

    try:
        pool = await init_pool()
    except Exception as exc:
        print(f"==> DB unavailable ({exc})")
        return None

    try:
        async with pool.acquire() as conn:
            spot_tokens = await _discover_spot_tokens_from_db(conn, start, end)
            if not spot_tokens:
                print(f"==> DB: no futures/index 1m bars for {day.isoformat()}")
                return None

            ranked = await _discover_spot_tokens_ranked(conn, start, end)
            pref = spot_tokens[0]
            bus = EventBus(max_size=1)
            md = MarketDataEngine(config, create_live_broker(config), bus)
            cash_m1: list[Candle] = []
            fut_m1: list[Candle] = []
            fut_tok = pref
            if len(ranked) >= 2:
                cash_tok, cash_avg, _ = ranked[0]
                fut_candidates = [
                    r for r in ranked[1:] if r[1] > cash_avg * 1.003
                ]
                if not fut_candidates:
                    fut_candidates = list(ranked[1:])
                # Nearest-month future (lowest premium above cash), not far month.
                fut_tok, fut_avg, _ = min(
                    fut_candidates,
                    key=lambda x: (x[1], -x[2]),
                )
                cash_m1 = await _m1_series_for_token(md, conn, cash_tok, start, end)
                fut_m1 = await _m1_series_for_token(md, conn, fut_tok, start, end)
                if len(fut_m1) < len(cash_m1):
                    fut_m1 = _extend_fut_from_cash(
                        cash_m1, fut_m1, fut_token=fut_tok
                    )
                    print(
                        f"  DB: extended fut {fut_tok} to {len(fut_m1)} bars "
                        f"(cash={len(cash_m1)})"
                    )
                pref = fut_tok
                print(
                    f"  DB: dual spot cash={cash_tok} (~{cash_avg:.0f}) "
                    f"fut={fut_tok} (~{fut_avg:.0f})"
                )
            chart_tokens = await md.chart_tokens_from_db(
                conn, seed_tokens=spot_tokens, start=start, ref_token=pref
            )
            m1_all = await _stitched_interval_from_db(
                md, conn, chart_tokens, CandleInterval.M1, start, end, preferred_token=pref
            )
            if len(m1_all) < 30:
                print(f"==> DB: only {len(m1_all)} spot 1m bars for {day.isoformat()}")
                return None

            m3_all = await _stitched_interval_from_db(
                md, conn, chart_tokens, CandleInterval.M3, start, end, preferred_token=pref
            )
            m5_all = await _stitched_interval_from_db(
                md, conn, chart_tokens, CandleInterval.M5, start, end, preferred_token=pref
            )
            m15_rows = await md.candles_from_db_for_tokens(
                conn, chart_tokens, CandleInterval.M15, start, end
            )
            m15_all = md.stitch_contract_candles(
                {pref: m15_rows} if m15_rows else {},
                preferred_token=pref,
            )
            if not m15_all and m5_all:
                m15_all = aggregate_from_m5(m5_all, 15)

            all_candles: dict[CandleInterval, list[Candle]] = {
                CandleInterval.M1: m1_all,
                CandleInterval.M3: m3_all,
                CandleInterval.M5: m5_all,
            }
            if m15_all:
                all_candles[CandleInterval.M15] = m15_all

            if cash_m1 and fut_m1:
                m1_all = _session_driver_m1(m1_all, cash_m1, fut_m1)
                all_candles[CandleInterval.M1] = m1_all

            mid_spot = m1_all[len(m1_all) // 2].close
            universe = await _universe_from_db(
                conn, underlying=ul, expiry_date=expiry_d, mid_spot=mid_spot
            )
            if universe is None or not universe.instruments:
                snap_uni = load_day_snap(day, ul)
                if snap_uni and snap_uni["universe"].instruments:
                    universe = _repair_empty_universe(snap_uni["universe"], day, ul)
                    print(f"  DB: reused universe from snap ({len(universe.instruments)} instruments)")
                else:
                    universe = _synthetic_universe(mid_spot, day, config, underlying=ul)
                    print(f"  DB: synthetic universe ({len(universe.instruments)} instruments)")

            opt_tokens = await _discover_option_tokens_from_db(
                conn, start, end, underlying=ul, expiry_date=expiry_d
            )
            option_series: dict[str, list[Candle]] = {}
            for tok in opt_tokens:
                rows = await md.candles_from_db_for_tokens(
                    conn, [tok], CandleInterval.M1, start, end
                )
                if rows:
                    option_series[tok] = rows

            if not option_series:
                file_snap = load_day_snap(day, ul)
                if file_snap and file_snap.get("option_series"):
                    option_series = file_snap["option_series"]
                    print(
                        f"  DB: merged {len(option_series)} option series from day snap"
                    )
                if not option_series:
                    probe = day - timedelta(days=1)
                    for _ in range(8):
                        while probe.weekday() >= 5:
                            probe -= timedelta(days=1)
                        prior_snap = load_day_snap(probe, ul)
                        if prior_snap and prior_snap.get("option_series"):
                            exp = (prior_snap["universe"].expiry_symbol or "").upper()
                            if not universe.expiry_symbol or exp == (
                                universe.expiry_symbol or ""
                            ).upper():
                                option_series = prior_snap["option_series"]
                                print(
                                    f"  DB: merged {len(option_series)} option series "
                                    f"from {probe.isoformat()} snap (same expiry)"
                                )
                                break
                        probe -= timedelta(days=1)
                if file_snap and len(universe.instruments) < 10:
                    if file_snap["universe"].instruments:
                        universe = _repair_empty_universe(
                            file_snap["universe"], day, ul
                        )

            prior_day = day - timedelta(days=1)
            while prior_day.weekday() >= 5:
                prior_day -= timedelta(days=1)
            prior_m5: list[Candle] = []
            p_start, p_end = _session_bounds(prior_day)
            prior_spot = await _discover_spot_tokens_from_db(conn, p_start, p_end)
            if prior_spot:
                prior_m5 = await _stitched_interval_from_db(
                    md,
                    conn,
                    prior_spot,
                    CandleInterval.M5,
                    p_start,
                    p_end,
                    preferred_token=prior_spot[0],
                )
            if len(prior_m5) < 5:
                prior_snap = load_day_snap(prior_day, ul)
                if prior_snap:
                    prior_m5 = prior_snap.get("prior_m5") or prior_snap["all_candles"].get(
                        CandleInterval.M5, []
                    )
                else:
                    prior_day = None

            print(
                f"==> DB session loaded for {day.isoformat()} "
                f"(spot 1m={len(m1_all)} options={len(option_series)} "
                f"expiry={universe.expiry_symbol or expected_exp})"
            )
            return {
                "all_candles": all_candles,
                "cash_m1": cash_m1 or m1_all,
                "fut_m1": fut_m1 or m1_all,
                "prior_day": prior_day,
                "prior_m5": prior_m5,
                "universe": universe,
                "option_series": option_series,
                "meta": {
                    "source": "postgres",
                    "expected_expiry": expected_exp,
                    "spot_tokens": chart_tokens,
                    "option_tokens": len(option_series),
                    "dual_spot": bool(cash_m1 and fut_m1 and cash_m1 is not fut_m1),
                },
            }
    finally:
        await close_pool()


async def _persist_session_to_db(
    *,
    day: date,
    all_candles: dict[CandleInterval, list[Candle]],
    option_series: dict[str, list[Candle]],
    spot_token: str,
    cash_m1: list[Candle] | None = None,
    fut_token: str | None = None,
    universe: ContractUniverse | None = None,
) -> None:
    """Write fetched session candles into Postgres (idempotent upsert via journal writer)."""
    from algoflat.db.connection import close_pool, init_pool, get_pool
    from algoflat.contract_selector.selector import ContractSelector
    from algoflat.journal.writer import JournalWriter

    try:
        await init_pool()
    except Exception as exc:
        print(f"  WARN: could not persist to DB ({exc})")
        return
    writer = JournalWriter()
    try:
        for interval, bars in all_candles.items():
            tagged = []
            for c in bars:
                c.instrument_token = spot_token
                c.interval = interval
                tagged.append(c)
            if tagged:
                await writer.write_candles(tagged)
        if cash_m1:
            cash_tagged = []
            cash_token = "upstox_nifty_index"
            for c in cash_m1:
                cash_tagged.append(
                    c.model_copy(
                        update={
                            "instrument_token": cash_token,
                            "interval": CandleInterval.M1,
                        }
                    )
                )
            await writer.write_candles(cash_tagged)
        if fut_token and spot_token != fut_token:
            # all_candles already tagged with spot_token (= fut); ensure token set
            pass
        for token, bars in option_series.items():
            for c in bars:
                c.instrument_token = token
            if bars:
                await writer.write_candles(bars)
        if universe and universe.instruments:
            selector = ContractSelector(get_config(), create_live_broker(get_config()))
            await selector.persist_instruments(get_pool(), universe)
        print(
            f"  Persisted session candles to DB for {day.isoformat()} "
            f"(spot={spot_token} options={len(option_series)} "
            f"cash_m1={len(cash_m1 or [])})"
        )
    except Exception as exc:
        print(f"  WARN: DB persist failed ({exc})")
    finally:
        await close_pool()


async def _supplement_from_upstox(
    day: date,
    *,
    underlying: str,
    snap: dict[str, Any],
) -> dict[str, Any]:
    """Merge Upstox index/options into an existing session payload when gaps exist."""
    from algoflat.broker.upstox_data import fetch_session_from_upstox

    m1 = (snap.get("all_candles") or {}).get(CandleInterval.M1) or []
    opts = snap.get("option_series") or {}
    need_spot = not m1 or len(m1) < 30
    need_opts = len(opts) < 4
    if not need_spot and not need_opts:
        return snap

    print("==> Supplementing session from Upstox (missing spot and/or options)...")
    try:
        upx = await asyncio.to_thread(
            fetch_session_from_upstox, day, underlying=underlying
        )
    except Exception as exc:
        print(f"  Upstox supplement failed: {exc}")
        return snap
    if not upx:
        return snap

    if need_spot and upx.get("all_candles"):
        snap["all_candles"] = upx["all_candles"]
        if upx.get("cash_m1"):
            snap["cash_m1"] = upx["cash_m1"]
        if upx.get("fut_m1"):
            snap["fut_m1"] = upx["fut_m1"]
        meta = snap.setdefault("meta", {})
        meta["source"] = "upstox"
        meta["spot_from"] = "upstox_index"
        if upx.get("meta"):
            meta.update(
                {
                    k: v
                    for k, v in upx["meta"].items()
                    if k in ("fut_token", "cash_token", "spot_tokens", "dual_spot")
                }
            )
    if need_opts and upx.get("option_series"):
        snap["option_series"] = upx["option_series"]
        if upx["universe"].instruments:
            snap["universe"] = upx["universe"]
        meta = snap.setdefault("meta", {})
        meta["options_from"] = "upstox"
    if upx.get("prior_m5") and not snap.get("prior_m5"):
        snap["prior_m5"] = upx["prior_m5"]
        snap["prior_day"] = upx.get("prior_day")
    return await _supplement_from_nse(day, underlying=underlying, snap=snap)


async def _supplement_from_nse(
    day: date,
    *,
    underlying: str,
    snap: dict[str, Any],
) -> dict[str, Any]:
    """NSE bhavcopy universe + BS option 1m when broker history is missing."""
    from algoflat.broker.nse_data import supplement_session_from_nse

    opts = snap.get("option_series") or {}
    universe = snap.get("universe")
    insts = list(getattr(universe, "instruments", None) or [])
    syn_only = not insts or all(
        str(i.token).startswith(("syn_", "nse_")) for i in insts
    )
    if len(opts) >= 4 and not syn_only:
        return snap
    try:
        return await asyncio.to_thread(
            supplement_session_from_nse, snap, day, underlying=underlying
        )
    except Exception as exc:
        print(f"  NSE supplement failed: {exc}")
        return snap


async def _fetch_session_with_fallback(
    day: date,
    *,
    underlying: str,
    config: AppConfig,
    prefer_upstox: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """Return (session_payload, spot_token_for_db) from Upstox and/or Flattrade."""
    from algoflat.broker.upstox_data import fetch_session_from_upstox

    ul = underlying.upper()

    if prefer_upstox:
        print(f"==> Fetching {ul} session from Upstox for {day.isoformat()}...")
        upx = await asyncio.to_thread(
            fetch_session_from_upstox, day, underlying=ul
        )
        if upx and len((upx.get("all_candles") or {}).get(CandleInterval.M1) or []) >= 30:
            meta = upx.get("meta") or {}
            spot_tok = str(meta.get("fut_token") or "upstox_nifty_index")
            return upx, spot_tok
        print("  Upstox fetch incomplete — trying Flattrade...")

    broker: BrokerAdapter | None = None
    try:
        print("==> Ensuring Flattrade session...")
        await ensure_session(FlattradeConfig.from_env())
        broker = create_live_broker(config)
        await broker.connect()

        from algoflat.contract_selector.expiry import (
            format_expiry_tag,
            next_weekly_expiry_dates,
        )

        expected_exp = format_expiry_tag(
            next_weekly_expiry_dates(
                underlying=ul, as_of=day, count=1, include_today_if_expiry=True
            )[0]
        )
        print(
            f"==> Fetching {ul} session candles from Flattrade for {day.isoformat()} "
            f"(expected weekly expiry {expected_exp})..."
        )
        all_candles, spot_token = await _fetch_session_candles(broker, config, day)
        m1_all = all_candles[CandleInterval.M1]
        if len(m1_all) < 30:
            raise RuntimeError(f"only {len(m1_all)} spot 1m bars from Flattrade")

        mid_spot = m1_all[len(m1_all) // 2].close
        step = Decimal(str(config.symbols["strike_step"]))
        selector = ContractSelector(config, broker)
        universe = await selector.build_universe(mid_spot, as_of=day)
        if not universe.instruments:
            cal = next_weekly_expiry_dates(
                underlying=ul, as_of=day, count=1, include_today_if_expiry=True
            )
            primary_exp = format_expiry_tag(cal[0]) if cal else None
            prior_uni = _prior_snap_universe(day, ul)
            if prior_uni and (
                primary_exp is None or prior_uni.expiry_symbol == primary_exp
            ):
                universe = selector.retarget_atm(prior_uni, mid_spot)
        if not universe.instruments:
            universe = _synthetic_universe(mid_spot, day, config, underlying=ul)
        elif universe.expiry_symbol and universe.expiry_symbol != expected_exp:
            universe = _synthetic_universe(mid_spot, day, config, underlying=ul)

        open_atm = _round_strike(m1_all[0].close, step)
        close_atm = _round_strike(m1_all[-1].close, step)
        lo = min(open_atm, close_atm) - Decimal("150")
        hi = max(open_atm, close_atm) + Decimal("150")
        strikes: list[Decimal] = []
        s = lo
        while s <= hi:
            strikes.append(s)
            s += step

        session_start = m1_all[0].ts
        session_end = m1_all[-1].ts
        real_insts = [
            i for i in universe.instruments if not str(i.token).startswith("syn_")
        ]
        option_series: dict[str, list[Candle]] = {}
        if real_insts:
            uni_for_opts = ContractUniverse(
                spot=universe.spot,
                atm_strike=universe.atm_strike,
                expiry_symbol=universe.expiry_symbol,
                instruments=real_insts,
                atm_ce=universe.atm_ce,
                atm_pe=universe.atm_pe,
            )
            option_series = await _load_option_series(
                broker, uni_for_opts, strikes, session_start, session_end
            )

        prior_day = day - timedelta(days=1)
        while prior_day.weekday() >= 5:
            prior_day = prior_day - timedelta(days=1)
        prior_m5: list[Candle] = []
        try:
            prev_candles, _ = await _fetch_session_candles(broker, config, prior_day)
            prior_m5 = prev_candles.get(CandleInterval.M5) or []
        except Exception as exc:
            print(f"  WARN: prior day fetch failed: {exc}")
            prior_day = None

        payload = {
            "all_candles": all_candles,
            "prior_day": prior_day,
            "prior_m5": prior_m5,
            "universe": universe,
            "option_series": option_series,
            "meta": {"source": "flattrade", "expected_expiry": expected_exp},
        }
        if len(option_series) < 4:
            payload = await _supplement_from_upstox(day, underlying=ul, snap=payload)
        await broker.disconnect()
        return payload, spot_token
    except Exception as exc:
        print(f"  Flattrade session fetch failed: {exc}")
        if broker is not None:
            await broker.disconnect()
        print(f"==> Falling back to Upstox for {day.isoformat()}...")
        upx = await asyncio.to_thread(
            fetch_session_from_upstox, day, underlying=ul
        )
        if upx and len((upx.get("all_candles") or {}).get(CandleInterval.M1) or []) >= 30:
            meta = upx.get("meta") or {}
            spot_tok = str(meta.get("fut_token") or "upstox_nifty_index")
            return upx, spot_tok
        return None, ""


async def _resolve_session_spot_source(
    broker: BrokerAdapter,
    config: AppConfig,
    day: date,
) -> tuple[str, str, str]:
    """Pick spot candle source for a historical session (futures preferred, NSE index fallback)."""
    from algoflat.contract_selector.scripmaster import nearest_future_from_scripmaster
    from algoflat.symbols_util import index_spot_exchange

    sym = config.symbols
    ul = str(sym.get("underlying", "NIFTY")).upper()
    fut_exchange = str(sym.get("exchange_spot", "NFO"))
    idx_exchange = index_spot_exchange(sym)
    idx_token = str(sym.get("index_spot_token", "26000")).strip()

    start = datetime.combine(day, time(9, 15), tzinfo=IST).astimezone(timezone.utc)
    end = datetime.combine(day, time(15, 30), tzinfo=IST).astimezone(timezone.utc)
    now = datetime.now(tz=timezone.utc)
    if end > now:
        end = now

    fut = nearest_future_from_scripmaster(
        underlying=ul, exchange=fut_exchange, as_of=day
    )
    if fut:
        token, tsym = fut
        probe = await broker.get_candles(
            fut_exchange, token, CandleInterval.M1, start, end
        )
        if len(probe) >= 30:
            print(f"  Spot source: {fut_exchange} futures {tsym} ({len(probe)} x 1m probe)")
            return fut_exchange, token, f"futures:{tsym}"

    if idx_token:
        probe = await broker.get_candles(
            idx_exchange, idx_token, CandleInterval.M1, start, end
        )
        if len(probe) >= 30:
            print(
                f"  Spot source: {idx_exchange} index token={idx_token} "
                f"({len(probe)} x 1m probe)"
            )
            return idx_exchange, idx_token, f"index:{idx_token}"

    # Last resort: configured live token (today's future).
    live_token = str(sym.get("spot_token", "")).strip()
    if live_token:
        probe = await broker.get_candles(
            fut_exchange, live_token, CandleInterval.M1, start, end
        )
        if len(probe) >= 30:
            print(f"  Spot source: configured token={live_token} ({len(probe)} x 1m)")
            return fut_exchange, live_token, f"configured:{live_token}"

    raise RuntimeError(f"No spot candle source for {day.isoformat()}")


async def _fetch_session_candles(
    broker: BrokerAdapter,
    config: AppConfig,
    day: date,
) -> tuple[dict[CandleInterval, list[Candle]], str]:
    start = (
        datetime.combine(day, time(9, 15), tzinfo=IST).astimezone(timezone.utc)
    )
    end = (
        datetime.combine(day, time(15, 30), tzinfo=IST).astimezone(timezone.utc)
    )
    now = datetime.now(tz=timezone.utc)
    if end > now:
        end = now
    exchange, token, label = await _resolve_session_spot_source(broker, config, day)
    out: dict[CandleInterval, list[Candle]] = {}
    for interval in CandleInterval.broker_native():
        rows = await broker.get_candles(exchange, token, interval, start, end)
        rows = sorted(rows, key=lambda c: c.ts)
        for c in rows:
            c.instrument_token = token
        out[interval] = rows
        print(f"  {interval.value} [{label}]: {len(rows)} bars", end="")
        if rows:
            print(
                f"  [{rows[0].ts.astimezone(IST).strftime('%H:%M')} → "
                f"{rows[-1].ts.astimezone(IST).strftime('%H:%M')}] "
                f"close={rows[-1].close}"
            )
        else:
            print()
    return out, token


async def _load_option_series(
    broker: BrokerAdapter,
    universe: ContractUniverse,
    strikes: list[Decimal],
    session_start: datetime,
    session_end: datetime,
) -> dict[str, list[Candle]]:
    """Fetch 1m candles for ATM± band CE/PE tokens present in universe."""
    series: dict[str, list[Candle]] = {}
    wanted = set(strikes)
    targets = [
        i
        for i in universe.instruments
        if i.strike in wanted and i.option_type in ("CE", "PE")
    ]
    print(f"  Fetching option 1m candles for {len(targets)} contracts...")
    for i, inst in enumerate(targets, 1):
        try:
            rows = await broker.get_candles(
                inst.exchange,
                inst.token,
                CandleInterval.M1,
                session_start,
                session_end,
            )
            rows = sorted(rows, key=lambda c: c.ts)
            if rows:
                series[inst.token] = rows
            if i % 5 == 0 or i == len(targets):
                print(f"    {i}/{len(targets)} done ({len(series)} with data)")
            await asyncio.sleep(0.15)  # gentle rate limit
        except Exception as exc:  # noqa: BLE001
            print(f"    skip {inst.tsym}: {exc}")
    return series


def _find_inst(universe: ContractUniverse, side: str, strike: Decimal) -> Instrument | None:
    for i in universe.instruments:
        if i.option_type == side and i.strike == strike:
            return i
    if side == "CE":
        return universe.atm_ce
    return universe.atm_pe


async def run_backtest(
    day: date | None = None,
    *,
    underlying: str = "NIFTY",
    exit_interval: int | None = None,
    scan_interval: int | None = None,
    from_snap: bool = False,
    from_db: bool = False,
    prefer_upstox: bool = False,
    refresh_snap: bool = False,
    snap_only: bool = False,
    write_db_on_fetch: bool = True,
    capital_pool: dict[str, Decimal] | None = None,
    honest_replay: bool = True,
    live_parity: bool = False,
    skip_option_m1: bool = False,
    decision_log: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if live_parity:
        honest_replay = False
    day = day or datetime.now(tz=IST).date()
    config = get_config()
    ul = underlying.upper()
    from algoflat.symbols_util import (
        apply_active_underlying,
        capital_for,
        total_account_capital,
        uses_pooled_capital,
    )

    apply_active_underlying(config, ul)
    broker: BrokerAdapter | None = None
    broker_connected = False

    snap = None if refresh_snap else load_day_snap(day, ul)
    if from_snap:
        if not snap:
            print(f"ERROR: --from-snap but no snap at {_snap_dir(day, ul)}")
            return None
        use_snap = True
    elif from_db and not refresh_snap:
        snap = await _load_session_from_db(day, underlying=ul, config=config)
        if snap is not None:
            snap = await _supplement_from_upstox(day, underlying=ul, snap=snap)
            use_snap = True
        else:
            print("==> DB insufficient — falling back to broker/Upstox fetch...")
            use_snap = False
    elif refresh_snap or snap is None:
        use_snap = False
    else:
        use_snap = True

    if use_snap and snap is not None:
        src = (snap.get("meta") or {}).get("source", "snap")
        print(f"==> Using {src} session data for {day.isoformat()} (1m bars + option series)...")
        print(
            "  NOTE: Flattrade has no historical ticks — snap is finest available "
            "(1m OHLC + intvwap/oi). Replay uses O→VWAP/mid→C path, not fake H/L thrash."
        )
        all_candles = snap["all_candles"]
        m1_all = all_candles[CandleInterval.M1]
        universe = _ensure_universe(
            snap["universe"],
            day,
            m1_all[len(m1_all) // 2].close,
            config,
            underlying=ul,
        )
        option_series = snap["option_series"]
        prior_day = snap["prior_day"]
        prior_m5 = snap["prior_m5"]
        m3_all = all_candles[CandleInterval.M3]
        m5_all = all_candles[CandleInterval.M5]
        cash_m1_all = snap.get("cash_m1") or m1_all
        fut_m1_all = snap.get("fut_m1") or m1_all
        m1_all = _session_driver_m1(m1_all, cash_m1_all, fut_m1_all)
        if len(m1_all) < 30:
            print(f"ERROR: snap has too few 1m candles for {day} — aborting")
            return None
        if len(option_series) < 4:
            snap = await _supplement_from_upstox(
                day,
                underlying=ul,
                snap={
                    "all_candles": all_candles,
                    "universe": universe,
                    "option_series": option_series,
                    "prior_day": prior_day,
                    "prior_m5": prior_m5,
                    "meta": snap.get("meta", {}),
                },
            )
            all_candles = snap["all_candles"]
            universe = snap["universe"]
            option_series = snap["option_series"]
            prior_day = snap.get("prior_day")
            prior_m5 = snap.get("prior_m5") or []
            m1_all = all_candles[CandleInterval.M1]
            m3_all = all_candles[CandleInterval.M3]
            m5_all = all_candles[CandleInterval.M5]
        if snap_only:
            meta = snap.get("meta") or {}
            spot_tok = str(
                meta.get("fut_token")
                or ((meta.get("spot_tokens") or ["upstox_nifty_index"])[0])
            )
            save_day_snap(
                day,
                underlying=ul,
                all_candles=all_candles,
                prior_day=prior_day,
                prior_m5=prior_m5,
                universe=universe,
                option_series=option_series,
                cash_m1=snap.get("cash_m1"),
                fut_m1=snap.get("fut_m1"),
            )
            if write_db_on_fetch and (from_db or prefer_upstox):
                await _persist_session_to_db(
                    day=day,
                    all_candles=all_candles,
                    option_series=option_series,
                    spot_token=str(spot_tok),
                    cash_m1=snap.get("cash_m1"),
                    fut_token=str(meta.get("fut_token") or spot_tok),
                    universe=universe,
                )
            print("==> --snap-only: snap persisted, skipping replay.")
            return None
    else:
        fetched, spot_token = await _fetch_session_with_fallback(
            day,
            underlying=ul,
            config=config,
            prefer_upstox=prefer_upstox,
        )
        if fetched is None:
            print(f"ERROR: could not load session data for {day} (Flattrade + Upstox)")
            return None
        all_candles = fetched["all_candles"]
        universe = fetched["universe"]
        option_series = fetched["option_series"]
        prior_day = fetched.get("prior_day")
        prior_m5 = fetched.get("prior_m5") or []
        m1_all = all_candles[CandleInterval.M1]
        m3_all = all_candles[CandleInterval.M3]
        m5_all = all_candles[CandleInterval.M5]
        cash_m1_all = fetched.get("cash_m1") or m1_all
        fut_m1_all = fetched.get("fut_m1") or m1_all
        m1_all = _session_driver_m1(m1_all, cash_m1_all, fut_m1_all)
        universe = _ensure_universe(
            universe,
            day,
            m1_all[len(m1_all) // 2].close,
            config,
            underlying=ul,
        )
        open_spot = m1_all[0].close
        close_spot = m1_all[-1].close
        print(f"\n==> Spot range: {open_spot} → {close_spot}  (Δ {close_spot - open_spot})")
        print(
            f"  expiry={universe.expiry_symbol} atm={universe.atm_strike} "
            f"instruments={len(universe.instruments)}  options={len(option_series)}"
        )
        save_day_snap(
            day,
            underlying=ul,
            all_candles=all_candles,
            prior_day=prior_day,
            prior_m5=prior_m5,
            universe=universe,
            option_series=option_series,
            cash_m1=fetched.get("cash_m1"),
            fut_m1=fetched.get("fut_m1"),
        )
        if (from_db or prefer_upstox) and write_db_on_fetch and spot_token:
            meta = fetched.get("meta") or {}
            await _persist_session_to_db(
                day=day,
                all_candles=all_candles,
                option_series=option_series,
                spot_token=spot_token,
                cash_m1=fetched.get("cash_m1"),
                fut_token=str(meta.get("fut_token") or spot_token),
                universe=universe,
            )
        if snap_only:
            print("==> --snap-only: fetch complete, skipping replay.")
            return None

    universe = _normalize_universe_lot_sizes(universe, underlying=ul)
    lot_size = LOT_FALLBACK
    if universe.atm_pe:
        lot_size = universe.atm_pe.lot_size
    elif universe.atm_ce:
        lot_size = universe.atm_ce.lot_size

    open_spot = m1_all[0].close
    close_spot = m1_all[-1].close
    step = Decimal(str(config.symbols["strike_step"]))
    if use_snap:
        print(f"\n==> Spot range: {open_spot} → {close_spot}  (Δ {close_spot - open_spot})")
        print(
            f"  expiry={universe.expiry_symbol} atm={universe.atm_strike} "
            f"instruments={len(universe.instruments)}  options={len(option_series)}"
        )

    # Expiry date for BS fallback
    expiry_d = day
    if universe.atm_ce and universe.atm_ce.expiry_date:
        expiry_d = universe.atm_ce.expiry_date.date()
    elif universe.expiry_symbol:
        from algoflat.contract_selector.expiry import parse_expiry_tag

        parsed = parse_expiry_tag(universe.expiry_symbol)
        if parsed:
            expiry_d = parsed

    # Replay engines
    bus = EventBus(max_size=10)
    # Broker only needed for MarketDataEngine construction; snap replay never calls it.
    if broker is None:
        broker = create_live_broker(config)
    md = MarketDataEngine(config, broker, bus)
    features_eng = FeatureEngine(config, md)
    if prior_m5:
        features_eng.set_prior_day(
            max(c.high for c in prior_m5),
            min(c.low for c in prior_m5),
            prior_m5[-1].close,
        )
        print(
            f"  Prior day {prior_day}: PDH={features_eng.prior_high} "
            f"PDL={features_eng.prior_low} close={features_eng.prior_close}"
        )
    else:
        print("  WARN: prior day levels unavailable")

    regime_clf = RegimeClassifier(config)
    quality = QualityGate(config)
    # Same learner file as live so demotion multipliers match.
    learner = StrategyLearner.from_config(
        ROOT / "reports" / "strategy_learner.json", config.strategy
    )
    learner.begin_session(day)
    quality.set_learner(learner)
    scanners = build_strategy_scanners(config)
    router = StrategyRouter(config, scanners, quality)
    validator = RuleValidator(config)
    validator.reset_session()
    entry_confirm = EntryConfirmationTracker.from_config(
        config.strategy.get("entry_confirmation") or {}
    )
    pb_cfg = config.validator.get("premium_responsive") or {}
    premium_tracker = PremiumBehaviourTracker(max_samples=int(pb_cfg.get("max_samples", 12)))
    print(f"  Strategies enabled: {len(scanners)} (same library as live)")
    print("  Layers: FeatureEngine → Regime → Router/Quality §9 → RuleValidator → sizing → exit_rules")
    exit_cfg = config.position_exit
    # Match PositionManager: prefer seconds; fall back to minutes*60.
    if "cooldown_after_exit_seconds" in config.risk:
        cooldown_sec = max(0, int(config.risk["cooldown_after_exit_seconds"]))
    else:
        cooldown_sec = max(0, int(config.risk.get("cooldown_after_exit_minutes", 3))) * 60
    entry_start = time.fromisoformat(str(config.validator.get("entry_start_time", "10:00")))
    entry_end = time.fromisoformat(str(config.validator.get("entry_end_time", "15:15")))
    force_exit_t = time.fromisoformat(str(config.risk.get("force_exit_time", "15:15")))
    max_concurrent = int(config.risk.get("max_concurrent_positions_per_index", 0))
    max_daily_loss = Decimal(str(config.risk.get("max_daily_loss", 0)))
    max_consec = int(config.risk.get("max_consecutive_losses", 0))
    max_trades_day = int(config.risk.get("max_trades_per_day", 0))
    scan_sec = int(
        scan_interval
        if scan_interval is not None
        else config.runtime.get("scan_interval_seconds", 30)
    )
    # Live marks exits on every quote (~2s REST poll / WS ticks). Coarse 30s-only
    # exits under-count re-entries vs a real session.
    exit_sec = int(
        exit_interval
        if exit_interval is not None
        else config.runtime.get(
            "backtest_exit_interval_seconds",
            config.runtime.get("rest_quote_poll_interval_market_seconds", 2),
        )
    )
    exit_sec = max(1, min(exit_sec, scan_sec))

    if capital_pool is not None:
        starting = capital_pool["starting"]
        available = capital_pool["available"]
        deployed = capital_pool["deployed"]
        realized = capital_pool["realized"]
    elif uses_pooled_capital(config):
        starting = total_account_capital(config)
        available = starting
        deployed = Decimal("0")
        realized = Decimal("0")
    else:
        starting = capital_for(config, ul)
        available = starting
        deployed = Decimal("0")
        realized = Decimal("0")
    pool_start_equity = starting + realized
    peak_deployed = Decimal("0")
    peak_util_pct = Decimal("0")

    stats = DayStats()
    open_trades: list[OpenTrade] = []
    cooldown_until: dict[str, datetime] = {}
    global_cooldown_until: datetime | None = None
    pending_flips: list[dict] = []
    consecutive_losses = 0
    prior_oi: dict[str, int] = {}
    option_vwap_num: dict[str, Decimal] = {}
    option_vwap_den: dict[str, Decimal] = {}
    strike_cfg = config.strategy.get("strike_selection") or {}
    strike_band = effective_quote_band_steps(strike_cfg)
    atm_band = int(strike_cfg.get("atm_band_steps", 1))
    strike_modes = list(strike_cfg.get("modes") or ["atm"])
    otm_steps = [int(s) for s in (strike_cfg.get("otm_steps") or [])]
    flip_on_reversal = bool(exit_cfg.get("flip_on_trend_reversal", True))

    mi_engine = MarketIntelligenceEngine(config)
    index_opens = {
        "NIFTY": (cash_m1_all[0].close if cash_m1_all else m1_all[0].close),
        "NIFTY_FUT": (fut_m1_all[0].close if fut_m1_all else m1_all[0].close),
    }

    print(
        f"\n==> Replaying {ul} session "
        "(LIVE-PARITY layers · exit_rules · RuleValidator · §9 quality · library)..."
    )
    print(
        f"  capital=₹{starting}"
        + (" (shared pool)" if capital_pool is not None or uses_pooled_capital(config) else "")
        + f"  deploy_cap={config.risk.get('max_deployed_pct_of_equity')}%  "
        f"per_trade_cap={config.risk.get('max_premium_pct_of_available')}%  "
        f"window={entry_start.strftime('%H:%M')}–{entry_end.strftime('%H:%M')} IST  "
        f"force_exit={force_exit_t.strftime('%H:%M')}  "
        f"exit_tick={exit_sec}s  entry_scan={scan_sec}s  strike_band=ATM±{strike_band}  "
        f"flip_on_reversal={flip_on_reversal}  "
        f"concurrent={max_concurrent or 'unlimited'}  "
        f"max_trades/day={max_trades_day or '∞'}  "
        f"daily_loss_cap=₹{max_daily_loss}  consec_loss_cap={max_consec or '∞'}  "
        f"cooldown={cooldown_sec}s global  "
        f"data={'snap' if use_snap else 'live-fetch→snap'}  "
        f"replay={'honest (no future bar peek)' if honest_replay else 'intra-bar synthetic'}"
        + (f"  mode=LIVE-PARITY (match live session)" if live_parity else "")
        + ("  option_m1=SKIP" if skip_option_m1 else "  option_m1=ON")
    )
    print(
        f"  lot_size={lot_size} qty/lot (NIFTY: 1 lot = {LOT_FALLBACK} units; "
        f"deploy = premium/unit × {LOT_FALLBACK} × lots)"
    )

    def _validator_option_m1(token: str | None) -> list[Candle] | None:
        """Option 1m for chart vetoes. Use --skip-option-m1 to mute (old optimistic parity)."""
        if skip_option_m1:
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
            honest=honest_replay,
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
                vol = None
                oi = None
                if inst.token in option_series:
                    for c in option_series[inst.token]:
                        if c.ts <= when:
                            vol = c.volume
                            if c.oi is not None:
                                oi = c.oi
                        else:
                            break
                bid, ask, spread_pct = _option_bid_ask_spread(
                    prem,
                    token=inst.token,
                    when=when,
                    option_series=option_series,
                )
                states[inst.token] = OptionState(
                    instrument_token=inst.token,
                    tsym=inst.tsym,
                    ltp=prem,
                    bid=bid,
                    ask=ask,
                    spread_pct=spread_pct,
                    volume=vol,
                    oi=oi,
                )
                # Running option VWAP (equal-weight ticks like live OptionDataLayer)
                option_vwap_num[inst.token] = option_vwap_num.get(
                    inst.token, Decimal("0")
                ) + prem
                option_vwap_den[inst.token] = option_vwap_den.get(
                    inst.token, Decimal("0")
                ) + Decimal("1")
        return states

    def _try_enter(
        *,
        strategy: str,
        side: str,
        strike: Decimal,
        inst: Instrument | None,
        conf: int,
        when: datetime,
        spot: Decimal,
        bar_frac: Decimal,
        ts_ist: datetime,
        equity: Decimal,
        bypass_cooldown: bool = False,
        strike_pick: dict | None = None,
        bar: Candle | None = None,
    ) -> bool:
        nonlocal available, deployed
        if max_trades_day > 0 and (len(stats.closed) + len(open_trades)) >= max_trades_day:
            stats.skips["max_trades_per_day"] = stats.skips.get("max_trades_per_day", 0) + 1
            return False
        if max_consec > 0 and consecutive_losses >= max_consec:
            stats.skips["max_consecutive_losses"] = stats.skips.get(
                "max_consecutive_losses", 0
            ) + 1
            return False
        # Flips must fire immediately — global cooldown would kill opposite entry.
        if (
            not bypass_cooldown
            and global_cooldown_until is not None
            and when < global_cooldown_until
        ):
            stats.skips["global_cooldown"] = stats.skips.get("global_cooldown", 0) + 1
            return False
        token = inst.token if inst else ""
        if (
            not bypass_cooldown
            and token
            and token in cooldown_until
            and when < cooldown_until[token]
        ):
            stats.skips["instrument_cooldown"] = stats.skips.get("instrument_cooldown", 0) + 1
            return False
        if any(o.token == token and o.side == side for o in open_trades if token):
            stats.skips["already_open_same_token"] = stats.skips.get(
                "already_open_same_token", 0
            ) + 1
            return False
        if max_concurrent > 0 and len(open_trades) >= max_concurrent:
            stats.skips["max_concurrent"] = stats.skips.get("max_concurrent", 0) + 1
            return False

        prem, src = _mark_premium(
            when=when,
            spot=spot,
            side=side,
            strike=strike,
            token=inst.token if inst else None,
            bar_frac=bar_frac,
            bar=bar or m1_all[0],
        )
        ls = _resolve_lot_size(
            inst,
            tsym=inst.tsym if inst else "",
            fallback=lot_size,
        )
        lots, premium_cost = _fit_lots(
            risk_cfg=config.risk,
            confidence=conf,
            entry_ltp=prem,
            lot_size=ls,
            available=available,
            deployed=deployed,
            equity=equity,
            strike_pick=strike_pick,
        )
        if lots < 1:
            stats.skips["capital_or_deploy_cap"] = stats.skips.get(
                "capital_or_deploy_cap", 0
            ) + 1
            return False

        greeks_mult = Decimal("1")
        if strike_pick:
            stub = CandidateSignal(
                ts=when,
                setup_type=strategy,
                side=side,
                instrument_token=token or "",
                tsym=inst.tsym if inst else "",
                strategy_version="backtest",
                feature_snapshot=FeatureSnapshot(ts=when, nifty_spot=spot),
                confidence=conf,
                scanner_metadata={"strike_pick": strike_pick},
            )
            greeks_mult = greeks_confirmation_multiplier(
                stub, None, config.risk
            )
        entry_meta = {
            "greeks_mult": float(greeks_mult),
            "volume": (strike_pick or {}).get("volume"),
            "oi": (strike_pick or {}).get("oi"),
        }

        available -= premium_cost
        deployed += premium_cost
        open_trades.append(
            OpenTrade(
                strategy=strategy,
                side=side,
                strike=strike,
                entry_ts=when,
                entry_spot=spot,
                entry_premium=prem,
                lot_size=ls,
                lots=lots,
                confidence=conf,
                premium_source=src,
                tsym=inst.tsym if inst else "",
                token=token,
                entry_meta=entry_meta,
            )
        )
        stats.entries += 1
        util_now = deployed / equity * Decimal("100") if equity > 0 else Decimal("0")
        target = lots_for_confidence(
            config.risk,
            conf,
            entry_ltp=prem,
            lot_size=ls,
            available=available,
            deployed=deployed,
            equity=equity,
        )
        pool_equity = starting + realized
        deploy_inr = _premium_deploy(prem, ls, lots)
        print(
            f"  ENTRY {ts_ist.strftime('%H:%M:%S')} {strategy} {side} "
            f"@ {strike} prem=₹{prem}/unit lots={lots}×{ls}qty "
            f"deploy=₹{deploy_inr:.0f} (conf={conf}) open={len(open_trades)} "
            f"pool=₹{pool_equity:.0f} deployed=₹{deployed:.0f} ({util_now:.0f}%) "
            f"avail=₹{available:.0f}"
        )
        return True

    total_ticks = 0
    total_entry_scans = 0
    for i, bar in enumerate(m1_all):
        prior_m1 = _slice_before(m1_all, bar.ts)
        waypoints = _live_parity_waypoints(
            bar,
            exit_sec=exit_sec,
            scan_sec=scan_sec,
            prior_m1=prior_m1,
            honest=honest_replay,
        )

        for wp_i, (ts, spot, allow_entry) in enumerate(waypoints):
            total_ticks += 1
            ts_ist = ts.astimezone(IST)
            t_clock = ts_ist.time()
            bar_frac = Decimal(str(min(1.0, (ts - bar.ts).total_seconds() / 60.0)))
            is_last_scan = i == len(m1_all) - 1 and wp_i == len(waypoints) - 1

            m1_closed = _slice_before(m1_all, bar.ts)
            m1 = m1_closed + [_forming_m1(bar, ts, spot, honest=honest_replay)]
            # Mirror live candles_stale: skip entries across feed holes >120s.
            bar_feed_stale = False
            if live_parity and i > 0:
                prev_ts = m1_all[i - 1].ts
                if prev_ts.tzinfo is None:
                    prev_ts = prev_ts.replace(tzinfo=timezone.utc)
                gap_sec = (bar.ts - prev_ts).total_seconds()
                if gap_sec > 120:
                    bar_feed_stale = True
            if honest_replay:
                m3 = _slice_closed_period(m3_all, ts, 3)
                m5 = _slice_closed_period(m5_all, ts, 5)
            else:
                m3 = _slice_upto(m3_all, ts)
                m5 = _slice_upto(m5_all, ts)
            md._candles[CandleInterval.M1] = m1  # type: ignore[attr-defined]
            md._candles[CandleInterval.M3] = m3  # type: ignore[attr-defined]
            md._candles[CandleInterval.M5] = m5  # type: ignore[attr-defined]
            fut_spot_px = _price_at(fut_m1_all, ts) or spot
            cash_spot_px = _price_at(cash_m1_all, ts) or fut_spot_px
            md.set_cash_index_ltp(cash_spot_px)
            md._spot_ltp = fut_spot_px  # type: ignore[attr-defined]
            spot = fut_spot_px

            # Dual-chart: ATM from cash when configured; FUT remains momentum/VWAP source.
            prefer_cash_atm = bool(config.symbols.get("atm_from_cash_index", True))
            atm_px = cash_spot_px if prefer_cash_atm and cash_spot_px is not None else spot
            atm = _round_strike(atm_px, step)
            opt_ex = str(config.symbols.get("exchange_options", "NFO"))
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

            # Option VWAP / Greeks context from ATM side vs VWAP (same idea as live)
            vwap_hint = session_vwap(m1)
            prefer_pe = vwap_hint is not None and spot < vwap_hint
            ctx_inst = pe if prefer_pe else ce
            ctx_st = band_states.get(ctx_inst.token) if ctx_inst else None
            opt_ctx: dict = {}
            if ctx_st and ctx_st.ltp is not None:
                opt_ctx["ltp"] = float(ctx_st.ltp)
                opt_ctx["spread_pct"] = float(ctx_st.spread_pct or 1)
                opt_ctx["volume"] = ctx_st.volume
                opt_ctx["oi"] = ctx_st.oi
                den = option_vwap_den.get(ctx_inst.token)
                num = option_vwap_num.get(ctx_inst.token)
                if den and num and den > 0:
                    opt_ctx["option_vwap"] = float(num / den)
                # else leave unset → quality gate scores +7 like live
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
                    premium_tracker.record(
                        _tok, float(spot), float(_st.ltp), ts
                    )

            feat = features_eng.compute()
            extra = dict(feat.extra or {})
            extra["cash_index_spot"] = float(cash_spot_px)
            extra["fut_spot"] = float(fut_spot_px)
            extra["basis_points"] = round(float(fut_spot_px) - float(cash_spot_px), 2)
            feat = feat.model_copy(
                update={"ts": ts, "nifty_spot": fut_spot_px, "extra": extra}
            )
            dte = max(0, (expiry_d - day).days)
            mi_ctx = MarketIntelligenceContext(
                features=feat,
                m1=m1,
                m5=m5,
                index_ltps={
                    "NIFTY": Decimal(str(cash_spot_px)),
                    "NIFTY_FUT": Decimal(str(fut_spot_px)),
                },
                index_opens=index_opens,
                chain_snapshot=chain,
                option_states=band_states,
                now=ts,
                is_expiry_day=(expiry_d == day),
                days_to_expiry=dte,
            )
            mi_snap = mi_engine.build(mi_ctx)
            extra = mi_engine.apply_to_features_extra(dict(feat.extra or {}), mi_snap)
            feat = feat.model_copy(update={"extra": extra})
            regime = regime_clf.classify(
                feat,
                m1,
                m5,
                is_expiry_day=(expiry_d == day),
                days_to_expiry=dte,
                now=ts_ist,
            )
            vwap = feat.session_vwap
            force = t_clock >= force_exit_t or is_last_scan

            still_open: list[OpenTrade] = []
            for ot in open_trades:
                prem, src = _mark_premium(
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
                    pnl = (prem - ot.entry_premium) * ot.lot_size * ot.lots
                    premium_freed = ot.entry_premium * ot.lot_size * ot.lots
                    deployed = max(Decimal("0"), deployed - premium_freed)
                    available = available + premium_freed + pnl
                    realized += pnl
                    stats.closed.append(
                        ClosedTrade(
                            strategy=ot.strategy,
                            side=ot.side,
                            strike=ot.strike,
                            entry_ts=ot.entry_ts,
                            exit_ts=ts,
                            entry_spot=ot.entry_spot,
                            exit_spot=spot,
                            entry_premium=ot.entry_premium,
                            exit_premium=prem,
                            lots=ot.lots,
                            lot_size=ot.lot_size,
                            pnl=pnl.quantize(Decimal("0.01")),
                            exit_reason=reason,
                            confidence=ot.confidence,
                            premium_source=f"{ot.premium_source}->{src}",
                            tsym=ot.tsym,
                        )
                    )
                    if ot.token:
                        cooldown_until[ot.token] = ts + timedelta(seconds=cooldown_sec)
                    global_cooldown_until = ts + timedelta(seconds=cooldown_sec)
                    if pnl < 0:
                        consecutive_losses += 1
                    else:
                        consecutive_losses = 0
                    learner.record_trade(
                        ot.strategy, pnl, confidence=ot.confidence, exit_reason=reason
                    )
                    if pnl < 0:
                        validator.record_session_loss(
                            ot.strategy,
                            is_expiry_day=(expiry_d == day),
                            exit_reason=reason,
                        )
                    if should_queue_reversal_flip(
                        exit_reason=reason or "",
                        option_side=ot.side,
                        market_data=md,
                        exit_cfg=exit_cfg,
                    ):
                        pending_flips.append(
                            {
                                "side": "PE" if ot.side == "CE" else "CE",
                                "from_side": ot.side,
                            }
                        )
                    # Live does not force an extra entry scan on the exit quote.
                    pool_equity = starting + realized
                    print(
                        f"  EXIT  {ts_ist.strftime('%H:%M:%S')} {ot.strategy} {ot.side} "
                        f"{ot.strike} lots={ot.lots} P&L=₹{pnl.quantize(Decimal('0.01'))} "
                        f"reason={reason}  open={len(open_trades)-1} "
                        f"pool=₹{pool_equity:.0f} avail=₹{available:.0f} deployed=₹{deployed:.0f}"
                    )
                else:
                    still_open.append(ot)
            open_trades = still_open

            equity = starting + realized
            if deployed > peak_deployed:
                peak_deployed = deployed
            util = (deployed / equity * Decimal("100")) if equity > 0 else Decimal("0")
            if util > peak_util_pct:
                peak_util_pct = util

            if t_clock < entry_start or t_clock > entry_end:
                pending_flips.clear()
                continue
            if len(m1) < 20 or len(m3) < 5:
                pending_flips.clear()
                continue
            if max_daily_loss > 0 and realized <= -max_daily_loss:
                stats.skips["max_daily_loss"] = stats.skips.get("max_daily_loss", 0) + 1
                pending_flips.clear()
                continue

            # Immediate CE↔PE flip after trend_reversal (before normal scan).
            flips = list(pending_flips)
            pending_flips.clear()
            for flip in flips:
                flip_side = flip["side"]
                resolved = resolve_side_contract(
                    config=config,
                    universe=uni,
                    side=flip_side,
                    spot=spot,
                    option_states=band_states,
                    expiry=expiry_d,
                    now=ts,
                )
                if resolved is None:
                    stats.skips["flip_no_contract"] = stats.skips.get("flip_no_contract", 0) + 1
                    continue
                flip_inst, flip_st, pick = resolved
                flip_bias = Bias.BULLISH if flip_side == "CE" else Bias.BEARISH
                flip_feat = feat.model_copy(update={"bias_5m": flip_bias, "ts": ts})
                flip_signal = CandidateSignal(
                    ts=ts,
                    setup_type="trend_reversal_flip",
                    side=flip_side,
                    instrument_token=flip_inst.token,
                    tsym=flip_inst.tsym,
                    strategy_version=str(config.strategy.get("strategy_version", "bt")),
                    feature_snapshot=flip_feat,
                    scanner_metadata={
                        "option_ltp": str(flip_st.ltp),
                        "lot_size": flip_inst.lot_size,
                        "exchange": flip_inst.exchange,
                        "strike_pick": pick,
                    },
                    confidence=80,
                )
                v = validator.validate(
                    flip_signal,
                    flip_st,
                    has_open_for_token=any(
                        o.token == flip_inst.token for o in open_trades if o.token
                    ),
                    in_cooldown=False,
                    kill_switch=False,
                    is_expiry_day=(expiry_d == day),
                    now=ts,
                    premium_tracker=premium_tracker,
                    regime_primary=regime.primary,
                    option_m1=_validator_option_m1(flip_inst.token),
                    spot_m1=m1,
                )
                if not v.passed:
                    key = ",".join(v.rejection_reasons)[:60]
                    stats.skips[f"validator:{key}"] = stats.skips.get(f"validator:{key}", 0) + 1
                    if decision_log is not None:
                        decision_log.append(
                            {
                                "ts_ist": ts_ist.strftime("%Y-%m-%d %H:%M:%S"),
                                "ts_epoch": ts.timestamp(),
                                "setup_type": "trend_reversal_flip",
                                "side": flip_side,
                                "tsym": flip_inst.tsym,
                                "confidence": 80,
                                "validator_passed": False,
                                "rejection_reasons": list(v.rejection_reasons),
                                "entered": False,
                            }
                        )
                    continue
                flip_entered = _try_enter(
                    strategy="trend_reversal_flip",
                    side=flip_side,
                    strike=Decimal(str(pick.get("strike", flip_inst.strike))),
                    inst=flip_inst,
                    conf=80,
                    when=ts,
                    spot=spot,
                    bar_frac=bar_frac,
                    ts_ist=ts_ist,
                    equity=equity,
                    bypass_cooldown=True,
                    strike_pick=pick,
                    bar=bar,
                )
                if decision_log is not None:
                    decision_log.append(
                        {
                            "ts_ist": ts_ist.strftime("%Y-%m-%d %H:%M:%S"),
                            "ts_epoch": ts.timestamp(),
                            "setup_type": "trend_reversal_flip",
                            "side": flip_side,
                            "tsym": flip_inst.tsym,
                            "confidence": 80,
                            "validator_passed": True,
                            "rejection_reasons": [],
                            "entered": flip_entered,
                        }
                    )

            if not allow_entry and not flips:
                continue

            if bar_feed_stale:
                stats.skips["stale_candle_feed"] = stats.skips.get("stale_candle_feed", 0) + 1
                continue

            total_entry_scans += 1

            if max_concurrent > 0 and len(open_trades) >= max_concurrent:
                stats.skips["max_concurrent"] = stats.skips.get("max_concurrent", 0) + 1
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
                key = (decision.selected_reason or "no_signal")[:80]
                stats.skips[key] = stats.skips.get(key, 0) + 1
                continue

            confirmed, wait_reason = entry_confirm.consider(
                signal,
                now=ts,
                in_open_gap_window=in_open_gap_entry_window(
                    ts,
                    entry_start=str(config.validator.get("entry_start_time", "09:30")),
                    open_window_minutes=int(
                        (config.strategy.get("entry_confirmation") or {}).get(
                            "open_window_minutes", 15
                        )
                    ),
                ),
            )
            if confirmed is None:
                if wait_reason:
                    stats.skips[wait_reason[:60]] = stats.skips.get(wait_reason[:60], 0) + 1
                continue
            signal = confirmed

            stats.signals_seen += 1
            side = signal.side
            pick = signal.scanner_metadata.get("strike_pick") or {}
            strike = Decimal(str(pick.get("strike", atm)))
            inst = next(
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
                bid, ask, spread_pct = _option_bid_ask_spread(
                    prem0,
                    token=inst.token,
                    when=ts,
                    option_series=option_series,
                )
                opt_st = OptionState(
                    instrument_token=inst.token,
                    tsym=inst.tsym,
                    ltp=prem0,
                    bid=bid,
                    ask=ask,
                    spread_pct=spread_pct,
                )
            in_cd = (
                (global_cooldown_until is not None and ts < global_cooldown_until)
                or (
                    inst is not None
                    and inst.token in cooldown_until
                    and ts < cooldown_until[inst.token]
                )
            )
            v = validator.validate(
                signal,
                opt_st,
                has_open_for_token=any(
                    o.token == (inst.token if inst else "") for o in open_trades if o.token
                ),
                in_cooldown=in_cd,
                kill_switch=False,
                is_expiry_day=(expiry_d == day),
                now=ts,
                premium_tracker=premium_tracker,
                regime_primary=regime.primary,
                option_m1=_validator_option_m1(inst.token if inst else None),
                spot_m1=m1,
            )
            if not v.passed:
                key = ",".join(v.rejection_reasons)[:60]
                stats.skips[f"validator:{key}"] = stats.skips.get(f"validator:{key}", 0) + 1
                if decision_log is not None:
                    decision_log.append(
                        {
                            "ts_ist": ts_ist.strftime("%Y-%m-%d %H:%M:%S"),
                            "ts_epoch": ts.timestamp(),
                            "setup_type": signal.setup_type,
                            "side": side,
                            "tsym": signal.tsym,
                            "confidence": int(signal.confidence or 0),
                            "validator_passed": False,
                            "rejection_reasons": list(v.rejection_reasons),
                            "entered": False,
                        }
                    )
                continue

            entered = _try_enter(
                strategy=signal.setup_type,
                side=side,
                strike=strike,
                inst=inst,
                conf=int(signal.confidence or 0),
                when=ts,
                spot=spot,
                bar_frac=bar_frac,
                ts_ist=ts_ist,
                equity=equity,
                strike_pick=pick,
                bar=bar,
            )
            if decision_log is not None:
                decision_log.append(
                    {
                        "ts_ist": ts_ist.strftime("%Y-%m-%d %H:%M:%S"),
                        "ts_epoch": ts.timestamp(),
                        "setup_type": signal.setup_type,
                        "side": side,
                        "tsym": signal.tsym,
                        "confidence": int(signal.confidence or 0),
                        "validator_passed": True,
                        "rejection_reasons": [],
                        "entered": entered,
                    }
                )

    print(
        f"\n  Exit ticks: {total_ticks} (@{exit_sec}s)  |  "
        f"Entry scans: {total_entry_scans} (@{scan_sec}s + post-exit)  |  "
        f"1m bars: {len(m1_all)}"
    )

    # Persist machine-readable export for reports
    export_path = (
        ROOT / "reports" / f"backtest_{m1_all[0].ts.astimezone(IST).date()}_{ul}.json"
    )
    export_path.parent.mkdir(parents=True, exist_ok=True)
    import json

    payload = {
        "date": str(m1_all[0].ts.astimezone(IST).date()),
        "spot_open": float(open_spot),
        "spot_close": float(close_spot),
        "spot_change": float(close_spot - open_spot),
        "session_vwap": float(vwap_final) if (vwap_final := session_vwap(m1_all)) else None,
        "capital_start": float(starting),
        "capital_end": float(starting + realized),
        "total_pnl": float(realized),
        "peak_deployed": float(peak_deployed),
        "peak_util_pct": float(peak_util_pct),
        "rules": {
            "max_deployed_pct_of_equity": config.risk.get("max_deployed_pct_of_equity"),
            "max_premium_pct_of_available": config.risk.get("max_premium_pct_of_available"),
            "max_daily_loss": config.risk.get("max_daily_loss"),
            "confidence_lot_sizing": config.risk.get("confidence_lot_sizing"),
            "exits": "5% SL · progressive trail from 1.5% MFE · no time-stop when green",
            "strike_selection": "ATM±1 via BS delta/gamma",
            "flip_on_trend_reversal": flip_on_reversal,
            "exit_tick_seconds": exit_sec,
            "entry_scan_seconds": scan_sec,
            "cadence": "live_parity",
            "honest_replay": honest_replay,
        },
        "signals_seen": stats.signals_seen,
        "entries_filled": stats.entries,
        "trades": [
            {
                "n": n,
                "strategy": t.strategy,
                "side": t.side,
                "strike": float(t.strike),
                "tsym": t.tsym,
                "confidence": t.confidence,
                "lots": t.lots,
                "lot_size": t.lot_size,
                "qty": t.lots * t.lot_size,
                "entry_deploy_inr": float(
                    _premium_deploy(t.entry_premium, t.lot_size, t.lots)
                ),
                "exit_value_inr": float(
                    _premium_deploy(t.exit_premium, t.lot_size, t.lots)
                ),
                "entry_ist": t.entry_ts.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S"),
                "exit_ist": t.exit_ts.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S"),
                "hold_minutes": round((t.exit_ts - t.entry_ts).total_seconds() / 60, 1),
                "entry_spot": float(t.entry_spot),
                "exit_spot": float(t.exit_spot),
                "entry_premium": float(t.entry_premium),
                "exit_premium": float(t.exit_premium),
                "premium_change_pct": round(
                    float((t.exit_premium - t.entry_premium) / t.entry_premium * 100), 2
                )
                if t.entry_premium
                else 0,
                "pnl": float(t.pnl),
                "exit_reason": t.exit_reason,
                "premium_source": t.premium_source,
                "result": "WIN" if t.pnl > 0 else "LOSS",
            }
            for n, t in enumerate(stats.closed, 1)
        ],
        "skips": stats.skips,
    }
    export_path.write_text(json.dumps(payload, indent=2))
    print(f"\nJSON export: {export_path}")

    # Human-readable Markdown report
    md_path = ROOT / "reports" / f"backtest_{m1_all[0].ts.astimezone(IST).date()}_{ul}.md"
    wins = [t for t in stats.closed if t.pnl > 0]
    losses = [t for t in stats.closed if t.pnl <= 0]
    session_pnl = sum((t.pnl for t in stats.closed), Decimal("0"))
    pool_end_equity = starting + realized
    cap_label = (
        f"| Pool equity | ₹{pool_start_equity:.0f} → ₹{pool_end_equity:.2f} |"
        if capital_pool is not None
        else f"| Capital | ₹{starting:.0f} → ₹{pool_end_equity:.2f} |"
    )
    pnl_label = (
        f"| **Session P&L** | **₹{session_pnl:.2f}** |"
        if capital_pool is not None
        else f"| **Net P&L** | **₹{session_pnl:.2f}** |"
    )
    lines = [
        f"# Algo-Flat Day Backtest Report — {payload['date']} ({ul})",
        "",
        "## Session overview",
        "",
        f"| Field | Value |",
        f"|---|---|",
        f"| Spot | {open_spot} → {close_spot} (Δ {close_spot - open_spot}) |",
        f"| Session VWAP | {payload['session_vwap']:.2f} |" if payload["session_vwap"] else "| Session VWAP | n/a |",
        cap_label,
        pnl_label,
        f"| Trades | {len(stats.closed)} ({len(wins)} wins / {len(losses)} losses) |",
        f"| Win rate | {len(wins)/len(stats.closed)*100:.1f}% |" if stats.closed else "| Win rate | n/a |",
        f"| Peak deployed | ₹{peak_deployed:.0f} ({peak_util_pct:.1f}% of equity) |",
        f"| Deploy target | {config.risk.get('max_deployed_pct_of_equity')}% |",
        "",
        "## Rules used",
        "",
        "- **NIFTY lot sizing**: 1 lot = **65 qty**; deploy = premium/unit × 65 × lots",
        "- Confidence-based dynamic lots toward deploy room (not 1 qty per lot)",
        "- Multiple concurrent positions (different strike/side)",
        "- Max ~85% equity deployed; per-trade premium ≤ configured % of available",
        "- **5% max stop** on losers; early cut if never +2% green",
        "- **Progressive trailing** from +1.5% MFE (tighter lock-in as profit grows)",
        "- **No time exit while in profit** — winners run on trailing SL",
        "- Trend reversal (VWAP bias flip) still exits immediately",
        "- Strike pick among **ATM / ATM±1** via Black-Scholes delta/gamma + spread",
        "- **Live-parity cadence**: exit ticks ~2s (quote pace); entry scans every scan_interval + post-exit",
        "- Spot/premium path visits OHLC extremes (tick-like adverse/trail)",
        "- No fixed profit target wait",
        "- Premium marked from **live option 1m candles** (intra-bar OHLC path)",
        "",
        "## Trade-by-trade detail",
        "",
    ]
    for row in payload["trades"]:
        lines += [
            f"### Trade #{row['n']} — {row['result']} ₹{row['pnl']:+.2f}",
            "",
            f"| Field | Value |",
            f"|---|---|",
            f"| Strategy | `{row['strategy']}` |",
            f"| Contract | {row['side']} {row['strike']:.0f} (`{row['tsym'] or 'n/a'}`) |",
            f"| Confidence | {row['confidence']} |",
            f"| Size | {row['lots']} lot(s) × {row['lot_size']} qty/lot = **{row['qty']} units** |",
            f"| Entry deploy | **₹{row['entry_deploy_inr']:,.2f}** "
            f"({row['lots']}×{row['lot_size']}×₹{row['entry_premium']:.2f}/unit) |",
            f"| Entry | **{row['entry_ist']} IST** @ **₹{row['entry_premium']:.2f}/unit** "
            f"(spot {row['entry_spot']:.2f}) |",
            f"| Exit | **{row['exit_ist']} IST** @ **₹{row['exit_premium']:.2f}/unit** "
            f"(spot {row['exit_spot']:.2f}) |",
            f"| Hold | {row['hold_minutes']} min |",
            f"| Premium Δ | {row['premium_change_pct']:+.2f}% per unit |",
            f"| P&L calc | (exit−entry)×qty = "
            f"({row['exit_premium']:.2f}−{row['entry_premium']:.2f})×{row['qty']} |",
            f"| Exit reason | `{row['exit_reason']}` |",
            f"| Marking | {row['premium_source']} |",
            f"| **P&L** | **₹{row['pnl']:+.2f}** |",
            "",
        ]
    lines += [
        "## P&L running balance",
        "",
        "| After trade | Cumulative P&L | Equity |",
        "|---|---|---|",
    ]
    cum = Decimal("0")
    for row in payload["trades"]:
        cum += Decimal(str(row["pnl"]))
        lines.append(
            f"| #{row['n']} {row['strategy']} {row['side']} | ₹{float(cum):+.2f} | ₹{float(pool_start_equity + cum):.2f} |"
        )
    lines += [
        "",
        "## Exit reason breakdown",
        "",
    ]
    by_reason: dict[str, list] = {}
    for t in stats.closed:
        by_reason.setdefault(t.exit_reason, []).append(t)
    for reason, items in sorted(by_reason.items()):
        rpnl = sum((x.pnl for x in items), Decimal("0"))
        lines.append(f"- `{reason}`: {len(items)} trades, P&L ₹{rpnl:+.2f}")
    lines += ["", f"_Generated by `scripts/day_backtest.py` · export `{export_path.name}`_", ""]
    md_path.write_text("\n".join(lines))
    print(f"Markdown report: {md_path}")

    print("\n" + "=" * 64)
    print(f"TODAY BACKTEST — {ul} · futures · live-parity exits · flip · ATM±1")
    print("=" * 64)
    print(f"Date (IST): {m1_all[0].ts.astimezone(IST).date()}")
    print(f"Underlying: {ul}  capital=₹{starting}")
    print(f"Spot: {open_spot} → {close_spot}  Δ={close_spot - open_spot}")
    print(f"1m bars: {len(m1_all)}  | 3m: {len(m3_all)}  | 5m: {len(m5_all)}")
    print(f"Signals seen: {stats.signals_seen}  | Entries filled: {stats.entries}")
    print(f"Session VWAP (full day): {vwap_final}")
    print(f"Pool equity start: ₹{pool_start_equity:.0f}  | End: ₹{pool_end_equity:.2f}  session P&L: ₹{session_pnl:.2f}")
    print(
        f"Peak deployed: ₹{peak_deployed:.0f}  "
        f"({peak_util_pct:.1f}% of equity)  target≈85%"
    )

    total = sum((t.pnl for t in stats.closed), Decimal("0"))
    print(f"\nTrades closed: {len(stats.closed)}  wins={len(wins)} losses={len(losses)}")
    print(f"Total P&L (₹): {total}")
    if stats.closed:
        by_reason2: dict[str, int] = {}
        for t in stats.closed:
            by_reason2[t.exit_reason] = by_reason2.get(t.exit_reason, 0) + 1
        print("Exit reasons:", ", ".join(f"{k}={v}" for k, v in sorted(by_reason2.items())))
        print("\n--- Trade list ---")
        for n, t in enumerate(stats.closed, 1):
            hold_m = (t.exit_ts - t.entry_ts).total_seconds() / 60
            print(
                f"{n}. {t.entry_ts.astimezone(IST).strftime('%H:%M')}→"
                f"{t.exit_ts.astimezone(IST).strftime('%H:%M')}  "
                f"{t.strategy} {t.side} {t.strike}  conf={t.confidence} "
                f"lots={t.lots}  "
                f"prem {t.entry_premium}→{t.exit_premium}  "
                f"P&L ₹{t.pnl}  exit={t.exit_reason}  "
                f"hold={hold_m:.0f}m"
            )

    top = sorted(stats.skips.items(), key=lambda x: -x[1])[:10]
    print("\n--- Skip / NO_TRADE reasons ---")
    for k, v in top:
        print(f"  {v:4d}  {k}")

    opt_entries = sum(1 for t in stats.closed if t.premium_source.startswith("option_candle"))
    print(
        f"\nPremium marking: {opt_entries}/{len(stats.closed)} trades used option candles"
    )
    print("=" * 64)

    if broker_connected and broker is not None:
        await broker.disconnect()

    if capital_pool is not None:
        capital_pool["realized"] = realized
        end_equity = capital_pool["starting"] + realized
        capital_pool["opening_equity"] = end_equity
        capital_pool["available"] = end_equity
        capital_pool["deployed"] = Decimal("0")

    return {
        "underlying": ul,
        "date": str(m1_all[0].ts.astimezone(IST).date()),
        "starting": starting,
        "pnl": total,
        "trades": len(stats.closed),
        "wins": len(wins),
        "losses": len(losses),
        "entries": stats.entries,
        "signals_seen": stats.signals_seen,
        "open_spot": open_spot,
        "close_spot": close_spot,
        "closed": stats.closed,
        "skips": dict(stats.skips),
        "decision_log": decision_log or [],
    }


async def _run_carry_forward_backtest(
    *,
    days: list[date],
    underlying: str = "NIFTY",
    from_snap: bool = False,
    from_db: bool = False,
    prefer_upstox: bool = False,
    refresh_snap: bool = False,
    exit_interval: int | None = None,
    scan_interval: int | None = None,
    honest_replay: bool = True,
    live_parity: bool = False,
    initial_capital: Decimal | None = None,
    report_prefix: str = "backtest_week",
) -> dict[str, Any]:
    """Multi-day backtest with compounded capital (P&L carries day to day)."""
    from algoflat.symbols_util import total_account_capital, uses_pooled_capital

    if not days:
        raise ValueError("days must not be empty")

    config = get_config()
    ul = underlying.upper()
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    initial = initial_capital
    if initial is None:
        initial = total_account_capital(config) if uses_pooled_capital(config) else Decimal(
            str(config.risk.get("account_capital_inr", 50000))
        )
    capital_pool: dict[str, Decimal] = {
        "starting": initial,
        "available": initial,
        "deployed": Decimal("0"),
        "realized": Decimal("0"),
    }

    print("\n" + "=" * 72)
    print(f"CARRY-FORWARD BACKTEST — {ul} · {days[0]} → {days[-1]}")
    print(f"Opening equity: ₹{initial}")
    print("Expiry note: NIFTY weekly = Tuesday — Mon/Tue use expiring series; Wed+ rolls next week")
    print(
        f"Replay mode: {'HONEST (no future bar/VWAP peek)' if honest_replay else 'synthetic intra-bar'}"
        + (" · LIVE-PARITY (match live session)" if live_parity else "")
        + (" · DB-first" if from_db else "")
    )
    print("=" * 72)

    daily_results: list[dict[str, Any]] = []
    missing: list[date] = []

    for day in days:
        label = day_names[day.weekday()]
        print(f"\n{'#' * 72}\n#  {label} {day.isoformat()}\n{'#' * 72}")
        open_eq = capital_pool["starting"] + capital_pool["realized"]
        capital_pool["available"] = open_eq
        capital_pool["deployed"] = Decimal("0")
        print(f"  >>> Session open equity: ₹{open_eq:.2f}")

        snap = None if refresh_snap else (load_day_snap(day, ul) if not from_db else None)
        if from_snap and snap is None:
            print(f"  WARN: no snap for {day} — skipping (fetch with refresh-snap)")
            missing.append(day)
            continue

        out = await run_backtest(
            day,
            underlying=ul,
            exit_interval=exit_interval,
            scan_interval=scan_interval,
            from_snap=from_snap,
            from_db=from_db,
            prefer_upstox=prefer_upstox,
            refresh_snap=refresh_snap,
            capital_pool=capital_pool,
            honest_replay=honest_replay,
            live_parity=live_parity,
        )
        if not out:
            missing.append(day)
            continue

        end_eq = float(open_eq) + float(out["pnl"])
        day_pnl = out["pnl"]
        snap_meta = snap.get("meta", {}) if snap else {}
        expiry_sym = snap_meta.get("expiry_symbol") if snap_meta else "?"
        if not expiry_sym or expiry_sym == "?":
            loaded = load_day_snap(day, ul)
            if loaded:
                expiry_sym = loaded.get("meta", {}).get("expiry_symbol", "?")

        daily_results.append(
            {
                "underlying": out["underlying"],
                "date": out["date"],
                "weekday": label,
                "open_equity": float(open_eq),
                "close_equity": end_eq,
                "day_pnl": float(day_pnl),
                "pnl": float(out["pnl"]),
                "trades": out["trades"],
                "wins": out["wins"],
                "losses": out["losses"],
                "entries": out["entries"],
                "signals_seen": out["signals_seen"],
                "open_spot": float(out["open_spot"]),
                "close_spot": float(out["close_spot"]),
                "expiry_symbol": expiry_sym,
            }
        )

        capital_pool["starting"] = Decimal(str(end_eq))
        capital_pool["realized"] = Decimal("0")
        capital_pool["available"] = Decimal(str(end_eq))
        capital_pool["deployed"] = Decimal("0")
        print(f"  >>> Session close equity: ₹{end_eq:.2f}  (day P&L ₹{float(day_pnl):.2f})")

    final_equity = capital_pool["starting"]
    total_pnl = final_equity - initial
    total_trades = sum(r["trades"] for r in daily_results)
    total_wins = sum(r["wins"] for r in daily_results)

    def _json_num(v: Any) -> Any:
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, dict):
            return {k: _json_num(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_json_num(x) for x in v]
        return v

    range_payload = _json_num(
        {
            "underlying": ul,
            "range_start": days[0].isoformat(),
            "range_end": days[-1].isoformat(),
            "capital_start": float(initial),
            "capital_end": float(final_equity),
            "total_pnl": float(total_pnl),
            "return_pct": float(total_pnl / initial * 100) if initial else 0.0,
            "total_trades": total_trades,
            "total_wins": total_wins,
            "days_run": len(daily_results),
            "days_missing": [d.isoformat() for d in missing],
            "daily": daily_results,
        }
    )

    honest_tag = "honest_" if honest_replay and not live_parity else ""
    parity_tag = "live_parity_" if live_parity else ""
    report_stem = f"{report_prefix}_{parity_tag}{honest_tag}{days[0]}_{days[-1]}_{ul}"
    config_capital = total_account_capital(config) if uses_pooled_capital(config) else Decimal(
        str(config.risk.get("account_capital_inr", 50000))
    )
    if initial != config_capital:
        report_stem = f"{report_prefix}_{parity_tag}{honest_tag}{int(initial)}_{days[0]}_{days[-1]}_{ul}"
    json_path = ROOT / "reports" / f"{report_stem}.json"
    md_path = ROOT / "reports" / f"{report_stem}.md"
    json_path.write_text(json.dumps(range_payload, indent=2))

    md_lines = [
        f"# Carry-Forward Backtest — {ul} ({days[0]} → {days[-1]})",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Starting capital | ₹{initial:,.2f} |",
        f"| Ending capital | **₹{final_equity:,.2f}** |",
        f"| **Period P&L** | **₹{total_pnl:+,.2f}** ({range_payload['return_pct']:+.1f}%) |",
        f"| Trading days replayed | {len(daily_results)} / {len(days)} |",
        f"| Total trades | {total_trades} ({total_wins} wins) |",
        "",
        "## Daily carry-forward (compounded)",
        "",
        "| Day | Date | Expiry series | Open ₹ | Close ₹ | Day P&L | Trades | W/L |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for r in daily_results:
        md_lines.append(
            f"| {r['weekday']} | {r['date']} | {r.get('expiry_symbol', '?')} | "
            f"₹{r['open_equity']:,.0f} | ₹{r['close_equity']:,.0f} | "
            f"₹{r['day_pnl']:+,.2f} | {r['trades']} | {r['wins']}/{r['losses']} |"
        )
    if missing:
        md_lines += ["", f"**Missing snaps:** {', '.join(d.isoformat() for d in missing)}"]
    md_lines += [
        "",
        "_Capital compounds daily — each session opens with prior close equity._",
        f"_Generated by `scripts/day_backtest.py` · `{json_path.name}`_",
        "",
    ]
    md_path.write_text("\n".join(md_lines))

    ledger_stem = f"ledger_{report_prefix}_{days[0]}_{days[-1]}_{ul}"
    if initial != config_capital:
        ledger_stem = f"ledger_{report_prefix}_{int(initial)}_{days[0]}_{days[-1]}_{ul}"
    ledger_md, ledger_json = write_week_ledger(
        week_start=days[0],
        week_end=days[-1],
        underlying=ul,
        capital_start=initial,
        stem=ledger_stem,
        days=days,
    )

    print("\n" + "=" * 72)
    print("PERIOD SUMMARY")
    print("=" * 72)
    for r in daily_results:
        print(
            f"  {r['weekday']} {r['date']}  exp={r.get('expiry_symbol','?')}  "
            f"₹{r['open_equity']:,.0f} → ₹{r['close_equity']:,.0f}  "
            f"P&L ₹{r['day_pnl']:+,.2f}  trades={r['trades']}"
        )
    print("-" * 72)
    print(
        f"  PERIOD TOTAL  ₹{initial:,.0f} → ₹{final_equity:,.2f}  "
        f"P&L ₹{total_pnl:+,.2f} ({range_payload['return_pct']:+.1f}%)  "
        f"trades={total_trades}"
    )
    if missing:
        print(f"  MISSING DAYS: {', '.join(d.isoformat() for d in missing)}")
    print(f"\nJSON: {json_path}")
    print(f"Markdown: {md_path}")
    print(f"Ledger: {ledger_md}")
    print(f"Ledger JSON: {ledger_json}")
    print("=" * 72)

    return range_payload


def _weekdays_between(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError(f"end date {end} is before start date {start}")
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


async def run_range_backtest(
    *,
    start_date: date,
    end_date: date,
    underlying: str = "NIFTY",
    from_snap: bool = False,
    from_db: bool = False,
    prefer_upstox: bool = False,
    refresh_snap: bool = False,
    exit_interval: int | None = None,
    scan_interval: int | None = None,
    honest_replay: bool = True,
    live_parity: bool = False,
    initial_capital: Decimal | None = None,
) -> dict[str, Any]:
    days = _weekdays_between(start_date, end_date)
    return await _run_carry_forward_backtest(
        days=days,
        underlying=underlying,
        from_snap=from_snap,
        from_db=from_db,
        prefer_upstox=prefer_upstox,
        refresh_snap=refresh_snap,
        exit_interval=exit_interval,
        scan_interval=scan_interval,
        honest_replay=honest_replay,
        live_parity=live_parity,
        initial_capital=initial_capital,
        report_prefix="backtest_range",
    )


async def run_week_backtest(
    *,
    week_monday: date | None = None,
    underlying: str = "NIFTY",
    from_snap: bool = False,
    from_db: bool = False,
    prefer_upstox: bool = False,
    refresh_snap: bool = False,
    exit_interval: int | None = None,
    scan_interval: int | None = None,
    honest_replay: bool = True,
    live_parity: bool = False,
    initial_capital: Decimal | None = None,
) -> dict[str, Any]:
    """Mon–Fri backtest with compounded capital (P&L carries day to day)."""
    today = datetime.now(tz=IST).date()
    if week_monday is None:
        dow = today.weekday()
        if dow >= 5:
            friday = today - timedelta(days=dow - 4)
        else:
            friday = today - timedelta(days=dow + 3)
        week_monday = friday - timedelta(days=4)

    days = [week_monday + timedelta(days=i) for i in range(5)]
    payload = await _run_carry_forward_backtest(
        days=days,
        underlying=underlying,
        from_snap=from_snap,
        from_db=from_db,
        prefer_upstox=prefer_upstox,
        refresh_snap=refresh_snap,
        exit_interval=exit_interval,
        scan_interval=scan_interval,
        honest_replay=honest_replay,
        live_parity=live_parity,
        initial_capital=initial_capital,
        report_prefix="backtest_week",
    )
    payload["week_start"] = payload.pop("range_start")
    payload["week_end"] = payload.pop("range_end")
    return payload


def write_week_ledger(
    *,
    week_start: date,
    week_end: date,
    underlying: str,
    capital_start: Decimal,
    stem: str | None = None,
    days: list[date] | None = None,
) -> tuple[Path, Path]:
    """Merge per-day backtest JSONs into one full trade ledger."""
    ul = underlying.upper()
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    ledger_days = days if days is not None else [week_start + timedelta(days=i) for i in range(5)]
    all_trades: list[dict[str, Any]] = []
    for day in ledger_days:
        label = day_names[day.weekday()]
        p = ROOT / "reports" / f"backtest_{day.isoformat()}_{ul}.json"
        if not p.exists():
            continue
        j = json.loads(p.read_text())
        for t in j.get("trades", []):
            all_trades.append({**t, "date": day.isoformat(), "weekday": label})
    all_trades.sort(key=lambda t: t["entry_ist"])

    capital = float(capital_start)
    rows: list[dict[str, Any]] = []
    for i, t in enumerate(all_trades, 1):
        capital += float(t["pnl"])
        rows.append({**t, "trade_num": i, "running_equity": round(capital, 2)})

    stem = stem or f"ledger_week_{week_start}_{week_end}_{ul}"
    json_path = ROOT / "reports" / f"{stem}.json"
    md_path = ROOT / "reports" / f"{stem}.md"
    json_path.write_text(
        json.dumps(
            {
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "underlying": ul,
                "capital_start": float(capital_start),
                "capital_end": capital,
                "total_trades": len(rows),
                "trades": rows,
            },
            indent=2,
        )
    )

    lines = [
        f"# Weekly Trade Ledger — {ul} ({week_start} → {week_end})",
        "",
        "Full trade log: CE/PE, entry/exit premium per unit, lots (×65 qty), deploy ₹, P&L, equity.",
        "",
        "_NIFTY: 1 lot = 65 qty. Deploy at entry = premium/unit × 65 × lots. "
        "P&L = (exit_prem − entry_prem) × qty._",
        "",
        f"**Starting capital:** ₹{float(capital_start):,.0f}  |  "
        f"**Ending capital:** ₹{capital:,.2f}  |  **Trades:** {len(rows)}",
        "",
        "## Ledger table",
        "",
        "| # | Day | Date | Strategy | Side | Symbol | Strike | Lots | Qty | "
        "Entry (IST) | Prem/unit ₹ | Deploy ₹ | Exit (IST) | Exit/unit ₹ | Hold | "
        "Spot In→Out | P&L | Balance | Result | Exit |",
        "|---:|---|---|---|---|---|---:|---:|---:|---|---:|---:|---|---:|---:|---|---:|---:|---|---|",
    ]
    for r in rows:
        entry_t = r["entry_ist"].split(" ", 1)[1]
        exit_t = r["exit_ist"].split(" ", 1)[1]
        entry_deploy = float(
            r.get("entry_deploy_inr")
            or (r["entry_premium"] * r["lot_size"] * r["lots"])
        )
        lines.append(
            f"| {r['trade_num']} | {r['weekday']} | {r['date']} | {r['strategy']} | {r['side']} | "
            f"`{r['tsym']}` | {r['strike']:.0f} | {r['lots']} | {r['qty']} | {entry_t} | "
            f"{r['entry_premium']:.2f} | {entry_deploy:,.0f} | {exit_t} | {r['exit_premium']:.2f} | "
            f"{r['hold_minutes']:.1f}m | {r['entry_spot']:.0f}→{r['exit_spot']:.0f} | "
            f"**{r['pnl']:+,.2f}** | ₹{r['running_equity']:,.0f} | {r['result']} | {r['exit_reason']} |"
        )
    lines += ["", "## Trade-by-trade detail", ""]
    for r in rows:
        lines += [
            f"### #{r['trade_num']} — {r['result']} ₹{r['pnl']:+,.2f} ({r['weekday']} {r['date']})",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| Strategy | `{r['strategy']}` |",
            f"| Contract | **{r['side']}** {r['strike']:.0f} — `{r['tsym']}` |",
            f"| Confidence | {r['confidence']} |",
            f"| Size | {r['lots']} lot(s) × {r['lot_size']} qty/lot = **{r['qty']} units** |",
            f"| Entry deploy | **₹{r.get('entry_deploy_inr', r['entry_premium'] * r['lot_size'] * r['lots']):,.2f}** "
            f"({r['lots']}×{r['lot_size']}×₹{r['entry_premium']:.2f}/unit) |",
            f"| Entry | **{r['entry_ist']} IST** @ **₹{r['entry_premium']:.2f}/unit** "
            f"(NIFTY spot {r['entry_spot']:.2f}) |",
            f"| Exit | **{r['exit_ist']} IST** @ **₹{r['exit_premium']:.2f}/unit** "
            f"(NIFTY spot {r['exit_spot']:.2f}) |",
            f"| Premium change | {r['premium_change_pct']:+.2f}% per unit |",
            f"| Hold | {r['hold_minutes']:.1f} min |",
            f"| **P&L** | **₹{r['pnl']:+,.2f}** "
            f"= (₹{r['exit_premium']:.2f}−₹{r['entry_premium']:.2f})×{r['qty']} |",
            f"| Running equity | ₹{r['running_equity']:,.2f} |",
            f"| Exit reason | `{r['exit_reason']}` |",
            "",
        ]
    lines += [
        f"_Generated by `scripts/day_backtest.py --week` · `{json_path.name}`_",
        "",
    ]
    md_path.write_text("\n".join(lines))
    return md_path, json_path


async def run_dual_backtest(
    day: date | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    from algoflat.symbols_util import list_underlyings, total_account_capital, uses_pooled_capital

    config = get_config()
    symbols = [
        str(row.get("symbol", "")).upper()
        for row in list_underlyings(config)
        if row.get("symbol")
    ]
    if not symbols:
        symbols = ["NIFTY"]
    capital_pool: dict[str, Decimal] | None = None
    if uses_pooled_capital(config):
        total = total_account_capital(config)
        capital_pool = {
            "starting": total,
            "available": total,
            "deployed": Decimal("0"),
            "realized": Decimal("0"),
        }
        print(f"\n>>> Shared capital pool: ₹{total}")
    results: list[dict[str, Any]] = []
    for sym in symbols:
        print("\n" + "#" * 72)
        print(f"#  BACKTEST: {sym}")
        print("#" * 72)
        out = await run_backtest(
            day, underlying=sym, capital_pool=capital_pool, **kwargs
        )
        if out:
            results.append(out)
    if len(results) > 1:
        combined_pnl = sum((r["pnl"] for r in results), Decimal("0"))
        combined_trades = sum(r["trades"] for r in results)
        combined_wins = sum(r["wins"] for r in results)
        print("\n" + "=" * 72)
        print("COMBINED DUAL-INDEX BACKTEST")
        print("=" * 72)
        for r in results:
            print(
                f"  {r['underlying']:6s}  P&L ₹{r['pnl']:>10}  "
                f"trades={r['trades']}  wins={r['wins']}  "
                f"spot {r['open_spot']}→{r['close_spot']}"
            )
        print("-" * 72)
        print(
            f"  TOTAL   P&L ₹{combined_pnl:>10}  "
            f"trades={combined_trades}  wins={combined_wins}"
        )
        if capital_pool is not None:
            end_equity = capital_pool["starting"] + capital_pool["realized"]
            print(
                f"  POOL    end equity ₹{end_equity:.2f}  "
                f"avail=₹{capital_pool['available']:.0f}  "
                f"deployed=₹{capital_pool['deployed']:.0f}"
            )
        print("=" * 72)
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Algo-Flat day backtest")
    parser.add_argument(
        "--date",
        help="IST session date YYYY-MM-DD (default: today)",
        default=None,
    )
    parser.add_argument(
        "--underlying",
        help="Underlying symbol (default: NIFTY)",
        default="NIFTY",
    )
    parser.add_argument(
        "--exit-interval",
        type=int,
        default=None,
        help="Exit tick seconds (default: runtime backtest_exit_interval_seconds / 2)",
    )
    parser.add_argument(
        "--scan-interval",
        type=int,
        default=None,
        help="Entry scan seconds (default: runtime scan_interval_seconds / 30)",
    )
    parser.add_argument(
        "--from-snap",
        action="store_true",
        help="Replay only from reports/snaps/YYYY-MM-DD (no broker fetch)",
    )
    parser.add_argument(
        "--from-db",
        action="store_true",
        help="Load session candles from Postgres first; fall back to Flattrade/Upstox if missing",
    )
    parser.add_argument(
        "--prefer-upstox",
        action="store_true",
        help="Fetch historical index+option 1m from Upstox (falls back to Flattrade)",
    )
    parser.add_argument(
        "--refresh-snap",
        action="store_true",
        help="Re-fetch Flattrade 1m series and overwrite the day snap",
    )
    parser.add_argument(
        "--snap-only",
        action="store_true",
        help="Fetch and save day snap, then exit without replaying",
    )
    parser.add_argument(
        "--store-db",
        action="store_true",
        help="Ingest only: fetch Upstox 1m (index+options), save snap, persist Postgres, then exit",
    )
    parser.add_argument(
        "--week",
        action="store_true",
        help="Run Mon–Fri week backtest with capital carry-forward (last complete week)",
    )
    parser.add_argument(
        "--week-start",
        help="Monday of week to backtest YYYY-MM-DD (with --week)",
        default=None,
    )
    parser.add_argument(
        "--start-date",
        help="Range start YYYY-MM-DD (with --end-date; weekdays only)",
        default=None,
    )
    parser.add_argument(
        "--end-date",
        help="Range end YYYY-MM-DD inclusive (with --start-date)",
        default=None,
    )
    parser.add_argument(
        "--live-parity",
        action="store_true",
        help="Match live session cadence: intra-bar synthetic path (option M1 chart veto stays ON unless --skip-option-m1)",
    )
    parser.add_argument(
        "--skip-option-m1",
        action="store_true",
        help="Mute option-chart validator checks (optimistic; live on 2026-09-11 DID enforce them)",
    )
    parser.add_argument(
        "--optimistic-intra-bar",
        action="store_true",
        help="Allow intra-bar VWAP/close path (can peek at future within 1m bar; not recommended)",
    )
    parser.add_argument(
        "--honest",
        action="store_true",
        help="Honest replay: closed HTF bars only, no intra-bar feature peek (use with --live-parity for live validator parity)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=None,
        help="Override starting capital for backtest (week mode compounds daily from this)",
    )
    args = parser.parse_args()
    if args.store_db:
        args.prefer_upstox = True
        args.snap_only = True
        args.refresh_snap = True
    d = date.fromisoformat(args.date) if args.date else None
    if args.from_db and args.from_snap:
        parser.error("--from-db and --from-snap are mutually exclusive")
    if args.store_db and (args.from_db or args.from_snap):
        parser.error("--store-db cannot be combined with --from-db or --from-snap")
    live_parity = args.live_parity
    if (
        (args.from_db or args.prefer_upstox)
        and not live_parity
        and not args.optimistic_intra_bar
        and not args.honest
    ):
        live_parity = True
    if args.honest and args.live_parity:
        honest_replay = True
    elif args.honest:
        live_parity = False
        honest_replay = True
    else:
        honest_replay = not args.optimistic_intra_bar and not live_parity
    capital_override = Decimal(str(args.capital)) if args.capital is not None else None
    common = dict(
        exit_interval=args.exit_interval,
        scan_interval=args.scan_interval,
        from_snap=args.from_snap,
        from_db=args.from_db,
        prefer_upstox=args.prefer_upstox,
        refresh_snap=args.refresh_snap,
        snap_only=args.snap_only,
        honest_replay=honest_replay,
        live_parity=live_parity,
        skip_option_m1=bool(args.skip_option_m1),
    )
    if args.week:
        week_mon = date.fromisoformat(args.week_start) if args.week_start else None
        asyncio.run(
            run_week_backtest(
                week_monday=week_mon,
                underlying=args.underlying.upper()
                if str(args.underlying).upper() != "ALL"
                else "NIFTY",
                from_snap=args.from_snap,
                from_db=args.from_db,
                prefer_upstox=args.prefer_upstox,
                refresh_snap=args.refresh_snap,
                exit_interval=args.exit_interval,
                scan_interval=args.scan_interval,
                honest_replay=honest_replay,
                live_parity=live_parity,
                initial_capital=capital_override,
            )
        )
    elif args.start_date and args.end_date:
        start_d = date.fromisoformat(args.start_date)
        end_d = date.fromisoformat(args.end_date)
        if args.store_db:
            async def _store_range() -> None:
                days = _weekdays_between(start_d, end_d)
                print(
                    f"\n{'=' * 72}\n"
                    f"STORE DB — {args.underlying.upper()} · {start_d} → {end_d} "
                    f"({len(days)} weekdays)\n"
                    f"{'=' * 72}"
                )
                for day in days:
                    print(f"\n{'#' * 72}\n#  {day.strftime('%a %Y-%m-%d')}\n{'#' * 72}")
                    await run_backtest(
                        day,
                        underlying=args.underlying.upper(),
                        prefer_upstox=True,
                        refresh_snap=True,
                        snap_only=True,
                        honest_replay=honest_replay,
                        live_parity=live_parity,
                    )

            asyncio.run(_store_range())
        else:
            asyncio.run(
                run_range_backtest(
                    start_date=start_d,
                    end_date=end_d,
                    underlying=args.underlying.upper()
                    if str(args.underlying).upper() != "ALL"
                    else "NIFTY",
                    from_snap=args.from_snap,
                    from_db=args.from_db,
                    prefer_upstox=args.prefer_upstox,
                    refresh_snap=args.refresh_snap,
                    exit_interval=args.exit_interval,
                    scan_interval=args.scan_interval,
                    honest_replay=honest_replay,
                    live_parity=live_parity,
                    initial_capital=capital_override,
                )
            )
    elif args.start_date or args.end_date:
        parser.error("--start-date and --end-date must be used together")
    elif str(args.underlying).upper() == "ALL":
        asyncio.run(run_dual_backtest(d, **common))
    else:
        capital_pool = None
        if capital_override is not None:
            capital_pool = {
                "starting": capital_override,
                "available": capital_override,
                "deployed": Decimal("0"),
                "realized": Decimal("0"),
            }
        asyncio.run(
            run_backtest(
                d,
                underlying=args.underlying.upper(),
                capital_pool=capital_pool,
                **common,
            )
        )
