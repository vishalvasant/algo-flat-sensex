"""Phase 1 — NIFTY spot derived features (not standalone signals)."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from algoflat.features.indicators import ema
from algoflat.market_intelligence.context import MarketIntelligenceContext
from algoflat.market_intelligence.tick_meta import DataValidity, tick_meta
from algoflat.models.events import Candle


def _momentum_pct(candles: list[Candle], lookback: int) -> float | None:
  if len(candles) < lookback + 1:
    return None
  start = float(candles[-lookback - 1].close)
  end = float(candles[-1].close)
  if start <= 0:
    return None
  return round((end - start) / start * 100, 4)


def _atr_approx(candles: list[Candle], lookback: int = 14) -> float | None:
  if len(candles) < 2:
    return None
  window = candles[-lookback:] if len(candles) >= lookback else candles
  trs: list[float] = []
  for i in range(1, len(window)):
    h = float(window[i].high)
    l = float(window[i].low)
    pc = float(window[i - 1].close)
    trs.append(max(h - l, abs(h - pc), abs(l - pc)))
  if not trs:
    return None
  return round(sum(trs) / len(trs), 2)


def build_spot_features(ctx: MarketIntelligenceContext) -> dict[str, Any]:
  feat = ctx.features
  extra = feat.extra or {}
  cash = extra.get("cash_index_spot") or extra.get("level_anchor_spot")
  vwap = feat.session_vwap
  m1 = ctx.m1
  m3 = ctx.m3
  m5 = ctx.m5

  closes = [c.close for c in m1]
  e9 = ema(closes, 9) if closes else None
  e21 = ema(closes, 21) if closes else None
  e50 = ema(closes, 50) if closes else None

  dist_vwap = extra.get("distance_to_vwap_points")
  vwap_dist_pct = None
  if cash is not None and vwap is not None and float(vwap) != 0:
    vwap_dist_pct = round((float(cash) - float(vwap)) / float(vwap) * 100, 4)

  session_high = max((float(c.high) for c in m5), default=None) if m5 else None
  session_low = min((float(c.low) for c in m5), default=None) if m5 else None

  return {
    "available": cash is not None,
    "spot_ltp": cash,
    "session_vwap": float(vwap) if vwap is not None else None,
    "spot_vs_vwap": extra.get("spot_vs_vwap"),
    "distance_to_vwap_points": dist_vwap,
    "vwap_distance_pct": vwap_dist_pct,
    "momentum_1m_pct": _momentum_pct(m1, 3),
    "momentum_3m_pct": _momentum_pct(m3, 3),
    "momentum_5m_pct": _momentum_pct(m5, 3),
    "structure_5m": extra.get("structure_5m"),
    "atr_1m": _atr_approx(m1),
    "ema9": float(e9) if e9 is not None else None,
    "ema21": float(e21) if e21 is not None else None,
    "ema50": float(e50) if e50 is not None else None,
    "session_high": session_high,
    "session_low": session_low,
    "pdh": extra.get("pdh"),
    "pdl": extra.get("pdl"),
    "tick": tick_meta(
      source="nse_index",
      instrument="NIFTY",
      value=float(cash) if cash is not None else None,
      validity=DataValidity.OK if cash is not None else DataValidity.MISSING,
    ).to_dict(),
  }
