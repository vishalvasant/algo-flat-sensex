"""Phase 7 — Sector participation from index LTPs."""
from __future__ import annotations

from typing import Any

from algoflat.market_intelligence.context import MarketIntelligenceContext
from algoflat.market_intelligence.tick_meta import DataValidity


def build_sector_features(ctx: MarketIntelligenceContext, *, sector_symbols: list[str]) -> dict[str, Any]:
  rows: list[dict[str, Any]] = []
  nifty_chg = None
  nifty_open = ctx.index_opens.get("NIFTY")
  nifty_ltp = ctx.index_ltps.get("NIFTY") or (ctx.features.extra or {}).get("cash_index_spot")
  if nifty_open and nifty_ltp and float(nifty_open) > 0:
    nifty_chg = (float(nifty_ltp) - float(nifty_open)) / float(nifty_open) * 100

  for sym in sector_symbols:
    ul = sym.upper()
    ltp = ctx.index_ltps.get(ul)
    open_px = ctx.index_opens.get(ul)
    if ltp is None:
      rows.append({"symbol": ul, "available": False})
      continue
    ret_pct = None
    if open_px is not None and float(open_px) > 0:
      ret_pct = round((float(ltp) - float(open_px)) / float(open_px) * 100, 4)
    rs = round(ret_pct - nifty_chg, 4) if ret_pct is not None and nifty_chg is not None else None
    trend = "up" if (ret_pct or 0) > 0.05 else ("down" if (ret_pct or 0) < -0.05 else "flat")
    rows.append(
      {
        "symbol": ul,
        "available": True,
        "ltp": float(ltp),
        "return_pct": ret_pct,
        "relative_strength_vs_nifty": rs,
        "trend_direction": trend,
      }
    )

  available_rows = [r for r in rows if r.get("available")]
  if not available_rows:
    return {"available": False, "sectors": rows, "validity": DataValidity.MISSING.value}

  flat_band = 0.05
  pos = sum(
    1
    for r in available_rows
    if r.get("return_pct") is not None and float(r["return_pct"]) > flat_band
  )
  neg = sum(
    1
    for r in available_rows
    if r.get("return_pct") is not None and float(r["return_pct"]) < -flat_band
  )
  unchanged = sum(
    1
    for r in available_rows
    if r.get("return_pct") is not None and abs(float(r["return_pct"])) <= flat_band
  )
  total = pos + neg + unchanged
  breadth_pct = round(pos / total * 100, 2) if total else None
  returns = [float(r["return_pct"]) for r in available_rows if r.get("return_pct") is not None]
  dispersion = round(max(returns) - min(returns), 4) if len(returns) >= 2 else None
  strength = round(sum(returns) / len(returns), 4) if returns else None

  return {
    "available": True,
    "sectors": rows,
    "sectors_positive_count": pos,
    "sectors_negative_count": neg,
    "sectors_unchanged_count": unchanged,
    "sector_breadth_pct": breadth_pct,
    "sector_strength": strength,
    "sector_dispersion": dispersion,
    "sector_breadth_momentum": strength,
    "validity": DataValidity.OK.value,
  }
