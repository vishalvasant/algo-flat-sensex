"""Phase 36 — Reversal risk context (does NOT trigger PE entries)."""
from __future__ import annotations

from typing import Any

from algoflat.market_intelligence.context import MarketIntelligenceContext


def build_reversal_context(
  ctx: MarketIntelligenceContext,
  *,
  spot: dict[str, Any],
  fut: dict[str, Any],
  breadth: dict[str, Any],
  sectors: dict[str, Any],
  divergence: dict[str, Any],
  cfg: dict,
) -> dict[str, Any]:
  extra = ctx.features.extra or {}
  levels = extra.get("levels") or {}
  extension = extra.get("extension_state") or extra.get("distance_to_vwap_points")

  near_high = bool(levels.get("at_resistance")) or (
    extension is not None and float(extension) >= float(cfg.get("extension_pts", 25))
  )

  adv = breadth.get("advance_pct")
  breadth_weak = adv is not None and float(adv) < float(cfg.get("weak_breadth_pct", 45))

  sector_breadth = sectors.get("sector_breadth_pct")
  sectors_weak = sector_breadth is not None and float(sector_breadth) < float(cfg.get("weak_sector_pct", 45))

  fut_mom = float(fut.get("futures_momentum_5m_pct") or 0)
  fut_weakening = fut_mom < float(cfg.get("fut_weakening_pct", -0.05))

  div = divergence.get("nifty_breadth_divergence")
  narrow_rally = div == "narrow_rally"

  signals = sum([near_high, breadth_weak, sectors_weak, fut_weakening, narrow_rally])
  score = min(100, signals * 18)
  elevated = signals >= int(cfg.get("min_signals", 2))

  return {
    "available": True,
    "reversal_risk_score": score,
    "reversal_risk_elevated": elevated,
    "near_session_high": near_high,
    "breadth_deteriorating": breadth_weak,
    "sector_participation_weakening": sectors_weak,
    "fut_momentum_weakening": fut_weakening,
    "narrow_rally_divergence": narrow_rally,
    "contextual_only": True,
    "may_place_order": False,
  }
