"""Phase 3 — India VIX context features."""
from __future__ import annotations

from typing import Any

from algoflat.market_intelligence.context import MarketIntelligenceContext
from algoflat.market_intelligence.tick_meta import DataValidity, tick_meta


def build_vix_features(ctx: MarketIntelligenceContext, *, cfg: dict) -> dict[str, Any]:
  sym = str(cfg.get("symbol", "INDIAVIX")).upper()
  level = ctx.index_ltps.get(sym)
  open_px = ctx.index_opens.get(sym)
  if level is None:
    return {
      "available": False,
      "validity": DataValidity.MISSING.value,
      "volatility_regime": "unknown",
    }

  lvl = float(level)
  chg = None
  chg_pct = None
  if open_px is not None and float(open_px) > 0:
    chg = round(lvl - float(open_px), 4)
    chg_pct = round(chg / float(open_px) * 100, 4)

  elevated = float(cfg.get("elevated_level", 16.0))
  compressed = float(cfg.get("compressed_level", 12.0))
  if lvl >= elevated:
    vol_regime = "expansion"
    direction = "up" if (chg or 0) >= 0 else "down"
  elif lvl <= compressed:
    vol_regime = "compression"
    direction = "down" if (chg or 0) <= 0 else "up"
  else:
    vol_regime = "normal"
    direction = "up" if (chg or 0) > 0 else ("down" if (chg or 0) < 0 else "flat")

  return {
    "available": True,
    "vix_level": lvl,
    "vix_change": chg,
    "vix_change_pct": chg_pct,
    "vix_direction": direction,
    "vix_acceleration": chg_pct,
    "volatility_regime": vol_regime,
    "volatility_expansion": vol_regime == "expansion",
    "volatility_compression": vol_regime == "compression",
    "validity": DataValidity.OK.value,
    "tick": tick_meta(
      source="nse_index",
      instrument=sym,
      value=lvl,
      validity=DataValidity.OK,
    ).to_dict(),
  }
