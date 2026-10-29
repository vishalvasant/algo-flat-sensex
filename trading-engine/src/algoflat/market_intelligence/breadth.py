"""Phase 6/14 — Market breadth (sector-proxy when stock breadth unavailable)."""
from __future__ import annotations

from typing import Any

from algoflat.market_intelligence.context import MarketIntelligenceContext
from algoflat.market_intelligence.tick_meta import DataValidity


def build_breadth_features(
  ctx: MarketIntelligenceContext,
  *,
  sectors: dict[str, Any],
  cfg: dict,
) -> dict[str, Any]:
  # Stock-level NIFTY constituent breadth not available via Upstox in this build.
  stock_breadth_available = False
  if not stock_breadth_available and cfg.get("use_sector_proxy", True):
    pos = int(sectors.get("sectors_positive_count") or 0)
    neg = int(sectors.get("sectors_negative_count") or 0)
    unchanged = int(sectors.get("sectors_unchanged_count") or 0)
    total = pos + neg + unchanged
    adv_pct = round(pos / total * 100, 2) if total else None
    dec_pct = round(neg / total * 100, 2) if total else None
    ad_ratio = round(pos / neg, 3) if neg else None
    return {
      "available": sectors.get("available", False),
      "source": "sector_proxy",
      "stock_breadth_available": False,
      "advancing": pos,
      "declining": neg,
      "unchanged": unchanged,
      "advance_pct": adv_pct,
      "decline_pct": dec_pct,
      "advance_decline_ratio": ad_ratio,
      "equal_weight_breadth": adv_pct,
      "weighted_breadth": sectors.get("sector_breadth_pct"),
      "breadth_momentum": sectors.get("sector_breadth_momentum"),
      "validity": DataValidity.OK.value if sectors.get("available") else DataValidity.MISSING.value,
    }

  return {
    "available": False,
    "stock_breadth_available": False,
    "validity": DataValidity.MISSING.value,
    "source": "unavailable",
  }
