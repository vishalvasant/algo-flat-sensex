"""Phase 2 — NIFTY FUT first-class features."""
from __future__ import annotations

from typing import Any

from algoflat.market_intelligence.context import MarketIntelligenceContext
from algoflat.market_intelligence.spot_fut import build_spot_fut_context
from algoflat.market_intelligence.spot_features import _atr_approx, _momentum_pct
from algoflat.market_intelligence.tick_meta import DataValidity, tick_meta


def build_fut_features(ctx: MarketIntelligenceContext) -> dict[str, Any]:
  feat = ctx.features
  extra = feat.extra or {}
  fut = extra.get("fut_spot") or (float(feat.nifty_spot) if feat.nifty_spot else None)
  cash = extra.get("cash_index_spot")
  vwap = feat.session_vwap

  fut_vwap_dist = None
  if fut is not None and vwap is not None:
    fut_vwap_dist = round(float(fut) - float(vwap), 2)

  vols = [c.volume for c in ctx.m1 if c.volume is not None]
  avg_vol = sum(vols) / len(vols) if vols else None
  last_vol = vols[-1] if vols else None
  rel_vol = round(last_vol / avg_vol, 2) if avg_vol and last_vol else None

  spot_fut = build_spot_fut_context(
    cash_spot=float(cash) if cash is not None else None,
    fut_spot=float(fut) if fut is not None else None,
    cash_vwap=None,
    fut_vwap=float(vwap) if vwap is not None else None,
  )

  return {
    "available": fut is not None,
    "fut_ltp": fut,
    "futures_momentum_1m_pct": _momentum_pct(ctx.m1, 3),
    "futures_momentum_5m_pct": _momentum_pct(ctx.m5, 3),
    "futures_vwap_distance": fut_vwap_dist,
    "futures_volume_strength": rel_vol,
    "futures_atr_1m": _atr_approx(ctx.m1),
    **spot_fut,
    "tick": tick_meta(
      source="nfo_futures",
      instrument="NIFTY_FUT",
      value=float(fut) if fut is not None else None,
      validity=DataValidity.OK if fut is not None else DataValidity.MISSING,
    ).to_dict(),
  }
