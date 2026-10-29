"""Phase 13 — NIFTY vs breadth/sector divergence features."""
from __future__ import annotations

from typing import Any


def build_divergence_features(
  *,
  spot: dict[str, Any],
  breadth: dict[str, Any],
  sectors: dict[str, Any],
  cfg: dict,
) -> dict[str, Any]:
  nifty_mom = spot.get("momentum_5m_pct") or 0
  breadth_pct = breadth.get("advance_pct") or breadth.get("equal_weight_breadth")
  sector_breadth = sectors.get("sector_breadth_pct")

  hidden_strength_thr = float(cfg.get("hidden_strength_breadth_pct", 55))
  narrow_thr = float(cfg.get("narrow_rally_breadth_pct", 45))

  nifty_breadth_divergence = None
  nifty_sector_divergence = None
  if breadth_pct is not None:
    if abs(nifty_mom) < 0.05 and float(breadth_pct) >= hidden_strength_thr:
      nifty_breadth_divergence = "hidden_strength"
    elif nifty_mom > 0.1 and float(breadth_pct) < narrow_thr:
      nifty_breadth_divergence = "narrow_rally"
    else:
      nifty_breadth_divergence = "aligned"

  if sector_breadth is not None:
    if nifty_mom > 0.1 and float(sector_breadth) < narrow_thr:
      nifty_sector_divergence = "weak_participation"
  elif sector_breadth is not None and nifty_mom < -0.05 and float(sector_breadth) > hidden_strength_thr:
    nifty_sector_divergence = "hidden_strength"

  align = "neutral"
  if nifty_breadth_divergence == "aligned":
    align = "aligned"
  elif nifty_breadth_divergence in ("hidden_strength", "narrow_rally"):
    align = "divergent"

  ew = breadth.get("equal_weight_breadth")
  ww = breadth.get("weighted_breadth")
  breadth_weight_divergence = round(float(ew) - float(ww), 2) if ew is not None and ww is not None else None

  return {
    "nifty_breadth_divergence": nifty_breadth_divergence,
    "nifty_sector_divergence": nifty_sector_divergence,
    "breadth_price_alignment": align,
    "breadth_acceleration": breadth.get("breadth_momentum"),
    "equal_weight_breadth": ew,
    "weighted_breadth": ww,
    "breadth_weight_divergence": breadth_weight_divergence,
  }
