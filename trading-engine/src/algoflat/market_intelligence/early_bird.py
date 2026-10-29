"""Phase 35 — Early-bird expansion context (does NOT trigger entries)."""
from __future__ import annotations

from typing import Any

from algoflat.market_intelligence.context import MarketIntelligenceContext


def build_early_bird_context(
  ctx: MarketIntelligenceContext,
  *,
  spot: dict[str, Any],
  fut: dict[str, Any],
  breadth: dict[str, Any],
  sectors: dict[str, Any],
  cfg: dict,
) -> dict[str, Any]:
  flat_thr = float(cfg.get("spot_flat_pct", 0.08))
  vol_thr = float(cfg.get("relative_volume_min", 1.15))
  breadth_improve_thr = float(cfg.get("breadth_improve_min", 52))

  spot_mom = abs(float(spot.get("momentum_5m_pct") or 0))
  spot_flat = spot_mom <= flat_thr

  rel_vol = fut.get("futures_volume_strength")
  fut_strengthening = float(fut.get("futures_momentum_5m_pct") or 0) > 0.05

  adv = breadth.get("advance_pct")
  breadth_improving = adv is not None and float(adv) >= breadth_improve_thr

  sector_breadth = sectors.get("sector_breadth_pct")
  sector_improving = sector_breadth is not None and float(sector_breadth) >= breadth_improve_thr

  signals = sum(
    [
      spot_flat,
      fut_strengthening,
      breadth_improving,
      sector_improving,
      rel_vol is not None and float(rel_vol) >= vol_thr,
    ]
  )
  score = min(100, signals * 20)
  active = signals >= int(cfg.get("min_signals", 3))

  return {
    "available": True,
    "early_bird_score": score,
    "early_bird_active": active,
    "spot_relatively_flat": spot_flat,
    "fut_strengthening": fut_strengthening,
    "breadth_improving": breadth_improving,
    "sector_participation_improving": sector_improving,
    "relative_volume_elevated": rel_vol is not None and float(rel_vol) >= vol_thr,
    "contextual_only": True,
    "may_place_order": False,
  }
