#!/usr/bin/env python3
"""Setup-first backtest from the open — does NOT use the live trade sheet.

Flow (as agreed):
  WS/radar: option_lift on ATM±2 band
  → greeks (delta band advisory / max+theta hard; option_lift as-is)
  → CE/PE chart (premium 1m structure)
  → Nifty50 hard veto (3×5m plan + rolling 5m/15m regime + S/R side)
  → enter at option LTP; exit via momentum trail (same family as live)

Uses index 1m from Postgres + option 1m from FlatTrade (archived after first pull).
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
from algoflat.config import get_config
from algoflat.db.connection import get_pool, init_pool
from algoflat.features.indicators import aggregate_from_m1
from algoflat.market_data.vwap import session_vwap
from algoflat.models.events import Candle, CandleInterval, Instrument, OptionState
from algoflat.option_data.greeks import compute_greeks
from algoflat.trading.option_lift import find_option_lifts, split_confirmed_lifts
from algoflat.contract_selector.selector import ContractUniverse
from algoflat.validator.option_chart_confirm import analyze_option_chart
from algoflat.validator.quality_entry_pipeline import (
    PipelineCandidate,
    SpotBar,
    run_quality_pipeline,
)

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 10, 19)
EXPIRY = date(2026, 10, 19)  # expiry day
PDH, PDL = 23149.85, 22913.80
LOT = 65
COST_PER_TRADE = Decimal("80")
ARCHIVE = ARCHIVE_DIR / f"option_lift_{DAY.isoformat()}.json"

QUALITY_CFG = {
    "enabled": True,
    "test_only": True,
    "min_abs_delta": 0.30,
    "max_abs_delta": 0.75,
    "enforce_min_abs_delta": False,  # option_lift as-is
    "max_daily_theta_pct_of_premium": 12.0,
    "min_premium": 5.0,
    "vwap_buffer_pct": 0.50,
    "require_premium_above_vwap": True,
    "allow_option_chart_if_vwap_mismatch": True,
    "level_near_pts": 30.0,
    "swing_lookback_5m": 12,
    "mid_allow_continuation": True,
}

OPTION_CHART_CFG = {
    "min_closed_m1_bars": 5,
    "vwap_buffer_pct": 0.50,
    "m1_momentum_lookback": 3,
    "min_m1_momentum_pct": 0.15,
    "min_m3_bars": 3,
    "min_m5_bars": 2,
    "require_higher_low_structure": False,
    "decay_lookback_bars": 5,
    "max_premium_decay_pct": 4.0,
    "block_spot_premium_divergence": True,
    "divergence_lookback_bars": 5,
    "min_spot_move_pct_for_divergence": 0.08,
    "max_premium_flat_pct": 0.45,
    "min_alignment_score": 35,
}


@dataclass
class Trade:
    tsym: str
    side: str
    strike: Decimal
    entry_ts: datetime
    exit_ts: datetime
    entry: Decimal
    exit: Decimal
    pnl: Decimal
    reason: str
    abs_delta: float
    setup_key: str
    entry_spot: Decimal = Decimal("0")
    mfe: Decimal = Decimal("0")
    theta_pct: float = 0.0

    @property
    def hold_seconds(self) -> float:
        return max(0.0, (self.exit_ts - self.entry_ts).total_seconds())


def _row_candle(r) -> Candle:
    return Candle(
        instrument_token=str(r["instrument_token"]),
        ts=r["ts"].astimezone(IST) if r["ts"].tzinfo else r["ts"].replace(tzinfo=IST),
        open=Decimal(str(r["open"])),
        high=Decimal(str(r["high"])),
        low=Decimal(str(r["low"])),
        close=Decimal(str(r["close"])),
        volume=int(r["volume"] or 0),
        interval=CandleInterval.M1,
    )


def _candle_to_json(c: Candle) -> dict:
    return {
        "token": c.instrument_token,
        "ts": c.ts.isoformat(),
        "o": str(c.open),
        "h": str(c.high),
        "l": str(c.low),
        "c": str(c.close),
        "v": int(c.volume or 0),
        "oi": int(c.oi) if c.oi is not None else None,
    }


def _candle_from_json(d: dict) -> Candle:
    ts = datetime.fromisoformat(d["ts"])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    return Candle(
        instrument_token=str(d["token"]),
        ts=ts.astimezone(IST),
        open=Decimal(d["o"]),
        high=Decimal(d["h"]),
        low=Decimal(d["l"]),
        close=Decimal(d["c"]),
        volume=int(d.get("v") or 0),
        oi=int(d["oi"]) if d.get("oi") is not None else None,
        interval=CandleInterval.M1,
    )


def _closed_before(bars: list[Candle], when: datetime) -> list[Candle]:
    return [c for c in bars if c.ts + timedelta(minutes=1) <= when]


def _bar_at(bars: list[Candle], when: datetime) -> Candle | None:
    for c in bars:
        if c.ts <= when < c.ts + timedelta(minutes=1):
            return c
    return None


def _bar_path(bar: Candle, when: datetime) -> Decimal:
    """Open → mid → close path inside the minute (same idea as live replay)."""
    end = bar.ts + timedelta(minutes=1)
    frac = max(0.0, min(1.0, (when - bar.ts).total_seconds() / 60.0))
    if frac < 0.33:
        return bar.open
    if frac < 0.66:
        return (bar.high + bar.low) / Decimal("2")
    return bar.close


def _to_spot_bars(m1: list[Candle], tf: int) -> list[SpotBar]:
    agg = aggregate_from_m1(m1, tf)
    return [
        SpotBar(
            ts=c.ts.astimezone(IST),
            o=float(c.open),
            h=float(c.high),
            l=float(c.low),
            c=float(c.close),
            v=float(c.volume or 0),
        )
        for c in agg
    ]


async def _load_index() -> list[Candle]:
    pool = get_pool()
    start = datetime.combine(DAY, time(9, 15), tzinfo=IST).astimezone(timezone.utc)
    end = datetime.combine(DAY, time(15, 30), tzinfo=IST).astimezone(timezone.utc)
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
          AND strike BETWEEN 22700 AND 23300
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
                lot_size=int(r["lot_size"] or LOT),
            )
        )
    return out


async def _fetch_options(instruments: list[Instrument], start: datetime, end: datetime) -> dict[str, list[Candle]]:
    if ARCHIVE.exists():
        payload = json.loads(ARCHIVE.read_text())
        series = {tok: [_candle_from_json(x) for x in rows] for tok, rows in payload.get("options", {}).items()}
        if series:
            print(f"==> Option bars from archive ({len(series)} contracts) {ARCHIVE}")
            return series

    print("==> Fetching option 1m bars from FlatTrade (morning → close)...")
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
        oi=None,
    )


def _exit_sim(
    series: list[Candle],
    entry_ts: datetime,
    entry_px: Decimal,
    *,
    session_end: datetime | None = None,
    hard_sl_pct: Decimal = Decimal("0.05"),
    trail_lock_pct: Decimal = Decimal("0.55"),
) -> tuple[datetime, Decimal, str, Decimal]:
    """Two strategy exits only:

    1) Hard SL: ~5% against entry.
    2) Profit-side trailing only (lock share of peak profit). No timed exit.

    Flatten at session_end (15:15) only if still open — book close, not a strategy timer.
    """
    hard_sl = entry_px * (Decimal("1") - hard_sl_pct)
    end = session_end or (
        entry_ts.astimezone(IST).replace(hour=15, minute=15, second=0, microsecond=0)
    )
    t = entry_ts + timedelta(seconds=15)
    mfe = entry_px
    last_px = entry_px
    while t <= end:
        bar = _bar_at(series, t)
        if bar is None:
            t += timedelta(seconds=15)
            continue
        px = _bar_path(bar, t)
        last_px = px
        mfe = max(mfe, px)

        if px <= hard_sl:
            return t, px, "hard_sl_5pct", mfe - entry_px

        if mfe > entry_px:
            trail_floor = max(entry_px, entry_px + (mfe - entry_px) * trail_lock_pct)
            if px <= trail_floor:
                return t, px, "profit_trail", mfe - entry_px

        t += timedelta(seconds=15)
    bar = _bar_at(series, end) or (series[-1] if series else None)
    px = _bar_path(bar, end) if bar else last_px
    return end, px, "session_end", mfe - entry_px


def _run_session(
    *,
    index: list[Candle],
    instruments: list[Instrument],
    options: dict[str, list[Candle]],
    lift_cfg: dict,
    quality_cfg: dict,
    label: str,
) -> tuple[list[Trade], dict[str, int], int, int, dict[int, int]]:
    """Walk the full day once under a quality cfg. Returns trades + skip stats."""
    by_token = {i.token: i for i in instruments}
    confirm_scans = int(lift_cfg.get("confirm_scans", 1))
    streaks: dict[str, int] = {}
    sent: set[str] = set()
    skips: dict[str, int] = {}
    candidates_seen = 0
    quality_pass = 0
    trades: list[Trade] = []
    last_setup_key: str | None = None
    open_until: datetime | None = None
    hourly_pass: dict[int, int] = {}

    print(f"\n---- running: {label} ----")
    for bar in index:
        when = bar.ts + timedelta(minutes=1)
        ist = when.astimezone(IST)
        if ist.time() < time(9, 25) or ist.time() > time(15, 15):
            continue

        closed_idx = _closed_before(index, when)
        if len(closed_idx) < 20:
            continue
        spot = bar.close
        step = Decimal("50")
        atm = (spot / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step

        from algoflat.contract_selector.expiry import format_expiry_tag
        from algoflat.models.events import FeatureSnapshot

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

        features = FeatureSnapshot(
            ts=when,
            nifty_spot=spot,
            session_vwap=session_vwap(closed_idx),
            extra={"cash_index_spot": float(spot)},
        )
        _sig, note = find_option_lifts(
            features=features,
            universe=universe,
            band_states=band_states,
            option_m1_by_token=option_m1,
            spot_m1=closed_idx + [bar],
            cfg=lift_cfg,
            strategy_version="quality_setup_first",
            when=when,
        )
        found = list(note.get("_signals") or [])
        if not found:
            continue
        eligible = [
            sig
            for sig in found
            if f"{sig.instrument_token}:{sig.scanner_metadata.get('bar_ts')}" not in sent
        ]
        ready, _waiting, streaks = split_confirmed_lifts(
            eligible, streaks, required=confirm_scans
        )
        if not ready:
            continue

        bars5 = _to_spot_bars(closed_idx, 5)
        bars15 = _to_spot_bars(closed_idx, 15)

        for sig in ready:
            candidates_seen += 1
            inst = by_token.get(sig.instrument_token)
            state = band_states.get(sig.instrument_token)
            series = options.get(sig.instrument_token) or []
            if inst is None or state is None or state.ltp is None:
                skips["no_state"] = skips.get("no_state", 0) + 1
                continue

            chart = analyze_option_chart(
                side=sig.side,
                option_m1=_closed_before(series, when),
                spot_m1=closed_idx,
                when=when,
                cfg=OPTION_CHART_CFG,
            )
            if int(chart.get("score") or 0) < int(OPTION_CHART_CFG["min_alignment_score"]):
                skips["option_chart_weak"] = skips.get("option_chart_weak", 0) + 1
                continue

            opt_vwap = chart.get("premium_vwap")
            g = compute_greeks(
                spot=float(spot),
                strike=float(inst.strike),
                premium=float(state.ltp),
                option_type=sig.side,
                expiry=EXPIRY,
                now=when,
            )
            cand = PipelineCandidate(
                ts=when.astimezone(IST),
                side=sig.side,
                premium=float(state.ltp),
                strike=float(inst.strike),
                spot=float(spot),
                expiry=EXPIRY,
                option_vwap=float(opt_vwap) if opt_vwap is not None else None,
                delta=g.delta,
                theta=g.theta,
                tsym=sig.tsym,
            )
            pipe = run_quality_pipeline(
                cand, bars5=bars5, bars15=bars15, cfg=quality_cfg, pdh=PDH, pdl=PDL
            )
            if not pipe.accepted:
                stage = pipe.reject_stage or "pipeline"
                skips[f"q_{stage}"] = skips.get(f"q_{stage}", 0) + 1
                for r in pipe.reasons[:1]:
                    skips[r] = skips.get(r, 0) + 1
                continue

            quality_pass += 1
            hourly_pass[ist.hour] = hourly_pass.get(ist.hour, 0) + 1
            setup_key = (
                f"{bars5[-1].ts.isoformat() if bars5 else when.isoformat()}|{sig.side}"
            )

            if open_until and when < open_until:
                skips["busy_in_trade"] = skips.get("busy_in_trade", 0) + 1
                continue
            if setup_key == last_setup_key:
                skips["one_per_setup"] = skips.get("one_per_setup", 0) + 1
                continue

            bar_key = f"{sig.instrument_token}:{sig.scanner_metadata.get('bar_ts')}"
            sent.add(bar_key)
            entry_px = state.ltp
            exit_ts, exit_px, reason, mfe_pts = _exit_sim(series, when, entry_px)
            pnl = (exit_px - entry_px) * Decimal(LOT)
            th_day = float(g.theta or 0)
            if abs(th_day) > 5:
                th_day = th_day / 365.0
            th_pct = abs(th_day) / float(entry_px) * 100.0 if entry_px > 0 else 0.0
            trades.append(
                Trade(
                    tsym=sig.tsym,
                    side=sig.side,
                    strike=inst.strike,
                    entry_ts=when.astimezone(IST),
                    exit_ts=exit_ts.astimezone(IST),
                    entry=entry_px,
                    exit=exit_px,
                    pnl=pnl,
                    reason=reason,
                    abs_delta=abs(float(g.delta or 0)),
                    setup_key=setup_key,
                    entry_spot=spot,
                    mfe=mfe_pts,
                    theta_pct=th_pct,
                )
            )
            last_setup_key = setup_key
            open_until = exit_ts
            hold_s = (exit_ts - when).total_seconds()
            print(
                f"  #{len(trades):02d} ENTRY {ist.strftime('%H:%M:%S')}  "
                f"EXIT {exit_ts.astimezone(IST).strftime('%H:%M:%S')}  "
                f"hold={hold_s:.0f}s  {sig.side} {sig.tsym}  "
                f"spot={spot:.1f}  in=₹{entry_px} out=₹{exit_px}  "
                f"Δ={abs(float(g.delta or 0)):.2f}  pnl=₹{pnl:.0f}  ({reason})"
            )
            break

        if ist.minute == 0:
            print(f"  … {ist.strftime('%H:%M')} spot={spot} trades_so_far={len(trades)}")

    return trades, skips, candidates_seen, quality_pass, hourly_pass


def _live(rows: list[Trade]) -> Decimal:
    return sum((t.pnl for t in rows), Decimal("0")) - COST_PER_TRADE * len(rows)


def _summarize(label: str, rows: list[Trade]) -> None:
    wins = [t for t in rows if t.pnl > 0]
    total = sum((t.pnl for t in rows), Decimal("0"))
    print(f"\n==> {label}")
    print(
        f"  trades={len(rows)}  wins={len(wins)}  "
        f"wr={100 * len(wins) / len(rows) if rows else 0:.1f}%"
    )
    print(f"  paper=₹{total:.0f}  live_est=₹{_live(rows):.0f}")
    if rows:
        avg_hold = sum(t.hold_seconds for t in rows) / len(rows)
        print(
            f"  avg_hold={avg_hold:.0f}s  "
            f"avg|Δ|={sum(t.abs_delta for t in rows) / len(rows):.2f}"
        )


def _print_trades(rows: list[Trade]) -> None:
    print(
        f"  {'#':>2}  {'entry_ist':<12}  {'exit_ist':<12}  {'hold':>6}  "
        f"{'side':<3}  {'tsym':<20}  {'spot':>8}  {'in':>7}  {'out':>7}  "
        f"{'Δ':>5}  {'pnl':>8}  reason"
    )
    for i, t in enumerate(rows, 1):
        print(
            f"  {i:02d}  {t.entry_ts.strftime('%H:%M:%S'):<12}  "
            f"{t.exit_ts.strftime('%H:%M:%S'):<12}  "
            f"{t.hold_seconds:5.0f}s  {t.side:<3}  {t.tsym:<20}  "
            f"{float(t.entry_spot):8.1f}  {float(t.entry):7.2f}  "
            f"{float(t.exit):7.2f}  {t.abs_delta:5.2f}  "
            f"{float(t.pnl):+8.0f}  {t.reason}"
        )


async def main() -> None:
    await init_pool()
    index = await _load_index()
    instruments = await _load_instruments()
    print(
        f"==> Index 1m bars={len(index)}  "
        f"first={index[0].ts.astimezone(IST)}  last={index[-1].ts.astimezone(IST)}"
    )
    print(f"==> Instruments in band={len(instruments)}")

    start = datetime.combine(DAY, time(9, 15), tzinfo=IST)
    end = datetime.combine(DAY, time(15, 30), tzinfo=IST)
    options = await _fetch_options(
        instruments, start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    )

    lift_cfg = dict(get_config().strategy.get("option_lift") or {})
    lift_cfg["confirm_scans"] = 1
    lift_cfg["min_index_move_points"] = 4
    if isinstance(lift_cfg.get("opposite_trade_today"), dict):
        lift_cfg["opposite_trade_today"] = {
            **lift_cfg["opposite_trade_today"],
            "enabled": False,
        }

    cfg_asis = dict(QUALITY_CFG)
    cfg_asis["enforce_min_abs_delta"] = False

    cfg_d25 = dict(QUALITY_CFG)
    cfg_d25["enforce_min_abs_delta"] = True
    cfg_d25["min_abs_delta"] = 0.25

    cfg_d30 = dict(QUALITY_CFG)
    cfg_d30["enforce_min_abs_delta"] = True
    cfg_d30["min_abs_delta"] = 0.30

    trades, skips, cand_n, pass_n, hourly = _run_session(
        index=index,
        instruments=instruments,
        options=options,
        lift_cfg=lift_cfg,
        quality_cfg=cfg_asis,
        label="as-is (min Δ not enforced)",
    )
    trades25, skips25, _, pass25, hourly25 = _run_session(
        index=index,
        instruments=instruments,
        options=options,
        lift_cfg=lift_cfg,
        quality_cfg=cfg_d25,
        label="enforce min|Δ|≥0.25",
    )
    trades30, _, _, pass30, _ = _run_session(
        index=index,
        instruments=instruments,
        options=options,
        lift_cfg=lift_cfg,
        quality_cfg=cfg_d30,
        label="enforce min|Δ|≥0.30",
    )

    _summarize("FULL DAY as-is (min Δ not enforced)", trades)
    print(f"  lift_candidates={cand_n}  quality_passes={pass_n}")
    print(f"  passes by hour: {dict(sorted(hourly.items()))}")
    print("\n  Trade blotter (as-is) — entry/exit times + hold:")
    _print_trades(trades)

    _summarize("FULL DAY with min|Δ|≥0.25 enforced", trades25)
    print(f"  quality_passes={pass25}  passes by hour: {dict(sorted(hourly25.items()))}")
    print("\n  Trade blotter (min|Δ|≥0.25):")
    _print_trades(trades25)

    _summarize("FULL DAY with min|Δ|≥0.30 enforced", trades30)
    print("\n  Trade blotter (min|Δ|≥0.30):")
    _print_trades(trades30)

    print("\n  Skip reasons as-is (top):")
    for k, v in sorted(skips.items(), key=lambda kv: -kv[1])[:10]:
        print(f"    {v:4d}  {k}")
    print("\n  Skip reasons min|Δ|≥0.25 (top):")
    for k, v in sorted(skips25.items(), key=lambda kv: -kv[1])[:10]:
        print(f"    {v:4d}  {k}")

    print("\n==> min|Δ|=0.25 fit check (true re-runs, not post-filter)")
    print(
        f"  as-is n={len(trades)} live≈₹{_live(trades):.0f}  |  "
        f"Δ≥0.25 n={len(trades25)} live≈₹{_live(trades25):.0f}  |  "
        f"Δ≥0.30 n={len(trades30)} live≈₹{_live(trades30):.0f}"
    )
    if len(trades25) >= 4 and _live(trades25) >= _live(trades) * Decimal("0.85"):
        print(
            "  Verdict: min|Δ|=0.25 looks like a GOOD FIT "
            "(enough trades, live PnL holds up)."
        )
    elif len(trades25) < 4:
        print("  Verdict: min|Δ|=0.25 too harsh today (too few trades).")
    elif _live(trades25) > _live(trades):
        print("  Verdict: min|Δ|=0.25 GOOD FIT — better live PnL by dropping weak Δ.")
    else:
        print("  Verdict: mixed — compare blotters before enabling.")

    out = Path("/tmp/setup_first_summary_2026-10-19.txt")
    with out.open("w") as f:
        f.write(
            f"as_is n={len(trades)} live={_live(trades):.0f}\n"
            f"min_d25 n={len(trades25)} live={_live(trades25):.0f}\n"
            f"min_d30 n={len(trades30)} live={_live(trades30):.0f}\n\n"
        )
        f.write("--- blotter as-is ---\n")
        for t in trades:
            f.write(
                f"{t.entry_ts.strftime('%H:%M:%S')} -> {t.exit_ts.strftime('%H:%M:%S')} "
                f"hold={t.hold_seconds:.0f}s {t.side} {t.tsym} spot={t.entry_spot} "
                f"in={t.entry} out={t.exit} delta={t.abs_delta:.3f} "
                f"pnl={t.pnl} {t.reason}\n"
            )
        f.write("\n--- blotter min_d25 ---\n")
        for t in trades25:
            f.write(
                f"{t.entry_ts.strftime('%H:%M:%S')} -> {t.exit_ts.strftime('%H:%M:%S')} "
                f"hold={t.hold_seconds:.0f}s {t.side} {t.tsym} spot={t.entry_spot} "
                f"in={t.entry} out={t.exit} delta={t.abs_delta:.3f} "
                f"pnl={t.pnl} {t.reason}\n"
            )
    print(f"\nWrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
