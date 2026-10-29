"""Phase 8 — Global / pre-market context (VIX + optional gift symbol)."""
from __future__ import annotations

from typing import Any

from algoflat.market_intelligence.context import MarketIntelligenceContext
from algoflat.market_intelligence.tick_meta import DataValidity


def build_global_features(
  ctx: MarketIntelligenceContext,
  *,
  cfg: dict,
  vix_features: dict[str, Any] | None = None,
) -> dict[str, Any]:
  gift_sym = str(cfg.get("gift_symbol") or "").strip().upper()
  gift_ltp = ctx.index_ltps.get(gift_sym) if gift_sym else None
  gift_open = ctx.index_opens.get(gift_sym) if gift_sym else None
  gift_chg = None
  gift_gap_pct = None
  if gift_ltp is not None and gift_open is not None and float(gift_open) > 0:
    gift_chg = round(float(gift_ltp) - float(gift_open), 2)
    gift_gap_pct = round(gift_chg / float(gift_open) * 100, 4)

  # US / Asia indices not wired in this provider build — mark unavailable per spec §43.
  us_available = False
  asia_available = False

  score = 0.0
  parts = 0
  weight_gift = float(cfg.get("weight_gift", 0) or 0)
  if gift_gap_pct is not None and weight_gift:
    score += max(-1.0, min(1.0, gift_gap_pct / 0.5)) * weight_gift
    parts += 1

  vix = vix_features or {}
  if not vix:
    vix_level = ctx.index_ltps.get("INDIAVIX")
    if vix_level is not None:
      if float(vix_level) > 16:
        score -= 0.2 * float(cfg.get("weight_vix", 0.3))
      elif float(vix_level) < 12:
        score += 0.1 * float(cfg.get("weight_vix", 0.3))
      parts += 1
  elif vix.get("available"):
    vol_regime = vix.get("volatility_regime")
    if vol_regime == "expansion":
      score -= 0.2 * float(cfg.get("weight_vix", 0.3))
    elif vol_regime == "compression":
      score += 0.1 * float(cfg.get("weight_vix", 0.3))
    parts += 1

  global_sentiment = round(score, 4) if parts else None
  available = gift_ltp is not None or bool(vix.get("available")) or (
    ctx.index_ltps.get("INDIAVIX") is not None
  )

  return {
    "available": available,
    "gift_nifty_ltp": float(gift_ltp) if gift_ltp is not None else None,
    "gift_nifty_change": gift_chg,
    "gift_nifty_gap_pct": gift_gap_pct,
    "global_market_direction": (
      "bullish"
      if (gift_gap_pct or 0) > 0.15
      else ("bearish" if (gift_gap_pct or 0) < -0.15 else "neutral")
    ),
    "us_market_direction": "unavailable" if not us_available else None,
    "asian_market_direction": "unavailable" if not asia_available else None,
    "usd_inr_change": None,
    "crude_change": None,
    "global_sentiment_score": global_sentiment,
    "contextual_only": True,
    "validity": DataValidity.OK.value if available else DataValidity.MISSING.value,
  }
