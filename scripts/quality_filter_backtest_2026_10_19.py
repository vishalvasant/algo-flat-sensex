#!/usr/bin/env python3
"""Replay 2026-10-19 option_lift under proposed quality gates.

Gates:
  1) 5m plan: last 3 closed 5m candles agree (bull/bear structure).
  2) Level-aware side: PE at resistance, CE at support (session + rolling swings).
  3) Rolling regime: last-3x5m bias agrees with last closed 15m (not day-open).

Two views:
  A) Filter today's 120 real fills through the gates (honest PnL on same exits).
  B) Setup-first: each armed 5m close opens a window; take first matching fill.
"""
from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[1]
C5 = ROOT / "reports/bt_2026-10-19/candles_5m.csv"
C15 = ROOT / "reports/bt_2026-10-19/candles_15m.csv"
TRADE_CSV = Path(
    "/Users/apple/.cursor/projects/Users-apple-Desktop-Server/attachments/"
    "04b9a51a-e101-4a7a-a7b4-20a3ff34ed09/algoflat_orderbook_2026-10-19_2026-10-19.csv"
)

PDH = 23149.85  # 2026-10-16
PDL = 22913.80
NEAR = 30.0  # pts to treat as "at" a level
SWING_LOOKBACK = 12  # ~1h of 5m bars for local S/R
COST_PER_TRADE = 80.0  # ~₹40/leg RT proxy


@dataclass
class Bar:
    ts: datetime
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass
class Trade:
    entry_ts: datetime
    exit_ts: datetime
    side: str  # BUY_CE / BUY_PE
    pnl: float
    entry: float
    exit: float
    hold_s: float
    reason: str


def parse_ts(s: str, *, naive_as_ist: bool = False) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    if "T" not in s and " " in s:
        s = s.replace(" ", "T")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST if naive_as_ist else timezone.utc)
    return dt.astimezone(IST)


def load_bars(path: Path) -> list[Bar]:
    rows = []
    with path.open() as f:
        for r in csv.DictReader(f):
            rows.append(
                Bar(
                    ts=parse_ts(r["ts_ist"] if "ts_ist" in r else r["ts"], naive_as_ist=True),
                    o=float(r["open"]),
                    h=float(r["high"]),
                    l=float(r["low"]),
                    c=float(r["close"]),
                    v=float(r["volume"] or 0),
                )
            )
    return rows


def infer_ce_pe(r: dict) -> str | None:
    sym = (r.get("tsym") or "").upper()
    m = re.search(r"([CP])(\d{4,5})$", sym)
    if m:
        return "BUY_CE" if m.group(1) == "C" else "BUY_PE"
    return None


def load_trades(path: Path) -> list[Trade]:
    out = []
    with path.open() as f:
        for r in csv.DictReader(f):
            if not r.get("pnl"):
                continue
            side = infer_ce_pe(r)
            if not side:
                continue
            entry_ts = parse_ts(r["entry_ts"])
            exit_ts = parse_ts(r["exit_ts"])
            out.append(
                Trade(
                    entry_ts=entry_ts,
                    exit_ts=exit_ts,
                    side=side,
                    pnl=float(r["pnl"]),
                    entry=float(r["entry_price"]),
                    exit=float(r["exit_price"]),
                    hold_s=float(r["hold_seconds"]),
                    reason=r.get("exit_reason") or "",
                )
            )
    out.sort(key=lambda t: t.entry_ts)
    return out


def closed_bars(bars: list[Bar], t: datetime, tf: int) -> list[Bar]:
    return [b for b in bars if b.ts + timedelta(minutes=tf) <= t]


def bias_3x5(bars: list[Bar]) -> str | None:
    """3-candle arm: all same color, or 2/3 with clear HH/HL or LH/LL structure."""
    if len(bars) < 3:
        return None
    w = bars[-3:]
    greens = sum(1 for b in w if b.c >= b.o)
    reds = 3 - greens
    net = w[-1].c - w[0].o
    hh = w[1].h >= w[0].h and w[2].h >= w[1].h
    hl = w[1].l >= w[0].l and w[2].l >= w[1].l
    lh = w[1].h <= w[0].h and w[2].h <= w[1].h
    ll = w[1].l <= w[0].l and w[2].l <= w[1].l
    if greens == 3 and net > 0:
        return "bull"
    if reds == 3 and net < 0:
        return "bear"
    if greens >= 2 and (hh or hl) and net > 0:
        return "bull"
    if reds >= 2 and (lh or ll) and net < 0:
        return "bear"
    return None


def regime(bars5: list[Bar], bars15: list[Bar], t: datetime) -> str | None:
    b5 = closed_bars(bars5, t, 5)
    b15 = closed_bars(bars15, t, 15)
    plan = bias_3x5(b5)
    if plan is None or len(b15) < 1:
        return None
    last = b15[-1]
    prior = b15[-2].c if len(b15) >= 2 else last.o
    if last.c > prior and last.c >= last.o:
        b15s = "bull"
    elif last.c < prior and last.c <= last.o:
        b15s = "bear"
    else:
        b15s = "bull" if last.c >= last.o else "bear"
    return plan if plan == b15s else None


def levels_at(bars5: list[Bar], t: datetime) -> dict:
    day = closed_bars(bars5, t, 5)
    day = [b for b in day if b.ts.date() == t.date()]
    if len(day) < 3:
        return {}
    sh = max(b.h for b in day)
    sl = min(b.l for b in day)
    look = day[-SWING_LOOKBACK:] if len(day) >= SWING_LOOKBACK else day
    swing_hi = max(b.h for b in look)
    swing_lo = min(b.l for b in look)
    # VWAP proxy
    num = den = 0.0
    for b in day:
        tp = (b.h + b.l + b.c) / 3
        vol = b.v if b.v > 0 else 1.0
        num += tp * vol
        den += vol
    vwap = num / den
    return {
        "session_high": sh,
        "session_low": sl,
        "swing_hi": swing_hi,
        "swing_lo": swing_lo,
        "vwap": vwap,
        "last": day[-1].c,
        "n": len(day),
    }


def zone_of(price: float, lv: dict) -> str:
    if not lv:
        return "unknown"
    res_levels = [lv["session_high"], lv["swing_hi"], PDH, lv["vwap"]]
    sup_levels = [lv["session_low"], lv["swing_lo"], PDL, lv["vwap"]]
    # distance to nearest res/sup (vwap counts for both; pick closer side)
    d_res = min(abs(price - x) for x in res_levels)
    d_sup = min(abs(price - x) for x in sup_levels)
    # prefer explicit near-hit
    near_res = d_res <= NEAR and price >= lv["vwap"] - NEAR
    near_sup = d_sup <= NEAR and price <= lv["vwap"] + NEAR
    # local swing: if within NEAR of swing_hi and above vwap → resistance
    if abs(price - lv["swing_hi"]) <= NEAR:
        return "resistance"
    if abs(price - lv["swing_lo"]) <= NEAR:
        return "support"
    if abs(price - lv["session_high"]) <= NEAR or abs(price - PDH) <= NEAR:
        return "resistance"
    if abs(price - lv["session_low"]) <= NEAR or abs(price - PDL) <= NEAR:
        return "support"
    # VWAP touch
    if abs(price - lv["vwap"]) <= NEAR:
        return "resistance" if price <= lv["vwap"] else "support"
    # relative position in rolling swing range (not full session — avoids permanent "support" on grind-down)
    rng = max(lv["swing_hi"] - lv["swing_lo"], 1.0)
    pos = (price - lv["swing_lo"]) / rng
    if pos >= 0.70:
        return "resistance"
    if pos <= 0.30:
        return "support"
    return "mid"


def allowed_sides(zone: str, reg: str | None) -> set[str]:
    if reg is None:
        return set()
    if zone == "resistance":
        return {"BUY_PE"} if reg == "bear" else set()
    if zone == "support":
        return {"BUY_CE"} if reg == "bull" else set()
    if zone == "mid":
        return {"BUY_CE"} if reg == "bull" else {"BUY_PE"}
    return set()


def evaluate(tr: Trade, bars5: list[Bar], bars15: list[Bar]) -> tuple[bool, str]:
    t = tr.entry_ts
    reg = regime(bars5, bars15, t)
    plan = bias_3x5(closed_bars(bars5, t, 5))
    lv = levels_at(bars5, t)
    zone = zone_of(lv.get("last", 0.0), lv)
    allow = allowed_sides(zone, reg)
    ok = tr.side in allow
    detail = (
        f"plan={plan} regime={reg} zone={zone} side={tr.side} "
        f"allow={sorted(allow)} px={lv.get('last', 0):.1f} "
        f"sw={lv.get('swing_lo', 0):.0f}-{lv.get('swing_hi', 0):.0f}"
    )
    return ok, ("pass " if ok else "reject ") + detail


def summarize(name: str, trades: list[Trade]) -> dict:
    if not trades:
        return {"name": name, "n": 0, "pnl": 0.0, "wr": 0.0, "avg": 0.0}
    wins = [t for t in trades if t.pnl > 0]
    pnl = sum(t.pnl for t in trades)
    return {
        "name": name,
        "n": len(trades),
        "pnl": pnl,
        "wr": 100.0 * len(wins) / len(trades),
        "avg": pnl / len(trades),
        "avg_hold_s": sum(t.hold_s for t in trades) / len(trades),
        "ce": sum(1 for t in trades if t.side == "BUY_CE"),
        "pe": sum(1 for t in trades if t.side == "BUY_PE"),
    }


def print_s(s: dict) -> None:
    live = s["pnl"] - s["n"] * COST_PER_TRADE
    extra = ""
    if s["n"]:
        extra = f" CE={s.get('ce',0)} PE={s.get('pe',0)} hold≈{s.get('avg_hold_s',0):.0f}s"
    print(
        f"  {s['name']}: n={s['n']} paper=₹{s['pnl']:.0f} wr={s['wr']:.1f}% "
        f"avg=₹{s['avg']:.1f} live_est=₹{live:.0f}{extra}"
    )


def setup_first(bars5: list[Bar], bars15: list[Bar], trades: list[Trade]) -> list[Trade]:
    """At each 5m close, if armed, take first matching fill in next 15 minutes."""
    picked: list[Trade] = []
    used_ids: set[int] = set()
    # iterate bar closes (skip first few)
    for i, b in enumerate(bars5):
        if i < 3:
            continue
        t_close = b.ts + timedelta(minutes=5)
        reg = regime(bars5, bars15, t_close)
        if reg is None:
            continue
        lv = levels_at(bars5, t_close)
        zone = zone_of(lv.get("last", 0.0), lv)
        allow = allowed_sides(zone, reg)
        if not allow:
            continue
        window_end = t_close + timedelta(minutes=15)
        for j, tr in enumerate(trades):
            if j in used_ids:
                continue
            if tr.entry_ts < t_close or tr.entry_ts > window_end:
                continue
            if tr.side in allow:
                picked.append(tr)
                used_ids.add(j)
                break
    return picked


def one_per_setup(kept: list[Trade], bars5: list[Bar]) -> list[Trade]:
    out: list[Trade] = []
    last_key = None
    for tr in kept:
        b5 = closed_bars(bars5, tr.entry_ts, 5)
        key = (b5[-1].ts.isoformat() if b5 else tr.entry_ts.isoformat(), bias_3x5(b5))
        if key != last_key:
            out.append(tr)
            last_key = key
    return out


def main() -> None:
    bars5 = load_bars(C5)
    bars15 = load_bars(C15)
    trades = load_trades(TRADE_CSV)
    print(f"Loaded trades={len(trades)} bars5={len(bars5)} bars15={len(bars15)}")
    print(f"Day spot: {bars5[0].c:.0f} → {bars5[-1].c:.0f}  PDH={PDH} PDL={PDL}")

    kept: list[Trade] = []
    rejected: list[str] = []
    for tr in trades:
        ok, why = evaluate(tr, bars5, bars15)
        if ok:
            kept.append(tr)
        else:
            rejected.append(why)

    sf = setup_first(bars5, bars15, trades)
    ops = one_per_setup(kept, bars5)

    print("\n=== 2026-10-19 backtest (new quality rules) ===")
    print_s(summarize("A0 baseline (all option_lift)", trades))
    print_s(summarize("A1 filter: all matching fills", kept))
    print_s(summarize("A2 filter: one trade per setup", ops))
    print_s(summarize("B  setup-first (1 fill / armed window)", sf))

    # armed windows count
    armed = 0
    armed_sides: Counter = Counter()
    for i, b in enumerate(bars5):
        if i < 3:
            continue
        t_close = b.ts + timedelta(minutes=5)
        reg = regime(bars5, bars15, t_close)
        if reg is None:
            continue
        lv = levels_at(bars5, t_close)
        zone = zone_of(lv.get("last", 0.0), lv)
        allow = allowed_sides(zone, reg)
        if allow:
            armed += 1
            armed_sides[",".join(sorted(allow)) + f"@{zone}"] += 1
    print(f"\nArmed 5m closes today: {armed}")
    for k, v in armed_sides.most_common():
        print(f"  {v:3d}  {k}")

    print("\nA2 kept trades:")
    for t in ops:
        print(f"  {t.entry_ts.strftime('%H:%M:%S')} {t.side} pnl=₹{t.pnl:.0f} hold={t.hold_s:.0f}s")

    print("\nB setup-first trades:")
    for t in sf:
        print(f"  {t.entry_ts.strftime('%H:%M:%S')} {t.side} pnl=₹{t.pnl:.0f} hold={t.hold_s:.0f}s")

    buckets = Counter()
    for w in rejected:
        parts = {p.split("=")[0]: p.split("=")[1] for p in w.split() if "=" in p}
        buckets[(parts.get("plan"), parts.get("regime"), parts.get("zone"), parts.get("side"))] += 1
    print("\nTop reject buckets:")
    for k, v in buckets.most_common(10):
        print(f"  {v:4d}  plan={k[0]} regime={k[1]} zone={k[2]} side={k[3]}")

    out = ROOT / "reports/bt_2026-10-19/quality_filter_summary.txt"
    with out.open("w") as f:
        for s in (
            summarize("baseline", trades),
            summarize("filter_all", kept),
            summarize("filter_one_per_setup", ops),
            summarize("setup_first", sf),
        ):
            live = s["pnl"] - s["n"] * COST_PER_TRADE
            f.write(
                f"{s['name']}: n={s['n']} paper=₹{s['pnl']:.0f} wr={s['wr']:.1f}% live_est=₹{live:.0f}\n"
            )
        f.write(f"armed_5m_closes={armed}\n")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
