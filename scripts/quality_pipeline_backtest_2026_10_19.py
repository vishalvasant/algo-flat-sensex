#!/usr/bin/env python3
"""Test-only backtest: option_lift → greeks → option chart → Nifty hard veto.

Uses today's closed paper fills + Nifty 5m/15m. Does NOT enable live trading.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
ENGINE_SRC = ROOT / "trading-engine" / "src"
sys.path.insert(0, str(ENGINE_SRC))

from algoflat.validator.quality_entry_pipeline import (  # noqa: E402
  PipelineCandidate,
  SpotBar,
  run_quality_pipeline,
)

IST = ZoneInfo("Asia/Kolkata")
C5 = ROOT / "reports/bt_2026-10-19/candles_5m.csv"
C15 = ROOT / "reports/bt_2026-10-19/candles_15m.csv"
TRADES = ROOT / "reports/bt_2026-10-19/closed_trades_full.csv"
ORDERBOOK = Path(
  "/Users/apple/.cursor/projects/Users-apple-Desktop-Server/attachments/"
  "04b9a51a-e101-4a7a-a7b4-20a3ff34ed09/algoflat_orderbook_2026-10-19_2026-10-19.csv"
)
PDH, PDL = 23149.85, 22913.80
COST = 80.0
EXPIRY = date(2026, 10, 19)  # weekly expiry day (dte=0 in snapshots)

# Delta band = existing option_entry_veto (0.30–0.75). Min not hard-enforced:
# live option_lift is outside option_entry_veto.apply_setups ("as is").
CFG = {
  "enabled": True,
  "test_only": True,
  "min_abs_delta": 0.30,
  "max_abs_delta": 0.75,
  "enforce_min_abs_delta": False,
  "max_daily_theta_pct_of_premium": 12.0,
  "min_premium": 5.0,
  "vwap_buffer_pct": 0.50,
  "require_premium_above_vwap": True,
  "level_near_pts": 30.0,
  "swing_lookback_5m": 12,
  "mid_allow_continuation": True,
}


def parse_ts(s: str, *, naive_as_ist: bool = False) -> datetime:
  s = s.strip().replace("Z", "+00:00")
  if "T" not in s and " " in s:
    s = s.replace(" ", "T", 1)
  # Python <3.11: fromisoformat needs 3 or 6 fractional digits
  m = re.match(
    r"^(.*T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:\d{2}|[+-]\d{4})?$",
    s,
  )
  if m:
    base, frac, off = m.group(1), m.group(2) or "", m.group(3) or ""
    if frac:
      digits = frac[1:]
      digits = (digits + "000000")[:6]
      frac = "." + digits
    if len(off) == 5 and (off[0] in "+-"):
      off = off[:3] + ":" + off[3:]
    s = base + frac + off
  dt = datetime.fromisoformat(s)
  if dt.tzinfo is None:
    dt = dt.replace(tzinfo=IST if naive_as_ist else timezone.utc)
  return dt.astimezone(IST)


def load_bars(path: Path) -> list[SpotBar]:
  out: list[SpotBar] = []
  with path.open() as f:
    for r in csv.DictReader(f):
      # CSV already exported as IST wall-clock without offset
      ts = parse_ts(r["ts_ist"], naive_as_ist=True)
      out.append(
        SpotBar(
          ts=ts,
          o=float(r["open"]),
          h=float(r["high"]),
          l=float(r["low"]),
          c=float(r["close"]),
          v=float(r["volume"] or 0),
        )
      )
  return out


def parse_tsym(tsym: str) -> tuple[str, float]:
  """NIFTY19OCT26P22950 → (PE, 22950)."""
  m = re.search(r"([CP])(\d{4,5})$", tsym.upper())
  if not m:
    raise ValueError(tsym)
  return ("CE" if m.group(1) == "C" else "PE"), float(m.group(2))


def merge_trades() -> list[dict]:
  obs = list(csv.DictReader(ORDERBOOK.open()))
  cts = list(csv.DictReader(TRADES.open()))
  used: set[int] = set()
  rows: list[dict] = []
  for ct in cts:
    snap = json.loads(ct["snap"])
    extra = snap.get("extra") or {}
    chain = extra.get("chain") or {}
    matched = None
    for i, o in enumerate(obs):
      if i in used:
        continue
      if (
        abs(float(o["entry_price"]) - float(ct["entry_price"])) < 1e-6
        and abs(float(o["pnl"]) - float(ct["pnl"])) < 1e-6
        and o["exit_reason"] == ct["exit_reason"]
      ):
        used.add(i)
        matched = o
        break
    if not matched:
      continue
    side, strike = parse_tsym(matched["tsym"])
    atm = chain.get("atm_ce") if side == "CE" else chain.get("atm_pe")
    atm = atm or {}
    spot = float(
      extra.get("cash_index_spot")
      or extra.get("fut_spot")
      or extra.get("level_anchor_spot")
      or 0
    )
    entry_px = float(ct["entry_price"])
    # Prefer contract option_vwap when it is in the same premium neighbourhood.
    raw_vwap = None
    if extra.get("option_vwap"):
      raw_vwap = float(extra["option_vwap"])
    elif atm.get("option_vwap") and abs(float(atm.get("strike") or 0) - strike) < 1.0:
      raw_vwap = float(atm["option_vwap"])
    rows.append(
      {
        "entry_ts": parse_ts(ct["entry_ts"]),
        "pnl": float(ct["pnl"]),
        "hold": float(ct["hold_seconds"]),
        "entry": entry_px,
        "exit": float(ct["exit_price"]),
        "side": side,
        "strike": strike,
        "tsym": matched["tsym"],
        "spot": spot,
        "option_vwap": raw_vwap,
        # Always derive greeks from fill premium (wings ≠ ATM snapshot).
        "delta": None,
        "theta": None,
        "exit_reason": ct["exit_reason"],
      }
    )
  rows.sort(key=lambda r: r["entry_ts"])
  return rows


def summarize(name: str, pnls: list[float]) -> None:
  n = len(pnls)
  if n == 0:
    print(f"  {name}: n=0")
    return
  wins = [p for p in pnls if p > 0]
  total = sum(pnls)
  live = total - n * COST
  print(
    f"  {name}: n={n} paper=₹{total:.0f} wr={100*len(wins)/n:.1f}% "
    f"avg=₹{total/n:.1f} live_est=₹{live:.0f}"
  )


def run_filter(trades, bars5, bars15, cfg):
  from datetime import timedelta

  kept: list[dict] = []
  stage_rej: Counter[str] = Counter()
  reason_rej: Counter[str] = Counter()
  for t in trades:
    spot = t["spot"]
    if not spot:
      closed = [b for b in bars5 if b.ts + timedelta(minutes=5) <= t["entry_ts"]]
      spot = closed[-1].c if closed else 0.0
    cand = PipelineCandidate(
      ts=t["entry_ts"],
      side=t["side"],
      premium=t["entry"],
      strike=t["strike"],
      spot=spot,
      expiry=EXPIRY,
      option_vwap=t["option_vwap"],
      delta=t["delta"],
      theta=t["theta"],
      tsym=t["tsym"],
    )
    res = run_quality_pipeline(cand, bars5=bars5, bars15=bars15, cfg=cfg, pdh=PDH, pdl=PDL)
    if res.accepted:
      kept.append({**t, "stages": res.stages})
    else:
      stage_rej[res.reject_stage or "unknown"] += 1
      for r in res.reasons:
        reason_rej[r] += 1

  one: list[dict] = []
  last_key = None
  for t in kept:
    closed = [b for b in bars5 if b.ts + timedelta(minutes=5) <= t["entry_ts"]]
    key = (closed[-1].ts.isoformat(), t["side"]) if closed else t["entry_ts"].isoformat()
    if key != last_key:
      one.append(t)
      last_key = key
  return kept, one, stage_rej, reason_rej


def main() -> None:
  bars5 = load_bars(C5)
  bars15 = load_bars(C15)
  trades = merge_trades()
  print(f"Loaded trades={len(trades)} bars5={len(bars5)} bars15={len(bars15)}")
  print("Pipeline: lift → greeks → option_chart → nifty_chart (hard veto) → LTP entry")
  print("(test-only — live quality_entry_pipeline.enabled=false)\n")

  base_pnls = [t["pnl"] for t in trades]
  kept, one, stage_rej, reason_rej = run_filter(trades, bars5, bars15, CFG)

  print("=== 2026-10-19 quality pipeline backtest (TEST ONLY) ===")
  print(
    f"Delta as-is: band={CFG['min_abs_delta']}-{CFG['max_abs_delta']} "
    f"(option_entry_veto), enforce_min={CFG['enforce_min_abs_delta']} "
    f"(option_lift not in apply_setups)"
  )
  summarize("A0 baseline option_lift", base_pnls)
  summarize("A1 all pipeline passes", [t["pnl"] for t in kept])
  summarize("A2 one fill per setup", [t["pnl"] for t in one])

  print("\nIf min-delta were hard-enforced (stricter than live option_lift):")
  for dmin, label in (
    (0.30, "option_entry min 0.30"),
    (0.25, "risk near_delta_min 0.25"),
  ):
    cfg = dict(CFG)
    cfg["enforce_min_abs_delta"] = True
    cfg["min_abs_delta"] = dmin
    _k, o, _, _ = run_filter(trades, bars5, bars15, cfg)
    summarize(f"enforce min≥{dmin:.2f} ({label})", [t["pnl"] for t in o])

  print("\nReject by stage (primary):")
  for k, v in stage_rej.most_common():
    print(f"  {v:4d}  {k}")
  print("\nTop reject reasons:")
  for k, v in reason_rej.most_common(12):
    print(f"  {v:4d}  {k}")

  # Funnel: how many clear each stage
  from datetime import date as _date
  from algoflat.validator.quality_entry_pipeline import (
    stage_greeks as _sg,
    stage_option_chart as _so,
    stage_nifty_chart as _sn,
  )

  g_ok = o_ok = n_ok = 0
  g_ok_pnls: list[float] = []
  go_pnls: list[float] = []
  for t in trades:
    spot = t["spot"] or 0.0
    cand = PipelineCandidate(
      ts=t["entry_ts"],
      side=t["side"],
      premium=t["entry"],
      strike=t["strike"],
      spot=spot,
      expiry=EXPIRY,
      option_vwap=t["option_vwap"],
    )
    g = _sg(cand, CFG)
    if not g.passed:
      continue
    g_ok += 1
    g_ok_pnls.append(t["pnl"])
    oc = _so(cand, CFG)
    if not oc.passed:
      continue
    o_ok += 1
    go_pnls.append(t["pnl"])
    nf = _sn(cand, bars5, bars15, CFG, pdh=PDH, pdl=PDL)
    if nf.passed:
      n_ok += 1

  print("\nFunnel (primary cfg):")
  print(f"  after greeks:       n={g_ok} paper=₹{sum(g_ok_pnls):.0f}")
  print(f"  after option_chart: n={o_ok} paper=₹{sum(go_pnls):.0f}")
  print(f"  after nifty veto:   n={n_ok} paper=₹{sum(t['pnl'] for t in kept):.0f}")

  print("\nAccepted (A2 primary):")
  for t in one:
    print(
      f"  {t['entry_ts'].strftime('%H:%M:%S')} {t['side']} {t['tsym']} "
      f"entry={t['entry']:.2f} pnl=₹{t['pnl']:.0f} hold={t['hold']:.0f}s"
    )
  if not one:
    print("  (none — Nifty hard veto cleared the greeks survivors on this grind-down day)")

  out = ROOT / "reports/bt_2026-10-19/quality_pipeline_summary.txt"
  with out.open("w") as f:
    f.write(f"baseline_n={len(trades)} baseline_pnl={sum(base_pnls):.0f}\n")
    f.write(f"pipeline_n={len(kept)} pipeline_pnl={sum(t['pnl'] for t in kept):.0f}\n")
    f.write(f"one_per_setup_n={len(one)} one_per_setup_pnl={sum(t['pnl'] for t in one):.0f}\n")
    f.write(f"live_est_one=₹{sum(t['pnl'] for t in one) - len(one)*COST:.0f}\n")
    f.write(
      f"delta_as_is band={CFG['min_abs_delta']}-{CFG['max_abs_delta']} "
      f"enforce_min={CFG['enforce_min_abs_delta']}\n"
    )
  print(f"\nWrote {out}")


if __name__ == "__main__":
  main()
