#!/usr/bin/env python3
"""Offline full-day setup-first backtest (no Postgres / FlatTrade).

Uses local option archive + 5m Nifty CSV (expanded to 1m). Exit rules:
  1) Hard SL ~5% against entry
  2) Profit-side trailing only (no fixed target)
"""
from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "trading-engine" / "src"
sys.path.insert(0, str(SRC))

# Avoid algoflat.trading.__init__ pulling the full orchestrator on import.
import types

_trading = types.ModuleType("algoflat.trading")
_trading.__path__ = [str(SRC / "algoflat" / "trading")]  # type: ignore[attr-defined]
sys.modules["algoflat.trading"] = _trading

from algoflat.contract_selector.expiry import format_expiry_tag
from algoflat.contract_selector.selector import ContractUniverse
from algoflat.features.indicators import aggregate_from_m1
from algoflat.market_data.vwap import session_vwap
from algoflat.models.events import Candle, CandleInterval, FeatureSnapshot, Instrument, OptionState
from algoflat.option_data.greeks import compute_greeks
from algoflat.trading.option_lift import find_option_lifts, split_confirmed_lifts
from algoflat.validator.option_chart_confirm import analyze_option_chart
from algoflat.validator.quality_entry_pipeline import (
    PipelineCandidate,
    SpotBar,
    run_quality_pipeline,
)

IST = ZoneInfo("Asia/Kolkata")
# Usage: python ... [YYYY-MM-DD]   default 2026-10-19
DAY = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 10, 19)
# Filled in main() from archive / prior-day levels
EXPIRY = DAY
PDH, PDL = 0.0, 0.0
LOT = 65
COST = Decimal("80")
ARCHIVE = ROOT / f"data/archives/option_lift_{DAY.isoformat()}.json"
C5 = ROOT / f"reports/bt_{DAY.isoformat()}/candles_5m.csv"

# Prior-session high/low for S/R (cash index)
PRIOR_LEVELS: dict[str, tuple[float, float]] = {
    "2026-10-14": (22721.95, 22288.45),  # from 2026-10-13
    "2026-10-15": (22757.95, 22558.95),  # from 2026-10-14
    "2026-10-16": (23056.40, 22637.40),  # from 2026-10-15
    "2026-10-19": (23149.85, 22913.80),  # from 2026-10-16
}

# Token maps (union; load_instruments filters to tokens present in that day's archive)
INST_MAP: dict[str, tuple[str, int, str]] = {
    # NIFTY 19-Oct-2026 weeklies (used 2026-10-16 / 2026-10-19)
    "47143": ("CE", 22700, "NIFTY19OCT26C22700"),
    "47144": ("PE", 22700, "NIFTY19OCT26P22700"),
    "47149": ("CE", 22750, "NIFTY19OCT26C22750"),
    "47152": ("PE", 22750, "NIFTY19OCT26P22750"),
    "47153": ("CE", 22800, "NIFTY19OCT26C22800"),
    "47154": ("PE", 22800, "NIFTY19OCT26P22800"),
    "47155": ("CE", 22850, "NIFTY19OCT26C22850"),
    "47156": ("PE", 22850, "NIFTY19OCT26P22850"),
    "47157": ("CE", 22900, "NIFTY19OCT26C22900"),
    "47158": ("PE", 22900, "NIFTY19OCT26P22900"),
    "47159": ("CE", 22950, "NIFTY19OCT26C22950"),
    "47160": ("PE", 22950, "NIFTY19OCT26P22950"),
    "47161": ("CE", 23000, "NIFTY19OCT26C23000"),
    "47162": ("PE", 23000, "NIFTY19OCT26P23000"),
    "47164": ("CE", 23050, "NIFTY19OCT26C23050"),
    "47166": ("PE", 23050, "NIFTY19OCT26P23050"),
    "47167": ("CE", 23100, "NIFTY19OCT26C23100"),
    "47168": ("PE", 23100, "NIFTY19OCT26P23100"),
    "47169": ("CE", 23150, "NIFTY19OCT26C23150"),
    "47170": ("PE", 23150, "NIFTY19OCT26P23150"),
    "47171": ("CE", 23200, "NIFTY19OCT26C23200"),
    "47172": ("PE", 23200, "NIFTY19OCT26P23200"),
    "47173": ("CE", 23250, "NIFTY19OCT26C23250"),
    "47174": ("PE", 23250, "NIFTY19OCT26P23250"),
    "47175": ("CE", 23300, "NIFTY19OCT26C23300"),
    "47176": ("PE", 23300, "NIFTY19OCT26P23300"),
    "47177": ("CE", 23350, "NIFTY19OCT26C23350"),
    "47178": ("PE", 23350, "NIFTY19OCT26P23350"),
    "47179": ("CE", 23400, "NIFTY19OCT26C23400"),
    "47180": ("PE", 23400, "NIFTY19OCT26P23400"),
    # NIFTY 27-Oct-2026 monthlies (used 2026-10-13..15); low/high strikes inferred from premium↔spot)
    "51330": ("CE", 22500, "NIFTY27OCT26C22500"),
    "51331": ("PE", 22500, "NIFTY27OCT26P22500"),
    "51332": ("CE", 22550, "NIFTY27OCT26C22550"),
    "51333": ("PE", 22550, "NIFTY27OCT26P22550"),
    "51334": ("CE", 22600, "NIFTY27OCT26C22600"),
    "51337": ("PE", 22600, "NIFTY27OCT26P22600"),
    "51338": ("CE", 22650, "NIFTY27OCT26C22650"),
    "51339": ("PE", 22650, "NIFTY27OCT26P22650"),
    "51340": ("CE", 22700, "NIFTY27OCT26C22700"),
    "51341": ("PE", 22700, "NIFTY27OCT26P22700"),
    "51342": ("CE", 22750, "NIFTY27OCT26C22750"),
    "51343": ("PE", 22750, "NIFTY27OCT26P22750"),
    "51344": ("CE", 22800, "NIFTY27OCT26C22800"),
    "51345": ("PE", 22800, "NIFTY27OCT26P22800"),
    "51346": ("CE", 22850, "NIFTY27OCT26C22850"),
    "51347": ("PE", 22850, "NIFTY27OCT26P22850"),
    "51348": ("CE", 22900, "NIFTY27OCT26C22900"),
    "51349": ("PE", 22900, "NIFTY27OCT26P22900"),
    "51350": ("CE", 22950, "NIFTY27OCT26C22950"),
    "51351": ("PE", 22950, "NIFTY27OCT26P22950"),
    "51352": ("CE", 23000, "NIFTY27OCT26C23000"),
    "51353": ("PE", 23000, "NIFTY27OCT26P23000"),
    "51354": ("CE", 23050, "NIFTY27OCT26C23050"),
    "51355": ("PE", 23050, "NIFTY27OCT26P23050"),
    "51356": ("CE", 23100, "NIFTY27OCT26C23100"),
    "51357": ("PE", 23100, "NIFTY27OCT26P23100"),
    "51358": ("CE", 23150, "NIFTY27OCT26C23150"),
    "51359": ("PE", 23150, "NIFTY27OCT26P23150"),
    "51360": ("CE", 23200, "NIFTY27OCT26C23200"),
    "51361": ("PE", 23200, "NIFTY27OCT26P23200"),
    "51362": ("CE", 23250, "NIFTY27OCT26C23250"),
    "51363": ("PE", 23250, "NIFTY27OCT26P23250"),
    "51364": ("CE", 23300, "NIFTY27OCT26C23300"),
    "51365": ("PE", 23300, "NIFTY27OCT26P23300"),
}

QUALITY_CFG = {
    "enabled": True,
    "test_only": True,
    "min_abs_delta": 0.25,
    "max_abs_delta": 0.75,
    "enforce_min_abs_delta": True,  # agreed good-fit band floor
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

LIFT_CFG = {
    "atm_band_steps": 2,
    "strike_step": 50,
    "lookback_bars": 3,
    "volume_lookback_bars": 5,
    "min_closed_bars": 6,
    "min_premium_rise_pct": 0.75,
    "min_volume_ratio": 1.8,
    "min_bar_volume": 1000,
    "index_lookback_bars": 3,
    "min_index_move_points": 4,
    "confirm_scans": 1,
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
    entry_spot: Decimal
    mfe: Decimal

    @property
    def hold_seconds(self) -> float:
        return max(0.0, (self.exit_ts - self.entry_ts).total_seconds())


def _parse_ts(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00").replace(" ", "T", 1)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _candle_from_json(d: dict) -> Candle:
    ts = _parse_ts(d["ts"])
    return Candle(
        instrument_token=str(d["token"]),
        ts=ts,
        open=Decimal(d["o"]),
        high=Decimal(d["h"]),
        low=Decimal(d["l"]),
        close=Decimal(d["c"]),
        volume=int(d.get("v") or 0),
        oi=int(d["oi"]) if d.get("oi") is not None else None,
        interval=CandleInterval.M1,
    )


def load_index_1m_from_5m() -> list[Candle]:
    """Expand each 5m bar into five 1m bars (open→close path) for offline replay."""
    out: list[Candle] = []
    with C5.open() as f:
        for r in csv.DictReader(f):
            ts0 = _parse_ts(r["ts_ist"])
            o, h, l, c = map(Decimal, (r["open"], r["high"], r["low"], r["close"]))
            for i in range(5):
                ts = ts0 + timedelta(minutes=i)
                # path: open, mid-up, mid, mid-down, close
                if i == 0:
                    oo = o
                    cc = o + (c - o) * Decimal("0.25")
                elif i == 4:
                    oo = o + (c - o) * Decimal("0.75")
                    cc = c
                else:
                    frac = Decimal(i) / Decimal(5)
                    oo = o + (c - o) * frac
                    cc = o + (c - o) * (frac + Decimal("0.2"))
                hh = max(oo, cc, h if i in (1, 2) else max(oo, cc))
                ll = min(oo, cc, l if i in (2, 3) else min(oo, cc))
                out.append(
                    Candle(
                        instrument_token="26000",
                        ts=ts,
                        open=oo,
                        high=hh,
                        low=ll,
                        close=cc,
                        volume=0,
                        interval=CandleInterval.M1,
                    )
                )
    return out


def load_options() -> dict[str, list[Candle]]:
    payload = json.loads(ARCHIVE.read_text())
    return {
        tok: [_candle_from_json(x) for x in rows]
        for tok, rows in payload["options"].items()
    }


def load_instruments() -> list[Instrument]:
    out: list[Instrument] = []
    for token, (side, strike, tsym) in INST_MAP.items():
        out.append(
            Instrument(
                exchange="NFO",
                token=token,
                tsym=tsym,
                underlying="NIFTY",
                expiry_date=datetime.combine(EXPIRY, time(15, 30), tzinfo=IST),
                strike=Decimal(strike),
                option_type=side,
                lot_size=LOT,
            )
        )
    return out


def closed_before(bars: list[Candle], when: datetime) -> list[Candle]:
    return [c for c in bars if c.ts + timedelta(minutes=1) <= when]


def bar_at(bars: list[Candle], when: datetime) -> Candle | None:
    for c in bars:
        if c.ts <= when < c.ts + timedelta(minutes=1):
            return c
    return None


def bar_path(bar: Candle, when: datetime) -> Decimal:
    frac = max(0.0, min(1.0, (when - bar.ts).total_seconds() / 60.0))
    if frac < 0.33:
        return bar.open
    if frac < 0.66:
        return (bar.high + bar.low) / Decimal("2")
    return bar.close


def to_spot(m1: list[Candle], tf: int) -> list[SpotBar]:
    return [
        SpotBar(
            ts=c.ts.astimezone(IST),
            o=float(c.open),
            h=float(c.high),
            l=float(c.low),
            c=float(c.close),
            v=float(c.volume or 0),
        )
        for c in aggregate_from_m1(m1, tf)
    ]


def option_state(inst: Instrument, series: list[Candle], when: datetime) -> OptionState | None:
    bar = bar_at(series, when)
    if bar is None:
        return None
    ltp = bar_path(bar, when)
    if ltp <= 0:
        return None
    closed = closed_before(series, when)
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


def exit_sim(
    series: list[Candle],
    entry_ts: datetime,
    entry_px: Decimal,
    *,
    session_end: datetime | None = None,
    hard_sl_pct: Decimal = Decimal("0.05"),
    trail_lock_pct: Decimal = Decimal("0.55"),
) -> tuple[datetime, Decimal, str, Decimal]:
    """Only two strategy exits:
    1) ~5% hard SL
    2) profit-side trail (no fixed target, no timed exit)

    If still open at session_end (15:15 IST), flatten for the backtest book only.
    """
    hard_sl = entry_px * (Decimal("1") - hard_sl_pct)
    end = session_end or (
        entry_ts.astimezone(IST).replace(hour=15, minute=15, second=0, microsecond=0)
    )
    if end.tzinfo is None:
        end = end.replace(tzinfo=IST)
    t = entry_ts + timedelta(seconds=15)
    mfe = entry_px
    last_px = entry_px
    while t <= end:
        bar = bar_at(series, t)
        if bar is None:
            t += timedelta(seconds=15)
            continue
        px = bar_path(bar, t)
        last_px = px
        mfe = max(mfe, px)
        if px <= hard_sl:
            return t, px, "hard_sl_5pct", mfe - entry_px
        if mfe > entry_px:
            trail_floor = max(entry_px, entry_px + (mfe - entry_px) * trail_lock_pct)
            if px <= trail_floor:
                return t, px, "profit_trail", mfe - entry_px
        t += timedelta(seconds=15)
    bar = bar_at(series, end) or (series[-1] if series else None)
    px = bar_path(bar, end) if bar else last_px
    return end, px, "session_end", mfe - entry_px

def main() -> None:
    global EXPIRY, PDH, PDL
    if not ARCHIVE.exists():
        raise SystemExit(f"Missing option archive: {ARCHIVE}")
    if not C5.exists():
        raise SystemExit(f"Missing 5m candles: {C5}")
    payload_meta = json.loads(ARCHIVE.read_text())
    EXPIRY = date.fromisoformat(str(payload_meta.get("expiry") or DAY.isoformat()))
    if DAY.isoformat() not in PRIOR_LEVELS:
        raise SystemExit(f"Add PRIOR_LEVELS for {DAY}")
    PDH, PDL = PRIOR_LEVELS[DAY.isoformat()]

    index = load_index_1m_from_5m()
    options = load_options()
    # Only instruments present in the archive
    instruments = [i for i in load_instruments() if i.token in options]
    by_token = {i.token: i for i in instruments}
    print(
        f"==> Offline BT {DAY.isoformat()}  expiry={EXPIRY}  "
        f"PDH={PDH} PDL={PDL}  index_1m={len(index)}  options={len(options)}  "
        f"instruments={len(instruments)}"
    )
    print("==> Exit: hard SL -5% OR profit-side trail only (flatten 15:15 if still open)")
    print("==> Entry: lift → greeks (min|Δ|≥0.25) → option chart → Nifty veto")

    streaks: dict[str, int] = {}
    sent: set[str] = set()
    skips: dict[str, int] = {}
    trades: list[Trade] = []
    last_setup_key: str | None = None
    open_until: datetime | None = None
    candidates = 0
    qpass = 0
    session_end = datetime.combine(DAY, time(15, 15), tzinfo=IST)

    for bar in index:
        when = bar.ts + timedelta(minutes=1)
        ist = when.astimezone(IST)
        if ist.time() < time(9, 25) or ist.time() > time(15, 15):
            continue
        closed_idx = closed_before(index, when)
        if len(closed_idx) < 20:
            continue
        spot = bar.close
        step = Decimal("50")
        atm = (spot / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
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
            band_states[inst.token] = option_state(inst, series, when)

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
            cfg=LIFT_CFG,
            strategy_version="offline_quality",
            when=when,
        )
        found = list(note.get("_signals") or [])
        if not found:
            continue
        eligible = [
            s
            for s in found
            if f"{s.instrument_token}:{s.scanner_metadata.get('bar_ts')}" not in sent
        ]
        ready, _, streaks = split_confirmed_lifts(eligible, streaks, required=1)
        if not ready:
            continue

        bars5 = to_spot(closed_idx, 5)
        bars15 = to_spot(closed_idx, 15)
        for sig in ready:
            candidates += 1
            inst = by_token.get(sig.instrument_token)
            state = band_states.get(sig.instrument_token)
            series = options.get(sig.instrument_token) or []
            if not inst or not state or state.ltp is None:
                continue
            chart = analyze_option_chart(
                side=sig.side,
                option_m1=closed_before(series, when),
                spot_m1=closed_idx,
                when=when,
                cfg=OPTION_CHART_CFG,
            )
            if int(chart.get("score") or 0) < 35:
                skips["option_chart_weak"] = skips.get("option_chart_weak", 0) + 1
                continue
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
                option_vwap=float(chart["premium_vwap"]) if chart.get("premium_vwap") else None,
                delta=g.delta,
                theta=g.theta,
                tsym=sig.tsym,
            )
            pipe = run_quality_pipeline(
                cand, bars5=bars5, bars15=bars15, cfg=QUALITY_CFG, pdh=PDH, pdl=PDL
            )
            if not pipe.accepted:
                skips[pipe.reject_stage or "q"] = skips.get(pipe.reject_stage or "q", 0) + 1
                continue
            qpass += 1
            setup_key = f"{bars5[-1].ts.isoformat() if bars5 else when.isoformat()}|{sig.side}"
            if open_until and when < open_until:
                skips["busy"] = skips.get("busy", 0) + 1
                continue
            if setup_key == last_setup_key:
                skips["one_per_setup"] = skips.get("one_per_setup", 0) + 1
                continue
            sent.add(f"{sig.instrument_token}:{sig.scanner_metadata.get('bar_ts')}")
            entry_px = state.ltp
            exit_ts, exit_px, reason, mfe = exit_sim(
                series, when, entry_px, session_end=session_end
            )
            pnl = (exit_px - entry_px) * Decimal(LOT)
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
                    entry_spot=spot,
                    mfe=mfe,
                )
            )
            last_setup_key = setup_key
            open_until = exit_ts
            print(
                f"  #{len(trades):02d} ENTRY {ist.strftime('%H:%M:%S')}  "
                f"EXIT {exit_ts.astimezone(IST).strftime('%H:%M:%S')}  "
                f"hold={(exit_ts-when).total_seconds():.0f}s  {sig.side} {sig.tsym}  "
                f"spot={float(spot):.1f}  in=₹{entry_px} out=₹{exit_px}  "
                f"Δ={abs(float(g.delta or 0)):.2f}  pnl=₹{pnl:.0f}  ({reason})"
            )
            break

    wins = [t for t in trades if t.pnl > 0]
    total = sum((t.pnl for t in trades), Decimal("0"))
    live = total - COST * len(trades)
    print(f"\n==> FULL DAY {DAY.isoformat()} — exit: -5% SL | profit trail | 15:15 flatten")
    print(f"  candidates={candidates}  quality_passes={qpass}  trades={len(trades)}")
    print(f"  wins={len(wins)} wr={100*len(wins)/len(trades) if trades else 0:.1f}%")
    print(f"  paper=₹{total:.0f}  live_est=₹{live:.0f}")
    if trades:
        print(f"  avg_hold={sum(t.hold_seconds for t in trades)/len(trades):.0f}s")
    print(
        f"\n  {'#':>2}  {'entry':<10}  {'exit':<10}  {'hold':>6}  "
        f"{'side':<3}  {'tsym':<20}  {'spot':>8}  {'in':>7}  {'out':>7}  "
        f"{'Δ':>5}  {'pnl':>8}  reason"
    )
    for i, t in enumerate(trades, 1):
        print(
            f"  {i:02d}  {t.entry_ts.strftime('%H:%M:%S'):<10}  "
            f"{t.exit_ts.strftime('%H:%M:%S'):<10}  {t.hold_seconds:5.0f}s  "
            f"{t.side:<3}  {t.tsym:<20}  {float(t.entry_spot):8.1f}  "
            f"{float(t.entry):7.2f}  {float(t.exit):7.2f}  {t.abs_delta:5.2f}  "
            f"{float(t.pnl):+8.0f}  {t.reason}"
        )
    print("\n  Exit reason counts:")
    from collections import Counter
    for k, v in Counter(t.reason for t in trades).most_common():
        print(f"    {v}  {k}")
    print("\n  Skip top:")
    for k, v in sorted(skips.items(), key=lambda kv: -kv[1])[:10]:
        print(f"    {v:4d}  {k}")

    out_dir = ROOT / f"reports/bt_{DAY.isoformat()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "setup_first_exit_sl5_trail.txt"
    with out.open("w") as f:
        f.write(f"day={DAY} expiry={EXPIRY} trades={len(trades)} "
                f"paper={total:.0f} live={live:.0f}\n")
        f.write("exit=hard_sl_5pct | profit_trail_lock_55pct | session_end_15:15\n")
        for t in trades:
            f.write(
                f"{t.entry_ts.strftime('%H:%M:%S')}->{t.exit_ts.strftime('%H:%M:%S')} "
                f"hold={t.hold_seconds:.0f}s {t.side} {t.tsym} "
                f"in={t.entry} out={t.exit} delta={t.abs_delta:.3f} "
                f"pnl={t.pnl} {t.reason}\n"
            )
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
