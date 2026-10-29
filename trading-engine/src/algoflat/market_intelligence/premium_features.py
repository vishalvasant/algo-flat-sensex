"""Phase 4 — Option premium confirmation features."""
from __future__ import annotations

from typing import Any

from algoflat.market_intelligence.context import MarketIntelligenceContext
from algoflat.market_intelligence.tick_meta import DataValidity


def _side_state(chain: dict, side: str) -> dict[str, Any]:
  key = "atm_ce" if side == "CE" else "atm_pe"
  row = chain.get(key) or {}
  return {
    "ltp": row.get("ltp"),
    "volume": row.get("volume"),
    "oi": row.get("oi"),
    "oi_change": row.get("oi_change"),
    "iv": row.get("iv"),
    "delta": row.get("delta"),
    "gamma": row.get("gamma"),
    "spread_pct": row.get("spread_pct"),
    "option_vwap": row.get("option_vwap"),
  }


def _premium_score(row: dict, *, cfg: dict) -> tuple[int, list[str]]:
  reasons: list[str] = []
  score = 50
  spread = row.get("spread_pct")
  if spread is not None:
    max_sp = float(cfg.get("max_spread_pct", 12))
    if float(spread) <= max_sp:
      score += 15
      reasons.append("spread_ok")
    else:
      score -= 20
      reasons.append("spread_wide")
  ltp = row.get("ltp")
  if ltp is not None and float(ltp) > 0:
    score += 10
    reasons.append("ltp_ok")
  iv = row.get("iv")
  if iv is not None:
    score += 5
  delta = row.get("delta")
  if delta is not None:
    d = abs(float(delta))
    if 0.30 <= d <= 0.70:
      score += 10
      reasons.append("delta_band")
  oi_chg = row.get("oi_change")
  if oi_chg is not None and int(oi_chg) > 0:
    score += 8
    reasons.append("oi_building")
  return max(0, min(100, score)), reasons


def build_premium_features(ctx: MarketIntelligenceContext, *, cfg: dict) -> dict[str, Any]:
  chain = ctx.chain_snapshot or {}
  ce = _side_state(chain, "CE")
  pe = _side_state(chain, "PE")
  ce_score, ce_reasons = _premium_score(ce, cfg=cfg)
  pe_score, pe_reasons = _premium_score(pe, cfg=cfg)

  bias = (ctx.features.extra or {}).get("spot_fut", {}).get("fut_bias", "neutral")
  ce_aligned = bias in ("bullish", "neutral")
  pe_aligned = bias in ("bearish", "neutral")
  ce_confirm = ce_score >= 55 and ce_aligned
  pe_confirm = pe_score >= 55 and pe_aligned

  return {
    "available": ce.get("ltp") is not None or pe.get("ltp") is not None,
    "ce": ce,
    "pe": pe,
    "ce_premium_score": ce_score,
    "pe_premium_score": pe_score,
    "ce_confirm": ce_confirm,
    "pe_confirm": pe_confirm,
    "ce_reasons": ce_reasons,
    "pe_reasons": pe_reasons,
    "premium_momentum": None,
    "validity": DataValidity.OK.value if ce.get("ltp") or pe.get("ltp") else DataValidity.MISSING.value,
  }
