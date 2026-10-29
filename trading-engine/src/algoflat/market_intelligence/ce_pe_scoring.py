"""Phase 5 — CE vs PE relative strength comparison."""
from __future__ import annotations

from typing import Any

from algoflat.market_intelligence.context import MarketIntelligenceContext


def _metric_strength(value: float | None, *, invert: bool = False) -> float:
  if value is None:
    return 0.0
  v = float(value)
  if invert:
    v = -v
  return max(0.0, min(100.0, 50.0 + v * 10))


def build_ce_pe_comparison(
  ctx: MarketIntelligenceContext,
  *,
  premium: dict[str, Any],
  fut: dict[str, Any] | None = None,
  cfg: dict,
) -> dict[str, Any]:
  weights = cfg.get("weights") or {}
  w_mom = float(weights.get("momentum", 0.25))
  w_vol = float(weights.get("volume", 0.15))
  w_oi = float(weights.get("oi_change", 0.15))
  w_spread = float(weights.get("spread", 0.15))
  w_delta = float(weights.get("delta", 0.15))
  w_vwap = float(weights.get("vwap_distance", 0.15))

  extra = ctx.features.extra or {}
  fut_block = fut or extra.get("spot_fut") or {}
  mom = float(
    fut_block.get("futures_momentum_5m_pct")
    or fut_block.get("fut_momentum_5m_pct")
    or 0
  )

  ce = premium.get("ce") or {}
  pe = premium.get("pe") or {}

  ce_mom = mom if mom > 0 else 0
  pe_mom = abs(mom) if mom < 0 else 0

  ce_vol = float(ce.get("volume") or 0)
  pe_vol = float(pe.get("volume") or 0)
  ce_oi = float(ce.get("oi_change") or 0)
  pe_oi = float(pe.get("oi_change") or 0)
  ce_spread = float(ce.get("spread_pct") or 99)
  pe_spread = float(pe.get("spread_pct") or 99)
  ce_delta = abs(float(ce.get("delta") or 0))
  pe_delta = abs(float(pe.get("delta") or 0))

  ce_strength = (
    w_mom * _metric_strength(ce_mom)
    + w_vol * _metric_strength(ce_vol / max(pe_vol, 1))
    + w_oi * _metric_strength(ce_oi)
    + w_spread * _metric_strength(12 - ce_spread, invert=False)
    + w_delta * _metric_strength(ce_delta * 100)
    + w_vwap * float(premium.get("ce_premium_score") or 50) / 100 * 100
  )
  pe_strength = (
    w_mom * _metric_strength(pe_mom)
    + w_vol * _metric_strength(pe_vol / max(ce_vol, 1))
    + w_oi * _metric_strength(pe_oi)
    + w_spread * _metric_strength(12 - pe_spread, invert=False)
    + w_delta * _metric_strength(pe_delta * 100)
    + w_vwap * float(premium.get("pe_premium_score") or 50) / 100 * 100
  )

  ce_strength = int(round(max(0, min(100, ce_strength))))
  pe_strength = int(round(max(0, min(100, pe_strength))))
  gap = ce_strength - pe_strength
  both_weak_thr = int(cfg.get("both_weak_threshold", 42))
  min_gap = int(cfg.get("min_strength_gap", 8))

  if ce_strength < both_weak_thr and pe_strength < both_weak_thr:
    verdict = "both_weak"
  elif gap >= min_gap:
    verdict = "ce_better"
  elif gap <= -min_gap:
    verdict = "pe_better"
  elif ce_strength >= both_weak_thr and pe_strength >= both_weak_thr:
    verdict = "both_strong"
  else:
    verdict = "no_clear_advantage"

  return {
    "ce_strength": ce_strength,
    "pe_strength": pe_strength,
    "ce_pe_strength_difference": gap,
    "ce_pe_momentum_difference": round(ce_mom - pe_mom, 4),
    "ce_pe_volume_difference": ce_vol - pe_vol,
    "ce_pe_relative_value": round(ce_strength - pe_strength, 2),
    "verdict": verdict,
    "no_trade_recommended": verdict in ("both_weak", "no_clear_advantage"),
  }
