#!/usr/bin/env python3
"""Replay one session with the live option-lift rules and a ₹25,000 book.

Index bars come from Postgres. Option 1-minute bars come from Postgres when
they are already stored, otherwise from Flattrade. There are no historical
ticks, so each minute is walked open → mid → close, with entries every 5s
and exits every 1s — the same cadence as the live engine.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if Path("/app/src/algoflat").exists():
    sys.path.insert(0, "/app/src")
    os.environ.setdefault("CONFIG_DIR", "/app/config")
    ARCHIVE_DIR = Path("/app/data/archives")
else:
    sys.path.insert(0, str(ROOT / "trading-engine" / "src"))
    os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
    ARCHIVE_DIR = ROOT / "data" / "archives"
os.environ.setdefault("STRUCTLOG_LEVEL", "ERROR")
logging.basicConfig(level=logging.ERROR)

from algoflat.broker.auth import ensure_session
from algoflat.broker.credentials import load_flattrade_config
from algoflat.broker.factory import create_live_broker
from algoflat.bus.event_bus import EventBus
from algoflat.config import get_config
from algoflat.db.connection import get_pool, init_pool
from algoflat.features.engine import FeatureEngine
from algoflat.features.indicators import aggregate_from_m1
from algoflat.market_data.engine import MarketDataEngine
from algoflat.market_data.vwap import session_vwap
from algoflat.models.events import (
    Candle,
    CandleInterval,
    FeatureSnapshot,
    Instrument,
    OptionState,
)
from algoflat.position.exit_rules import _momentum_trail_floor, evaluate_momentum_exit
from algoflat.regime.classifier import RegimeClassifier
from algoflat.risk.engine import RiskEngine
from algoflat.risk.greeks_sizing import greeks_confirmation_multiplier
from algoflat.trading.option_lift import find_option_lifts, split_confirmed_lifts
from algoflat.validator.engine import RuleValidator
from algoflat.contract_selector.selector import ContractUniverse

IST = ZoneInfo("Asia/Kolkata")
DAY = date.fromisoformat(os.environ.get("REPLAY_DAY", "2026-10-15"))
EXPIRY = date.fromisoformat(os.environ.get("REPLAY_EXPIRY", "2026-10-27"))
CAPITAL = Decimal("25000")
ARCHIVE = ARCHIVE_DIR / f"option_lift_{DAY.isoformat()}.json"


@dataclass
class Pos:
    token: str
    tsym: str
    side: str
    strike: Decimal
    entry_ts: datetime
    entry_premium: Decimal
    entry_spot: Decimal
    lots: int
    lot_size: int
    confidence: int
    entry_meta: dict
    mfe: Decimal = Decimal("0")
    worst: Decimal | None = None
    confirm: tuple[str, int] | None = None


@dataclass
class Closed:
    tsym: str
    side: str
    strike: Decimal
    entry_ts: datetime
    exit_ts: datetime
    entry_premium: Decimal
    exit_premium: Decimal
    lots: int
    lot_size: int
    pnl: Decimal
    reason: str


def _bar_path(bar: Candle, when: datetime) -> Decimal:
    end = bar.ts + timedelta(minutes=1)
    if when >= end:
        return bar.close
    frac = Decimal(str(max(0.0, (when - bar.ts).total_seconds() / 60.0)))
    mid = bar.vwap if bar.vwap is not None else (bar.high + bar.low) / Decimal("2")
    if frac <= Decimal("0.5"):
        return bar.open + (mid - bar.open) * (frac * 2)
    return mid + (bar.close - mid) * ((frac - Decimal("0.5")) * 2)


def _bar_at(series: list[Candle], when: datetime) -> Candle | None:
    if not series:
        return None
    minute = when.astimezone(timezone.utc).replace(second=0, microsecond=0)
    lo, hi = 0, len(series) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        ts = series[mid].ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < minute:
            lo = mid + 1
        elif ts > minute:
            hi = mid - 1
        else:
            return series[mid]
    return None


def _closed_before(series: list[Candle], when: datetime) -> list[Candle]:
    out: list[Candle] = []
    for bar in series:
        ts = bar.ts if bar.ts.tzinfo else bar.ts.replace(tzinfo=timezone.utc)
        if ts + timedelta(minutes=1) <= when:
            out.append(bar)
    return out


def _forming(bar: Candle, when: datetime, px: Decimal) -> Candle:
    return Candle(
        instrument_token=bar.instrument_token,
        ts=bar.ts,
        open=bar.open,
        high=max(bar.open, px),
        low=min(bar.open, px),
        close=px,
        volume=bar.volume,
        interval=CandleInterval.M1,
    )


def _row_candle(row) -> Candle:
    return Candle(
        instrument_token=str(row["instrument_token"]),
        ts=row["ts"],
        open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        volume=int(row["volume"]) if row["volume"] is not None else None,
        interval=CandleInterval.M1,
    )


def _candle_to_json(c: Candle) -> dict:
    return {
        "token": c.instrument_token,
        "ts": c.ts.astimezone(timezone.utc).isoformat(),
        "o": str(c.open),
        "h": str(c.high),
        "l": str(c.low),
        "c": str(c.close),
        "v": c.volume,
        "vwap": str(c.vwap) if c.vwap is not None else None,
        "oi": c.oi,
    }


def _candle_from_json(d: dict) -> Candle:
    ts = datetime.fromisoformat(d["ts"])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return Candle(
        instrument_token=d["token"],
        ts=ts,
        open=Decimal(d["o"]),
        high=Decimal(d["h"]),
        low=Decimal(d["l"]),
        close=Decimal(d["c"]),
        volume=d.get("v"),
        interval=CandleInterval.M1,
        vwap=Decimal(d["vwap"]) if d.get("vwap") else None,
        oi=d.get("oi"),
    )


async def _load_index(day: date) -> list[Candle]:
    start = datetime.combine(day, time(9, 15), tzinfo=IST)
    end = datetime.combine(day, time(15, 30), tzinfo=IST)
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT instrument_token, ts, open, high, low, close, volume
        FROM candles_1m
        WHERE instrument_token = '26000' AND ts >= $1 AND ts < $2
        ORDER BY ts
        """,
        start,
        end,
    )
    return [_row_candle(r) for r in rows]


async def _load_instruments() -> list[Instrument]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT exchange, token, tsym, underlying, expiry_date, strike, option_type, lot_size
        FROM instruments
        WHERE underlying = 'NIFTY' AND expiry_date = $1
        ORDER BY option_type, strike
        """,
        EXPIRY,
    )
    out: list[Instrument] = []
    for r in rows:
        out.append(
            Instrument(
                exchange=r["exchange"],
                token=str(r["token"]),
                tsym=r["tsym"],
                underlying=r["underlying"],
                expiry_date=datetime.combine(r["expiry_date"], time(15, 30), tzinfo=IST),
                strike=Decimal(str(r["strike"])),
                option_type=r["option_type"],
                lot_size=int(r["lot_size"]),
            )
        )
    return out


def _strikes_for_session(index: list[Candle], instruments: list[Instrument]) -> list[Instrument]:
    """Add contracts the day's high and low need when the database band does not cover them."""
    from algoflat.contract_selector.expiry import format_expiry_tag
    from algoflat.contract_selector.scripmaster import load_weekly_band_from_scripmaster

    step = Decimal("50")
    low = min(c.low for c in index)
    high = max(c.high for c in index)
    need_low = (low / step).to_integral_value(ROUND_HALF_UP) * step - step * 2
    need_high = (high / step).to_integral_value(ROUND_HALF_UP) * step + step * 2
    have = {i.strike for i in instruments}
    if have and min(have) <= need_low and max(have) >= need_high:
        return instruments
    mid = (need_low + need_high) / 2
    band = max(abs(mid - need_low), abs(need_high - mid)) + step
    extra = load_weekly_band_from_scripmaster(
        underlying="NIFTY",
        expiry_tag=format_expiry_tag(EXPIRY),
        atm=mid,
        band_points=band,
    )
    by_token = {i.token: i for i in instruments}
    added = 0
    for inst in extra:
        if inst.strike < need_low or inst.strike > need_high or inst.token in by_token:
            continue
        by_token[inst.token] = inst
        added += 1
    if added:
        print(f"==> Added {added} contracts from the master to cover strikes {need_low}–{need_high}")
    return list(by_token.values())


async def _fetch_options(instruments: list[Instrument], start: datetime, end: datetime) -> dict[str, list[Candle]]:
    if ARCHIVE.exists():
        payload = json.loads(ARCHIVE.read_text())
        series = {tok: [_candle_from_json(x) for x in rows] for tok, rows in payload["options"].items()}
        print(f"==> Option bars from archive ({len(series)} contracts)")
        return series

    print("==> Option 1m bars are not in the database. Fetching them from Flattrade...")
    cfg = await load_flattrade_config()
    await ensure_session(cfg)
    broker = create_live_broker(get_config())
    await broker.connect()
    series: dict[str, list[Candle]] = {}
    try:
        for i, inst in enumerate(instruments, 1):
            rows = await broker.get_candles(inst.exchange, inst.token, CandleInterval.M1, start, end)
            rows = sorted(rows, key=lambda c: c.ts)
            if rows:
                series[inst.token] = rows
            if i % 6 == 0 or i == len(instruments):
                print(f"    {i}/{len(instruments)} contracts, {len(series)} with bars")
            await asyncio.sleep(0.12)
    finally:
        await broker.disconnect()

    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE.write_text(
        json.dumps(
            {
                "day": DAY.isoformat(),
                "expiry": EXPIRY.isoformat(),
                "options": {tok: [_candle_to_json(c) for c in rows] for tok, rows in series.items()},
            }
        )
    )
    print(f"==> Saved {len(series)} option series to {ARCHIVE}")
    return series


def _option_state(inst: Instrument, series: list[Candle], when: datetime) -> OptionState | None:
    bar = _bar_at(series, when)
    if bar is None:
        return None
    ltp = _bar_path(bar, when)
    if ltp <= 0:
        return None
    closed = _closed_before(series, when)
    volume = sum(int(c.volume or 0) for c in closed) or None
    oi = None
    for c in reversed(closed):
        if c.oi is not None:
            oi = c.oi
            break
    half = max(Decimal("0.05"), (ltp * Decimal("0.004")).quantize(Decimal("0.05")))
    bid = max(Decimal("0.05"), ltp - half)
    ask = ltp + half
    spread = ((ask - bid) / ask * Decimal("100")).quantize(Decimal("0.01"))
    return OptionState(
        instrument_token=inst.token,
        tsym=inst.tsym,
        ltp=ltp,
        bid=bid,
        ask=ask,
        spread_pct=spread,
        volume=volume,
        oi=oi,
    )


def _in_cooldown(
    when: datetime,
    token: str,
    *,
    last_any: datetime | None,
    last_loss: dict[str, datetime],
    global_sec: int,
    loss_sec: int,
) -> bool:
    if global_sec > 0 and last_any is not None and (when - last_any).total_seconds() < global_sec:
        return True
    last = last_loss.get(token)
    if last is not None and loss_sec > 0 and (when - last).total_seconds() < loss_sec:
        return True
    return False


def _confirm_exit(pos: Pos, reason: str, required: int) -> bool:
    if reason == "force_exit":
        return True
    if required <= 1:
        return True
    streak = pos.confirm[1] + 1 if pos.confirm and pos.confirm[0] == reason else 1
    pos.confirm = (reason, streak)
    return streak >= required


async def replay() -> None:
    await init_pool()
    config = get_config()
    index = await _load_index(DAY)
    if len(index) < 30:
        raise SystemExit(f"index bars missing for {DAY}: {len(index)}")
    instruments = _strikes_for_session(index, await _load_instruments())
    if not instruments:
        raise SystemExit(f"no {EXPIRY.isoformat()} instruments in the database")
    start = index[0].ts
    end = index[-1].ts + timedelta(minutes=1)
    options = await _fetch_options(instruments, start, end)
    print(
        f"==> {DAY.isoformat()} NIFTY {index[0].close} → {index[-1].close}  "
        f"index bars={len(index)}  contracts={len(options)}  capital=₹{CAPITAL}"
    )

    prior_rows_day = DAY - timedelta(days=1)
    while prior_rows_day.weekday() >= 5:
        prior_rows_day = prior_rows_day - timedelta(days=1)
    prior = await _load_index(prior_rows_day)

    bus = EventBus(max_size=4)
    md = MarketDataEngine(config, create_live_broker(config), bus)
    features_eng = FeatureEngine(config, md)
    if prior:
        features_eng.set_prior_day(max(c.high for c in prior), min(c.low for c in prior), prior[-1].close)
    regime_clf = RegimeClassifier(config)
    validator = RuleValidator(config)
    risk = RiskEngine(config)
    lift_cfg = dict(config.strategy.get("option_lift") or {})
    lift_cfg.setdefault("strike_step", config.symbols.get("strike_step", 50))
    exit_cfg = dict(config.position_exit)
    # Replay-only: keep the +1.5% breakeven arm, and do not lock a floor above entry.
    if os.environ.get("REPLAY_NO_STEP_TRAIL") == "1":
        exit_cfg["trail_step_arm_mfe_pct"] = 1000
    # Replay-only: structure exit waits 3 minutes so a new trade can reach the trail.
    breathe = os.environ.get("REPLAY_BREATHE") == "1"
    if breathe:
        exit_cfg["premium_structure_min_hold_seconds"] = 180
    # Live config keys, overridable by replay env flags.
    second_only_if_green = (
        os.environ.get("REPLAY_SECOND_IF_GREEN", "1" if lift_cfg.get("second_position_only_if_green") else "0")
        == "1"
    )
    one_side = (
        os.environ.get("REPLAY_ONE_SIDE", "1" if lift_cfg.get("one_side_at_a_time") else "0") == "1"
    )
    # Proposed finetunes (replay-only until promoted to live).
    one_per_scan = (
        os.environ.get("REPLAY_ONE_PER_SCAN", "1" if lift_cfg.get("one_fill_per_scan") else "0")
        == "1"
    )
    one_lot = os.environ.get("REPLAY_ONE_LOT") == "1"
    if os.environ.get("REPLAY_NEVER_GREEN_SCRATCH") == "0":
        exit_cfg["never_green_scratch_enabled"] = False
    elif os.environ.get("REPLAY_NEVER_GREEN_SCRATCH") == "1":
        exit_cfg["never_green_scratch_enabled"] = True
        exit_cfg.setdefault("never_green_scratch_min_hold_seconds", 5)
        exit_cfg.setdefault("never_green_scratch_max_mfe_pct", 0.3)
        exit_cfg.setdefault("never_green_scratch_loss_pct", 0.8)
    # Replay-only never-green / entry knobs (do not touch live YAML).
    if os.environ.get("REPLAY_NEVER_GREEN_HOLD"):
        exit_cfg["never_green_scratch_min_hold_seconds"] = int(os.environ["REPLAY_NEVER_GREEN_HOLD"])
    if os.environ.get("REPLAY_NEVER_GREEN_LOSS_PCT"):
        exit_cfg["never_green_scratch_loss_pct"] = float(os.environ["REPLAY_NEVER_GREEN_LOSS_PCT"])
    if os.environ.get("REPLAY_NEVER_GREEN_MFE_PCT"):
        exit_cfg["never_green_scratch_max_mfe_pct"] = float(os.environ["REPLAY_NEVER_GREEN_MFE_PCT"])
    if os.environ.get("REPLAY_MIN_PREMIUM_RISE_PCT"):
        lift_cfg["min_premium_rise_pct"] = float(os.environ["REPLAY_MIN_PREMIUM_RISE_PCT"])
    loss_token_cooldown = int(os.environ.get("REPLAY_LOSS_TOKEN_COOLDOWN", "0") or 0)
    if os.environ.get("REPLAY_ENTRY_END"):
        entry_end_override = time.fromisoformat(os.environ["REPLAY_ENTRY_END"])
    else:
        entry_end_override = None
    # Dead-zone windows: prefer live validator_config; REPLAY_TOD_DEAD_FILTER overrides.
    from algoflat.validator.time_of_day_cautious import tod_dead_zone_rejection_reasons

    dead_cfg = dict(config.validator.get("tod_dead_zone") or {})
    if os.environ.get("REPLAY_TOD_DEAD_FILTER") == "1":
        dead_cfg["enabled"] = True
    elif os.environ.get("REPLAY_TOD_DEAD_FILTER") == "0":
        dead_cfg["enabled"] = False
    if os.environ.get("REPLAY_TOD_SOFT_MIN_RANGE"):
        dead_cfg["soft_min_nifty_5m_range"] = float(os.environ["REPLAY_TOD_SOFT_MIN_RANGE"])

    confirm_scans = max(1, int(lift_cfg.get("confirm_scans", 2)))
    second_min_profit_pct = Decimal(str(lift_cfg.get("second_position_min_profit_pct", 0) or 0))
    second_need = Decimal("1") + (second_min_profit_pct / Decimal("100"))
    exit_ticks = int(exit_cfg.get("exit_confirm_ticks", 2))
    entry_start = time.fromisoformat(str(config.validator.get("entry_start_time", "09:30")))
    entry_end = entry_end_override or time.fromisoformat(
        str(config.validator.get("entry_end_time", "15:00"))
    )
    force_t = time.fromisoformat(str(config.risk.get("force_exit_time", "15:10")))
    max_daily = Decimal(str(config.risk.get("max_daily_loss", 0)))
    global_cd = int(config.risk.get("cooldown_after_exit_seconds", 20))
    loss_cd = max(int(config.risk.get("loss_exit_cooldown_seconds", 20)), loss_token_cooldown)
    block_daily = bool(config.risk.get("block_entries_on_daily_loss", True))

    by_token = {i.token: i for i in instruments}
    open_pos: list[Pos] = []
    closed: list[Closed] = []
    streaks: dict[str, int] = {}
    sent: set[str] = set()
    available = CAPITAL
    deployed = Decimal("0")
    realized = Decimal("0")
    consec = 0
    entries_blocked = False
    last_any: datetime | None = None
    last_loss: dict[str, datetime] = {}
    skips: dict[str, int] = {}
    last_features: FeatureSnapshot | None = None
    feature_minute: datetime | None = None

    def snap(when: datetime):
        from algoflat.risk.engine import DailyRiskSnapshot

        return DailyRiskSnapshot(
            trade_date=DAY,
            starting_capital=CAPITAL,
            available_capital=available,
            deployed_capital=deployed,
            realized_pnl=realized,
            trade_count=len(closed),
            consecutive_losses=consec,
            kill_switch=False,
            entries_blocked=entries_blocked,
            block_reason="max_daily_loss" if entries_blocked else None,
        )

    def close_pos(pos: Pos, when: datetime, px: Decimal, reason: str) -> None:
        nonlocal available, deployed, realized, consec, last_any, entries_blocked
        pnl = ((px - pos.entry_premium) * pos.lot_size * pos.lots).quantize(Decimal("0.01"))
        freed = pos.entry_premium * pos.lot_size * pos.lots
        deployed = max(Decimal("0"), deployed - freed)
        available = available + freed + pnl
        realized += pnl
        if pnl < 0:
            consec += 1
            last_loss[pos.token] = when
        else:
            consec = 0
        last_any = when
        if block_daily and max_daily > 0 and realized <= -max_daily:
            entries_blocked = True
        closed.append(
            Closed(
                tsym=pos.tsym,
                side=pos.side,
                strike=pos.strike,
                entry_ts=pos.entry_ts,
                exit_ts=when,
                entry_premium=pos.entry_premium,
                exit_premium=px,
                lots=pos.lots,
                lot_size=pos.lot_size,
                pnl=pnl,
                reason=reason,
            )
        )
        ist = when.astimezone(IST).strftime("%H:%M:%S")
        held = int((when - pos.entry_ts).total_seconds())
        mfe_pct = (pos.mfe / pos.entry_premium * Decimal("100")) if pos.entry_premium else Decimal("0")
        print(
            f"  EXIT  {ist} {pos.side} {pos.tsym} lots={pos.lots} "
            f"₹{pos.entry_premium}→₹{px} P&L=₹{pnl} {reason} "
            f"held={held}s mfe={mfe_pct:.2f}% book=₹{CAPITAL + realized:.0f}"
        )

    for bar in index:
        minute_end = bar.ts + timedelta(minutes=1)
        t = bar.ts
        while t < minute_end:
            when = t
            ist = when.astimezone(IST)
            clock = ist.time()
            spot = _bar_path(bar, when)
            force = clock >= force_t

            still: list[Pos] = []
            for pos in open_pos:
                series = options.get(pos.token) or []
                obar = _bar_at(series, when)
                ltp = _bar_path(obar, when) if obar is not None else pos.entry_premium
                pos.mfe = max(pos.mfe, ltp - pos.entry_premium)
                pos.worst = ltp if pos.worst is None else min(pos.worst, ltp)
                stop_px = min(ltp, pos.worst)
                pos.worst = ltp
                md._spot_ltp = spot  # type: ignore[attr-defined]
                decision = evaluate_momentum_exit(
                    option_side=pos.side,
                    entry_price=pos.entry_premium,
                    entry_ts=pos.entry_ts,
                    current_ltp=stop_px,
                    mfe_points=pos.mfe,
                    market_data=md,
                    cfg=exit_cfg,
                    force_exit=force,
                    quantity=pos.lot_size * pos.lots,
                    confidence=pos.confidence,
                    setup_type="option_lift",
                    entry_meta=pos.entry_meta,
                    now=when,
                    option_m1=_closed_before(series, when),
                )
                trail = _momentum_trail_floor(pos.entry_premium, pos.mfe, exit_cfg)
                if not decision.should_exit and trail is not None and stop_px <= trail:
                    decision = type(decision)(True, "momentum_trail")
                if decision.should_exit and decision.reason:
                    if _confirm_exit(pos, decision.reason, exit_ticks):
                        close_pos(pos, when, ltp, decision.reason)
                        continue
                else:
                    pos.confirm = None
                still.append(pos)
            open_pos = still

            if block_daily and max_daily > 0 and open_pos:
                unreal = sum(
                    (
                        (
                            (_bar_path(ob, when) if (ob := _bar_at(options.get(p.token) or [], when)) else p.entry_premium)
                            - p.entry_premium
                        )
                        * p.lot_size
                        * p.lots
                    )
                    for p in open_pos
                )
                if realized + unreal <= -max_daily:
                    for pos in list(open_pos):
                        ob = _bar_at(options.get(pos.token) or [], when)
                        px = _bar_path(ob, when) if ob is not None else pos.entry_premium
                        close_pos(pos, when, px, "max_daily_loss")
                    open_pos = []

            sec = int((when - bar.ts).total_seconds())
            if sec % 5 != 0 or clock < entry_start or clock > entry_end or entries_blocked:
                t += timedelta(seconds=1)
                continue
            closed_idx = _closed_before(index, when)
            dead_reasons = tod_dead_zone_rejection_reasons(
                dead_cfg, now=when, spot_m1=closed_idx
            )
            if dead_reasons:
                skips[dead_reasons[0]] = skips.get(dead_reasons[0], 0) + 1
                t += timedelta(seconds=1)
                continue

            forming = _forming(bar, when, spot)
            m1 = closed_idx + [forming]
            m3 = aggregate_from_m1(closed_idx, 3)
            m5 = aggregate_from_m1(closed_idx, 5)
            md._candles[CandleInterval.M1] = m1  # type: ignore[attr-defined]
            md._candles[CandleInterval.M3] = m3  # type: ignore[attr-defined]
            md._candles[CandleInterval.M5] = m5  # type: ignore[attr-defined]
            md._spot_ltp = spot  # type: ignore[attr-defined]
            md.set_cash_index_ltp(spot)
            minute_key = bar.ts
            if feature_minute != minute_key or last_features is None:
                try:
                    last_features = features_eng.compute()
                    feature_minute = minute_key
                except Exception:
                    last_features = FeatureSnapshot(
                        ts=when,
                        nifty_spot=spot,
                        session_vwap=session_vwap(m1),
                    )
            extra = dict(last_features.extra or {})
            extra["cash_index_spot"] = float(spot)
            features = last_features.model_copy(
                update={
                    "ts": when,
                    "nifty_spot": spot,
                    "session_vwap": session_vwap(m1),
                    "extra": extra,
                }
            )
            step = Decimal(str(config.symbols.get("strike_step", 50)))
            atm = (spot / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
            from algoflat.contract_selector.expiry import format_expiry_tag

            universe = ContractUniverse(
                spot=spot,
                atm_strike=atm,
                expiry_symbol=format_expiry_tag(EXPIRY),
                instruments=instruments,
            )
            band_states: dict[str, OptionState | None] = {}
            option_m1: dict[str, list[Candle]] = {}
            for inst in instruments:
                if abs(inst.strike - atm) > step * 2:
                    continue
                series = options.get(inst.token) or []
                option_m1[inst.token] = series
                band_states[inst.token] = _option_state(inst, series, when)

            _signal, note = find_option_lifts(
                features=features,
                universe=universe,
                band_states=band_states,
                option_m1_by_token=option_m1,
                spot_m1=m1,
                cfg=lift_cfg,
                strategy_version="option_lift_replay",
                when=when,
            )
            found = list(note.get("_signals") or [])
            eligible = []
            for sig in found:
                if any(p.token == sig.instrument_token for p in open_pos):
                    continue
                bar_key = f"{sig.instrument_token}:{sig.scanner_metadata.get('bar_ts')}"
                if bar_key in sent:
                    continue
                eligible.append(sig)
            ready, _waiting, streaks = split_confirmed_lifts(eligible, streaks, required=confirm_scans)
            if not ready:
                t += timedelta(seconds=1)
                continue

            try:
                regime = regime_clf.classify(features, m1, m5, is_expiry_day=False, days_to_expiry=12, now=ist)
                regime_name = regime.primary
            except Exception:
                regime_name = None

            from algoflat.validator.time_of_day_cautious import time_of_day_cautious_rejection_reasons

            tod = time_of_day_cautious_rejection_reasons(
                ready[0], config.validator.get("time_of_day_cautious") or {}, now=ist
            )
            if tod:
                skips[tod[0]] = skips.get(tod[0], 0) + 1
                t += timedelta(seconds=1)
                continue

            last_block: str | None = None
            batch_stop = {
                "deployed_cap_exceeded",
                "max_daily_loss",
                "max_consecutive_losses",
                "max_trades_per_day",
                "kill_switch",
                "entries_blocked",
                "cooldown_active",
            }
            size_skip = {"insufficient_capital", "premium_pct_exceeded", "position_cap_exceeded"}

            def head(reason: str | None) -> str:
                return (reason or "").split(",")[0].strip()

            def _mark(pos: Pos) -> Decimal:
                ob = _bar_at(options.get(pos.token) or [], when)
                return _bar_path(ob, when) if ob is not None else pos.entry_premium

            for sig in ready:
                if head(last_block) in batch_stop or head(last_block).startswith("cooldown"):
                    break
                if second_only_if_green and open_pos:
                    not_ready = False
                    for p in open_pos:
                        mark = _mark(p)
                        if second_min_profit_pct > 0:
                            if mark < p.entry_premium * second_need:
                                not_ready = True
                                break
                        elif mark <= p.entry_premium:
                            not_ready = True
                            break
                    if not_ready:
                        skips["wait_for_open_trade_green"] = skips.get("wait_for_open_trade_green", 0) + 1
                        break
                open_sides = {p.side for p in open_pos}
                if one_side and open_sides and sig.side not in open_sides:
                    skips["opposite_side_open"] = skips.get("opposite_side_open", 0) + 1
                    continue
                inst = by_token.get(sig.instrument_token)
                state = band_states.get(sig.instrument_token)
                if inst is None or state is None or state.ltp is None:
                    continue
                bar_key = f"{sig.instrument_token}:{sig.scanner_metadata.get('bar_ts')}"
                sent.add(bar_key)
                cooling = _in_cooldown(
                    when, sig.instrument_token, last_any=last_any, last_loss=last_loss,
                    global_sec=global_cd, loss_sec=loss_cd,
                )
                validation = validator.validate(
                    sig,
                    state,
                    has_open_for_token=any(p.token == sig.instrument_token for p in open_pos),
                    in_cooldown=cooling,
                    kill_switch=False,
                    is_expiry_day=False,
                    now=when,
                    regime_primary=regime_name,
                    option_m1=_closed_before(options.get(sig.instrument_token) or [], when),
                    spot_m1=m1,
                )
                if not validation.passed:
                    reason = validation.rejection_reasons[0] if validation.rejection_reasons else "validation_failed"
                    last_block = reason
                    skips[reason] = skips.get(reason, 0) + 1
                    if head(reason) in size_skip:
                        continue
                    if head(reason) in batch_stop or head(reason).startswith("cooldown"):
                        break
                    continue
                greeks_mult = greeks_confirmation_multiplier(sig, state, config.risk)
                meta = dict(sig.scanner_metadata or {})
                meta["greeks_mult"] = float(greeks_mult)
                sig = sig.model_copy(update={"scanner_metadata": meta})
                sizing = await risk.size_entry(
                    sig,
                    state,
                    snap(when),
                    open_position_count=len(open_pos),
                    is_expiry_day=False,
                )
                if not sizing.approved:
                    last_block = sizing.rejection_reason or "risk_rejected"
                    skips[last_block] = skips.get(last_block, 0) + 1
                    if head(last_block) in size_skip:
                        continue
                    break
                lots = 1 if one_lot else sizing.lots
                lot_size = sizing.lot_size
                qty = lots * lot_size
                premium = state.ltp * qty
                if premium > available:
                    skips["insufficient_capital"] = skips.get("insufficient_capital", 0) + 1
                    continue
                available -= premium
                deployed += premium
                open_pos.append(
                    Pos(
                        token=sig.instrument_token,
                        tsym=sig.tsym,
                        side=sig.side,
                        strike=inst.strike,
                        entry_ts=when,
                        entry_premium=state.ltp,
                        entry_spot=spot,
                        lots=lots,
                        lot_size=lot_size,
                        confidence=int(sig.confidence or 0),
                        entry_meta={"greeks_mult": float(greeks_mult), "volume": state.volume, "oi": state.oi},
                    )
                )
                print(
                    f"  ENTRY {ist.strftime('%H:%M:%S')} {sig.side} {sig.tsym} "
                    f"prem=₹{state.ltp} lots={lots}×{lot_size} "
                    f"deploy=₹{premium:.0f} open={len(open_pos)} avail=₹{available:.0f}"
                )
                if one_per_scan or second_only_if_green:
                    # One fill this scan (and a just-filled trade is not green yet).
                    break
            t += timedelta(seconds=1)

        if bar.ts.astimezone(IST).minute == 0:
            print(f"  … {bar.ts.astimezone(IST).strftime('%H:%M')} spot={bar.close} open={len(open_pos)} closed={len(closed)} book=₹{CAPITAL + realized:.0f}")

    if open_pos:
        last = index[-1]
        when = last.ts + timedelta(minutes=1)
        for pos in list(open_pos):
            ob = _bar_at(options.get(pos.token) or [], last.ts)
            px = ob.close if ob is not None else pos.entry_premium
            close_pos(pos, when, px, "session_end")
        open_pos = []

    wins = [c for c in closed if c.pnl > 0]
    losses = [c for c in closed if c.pnl < 0]
    equity = CAPITAL
    peak = equity
    max_dd = Decimal("0")
    for c in closed:
        equity += c.pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    by_reason: dict[str, Decimal] = {}
    for c in closed:
        by_reason[c.reason] = by_reason.get(c.reason, Decimal("0")) + c.pnl
    print("\n==> Result")
    print(f"  trades={len(closed)}  wins={len(wins)}  losses={len(losses)}  flats={len(closed) - len(wins) - len(losses)}")
    print(f"  net P&L=₹{realized.quantize(Decimal('0.01'))}  end equity=₹{(CAPITAL + realized).quantize(Decimal('0.01'))}")
    print(f"  max drawdown=₹{max_dd.quantize(Decimal('0.01'))}")
    if by_reason:
        print("  by exit:")
        for reason, pnl in sorted(by_reason.items(), key=lambda kv: kv[1]):
            n = sum(1 for c in closed if c.reason == reason)
            print(f"    {reason}: {n} trades, ₹{pnl.quantize(Decimal('0.01'))}")
    top_skips = sorted(skips.items(), key=lambda kv: kv[1], reverse=True)[:8]
    if top_skips:
        print("  entries not taken:")
        for reason, n in top_skips:
            print(f"    {reason}: {n}")


if __name__ == "__main__":
    asyncio.run(replay())
