"""Tests for NO_TRADE codes and context features."""
from __future__ import annotations

from algoflat.market_intelligence.early_bird import build_early_bird_context
from algoflat.market_intelligence.no_trade import classify_no_trade_code
from algoflat.market_intelligence.reversal_context import build_reversal_context
from algoflat.models.events import Bias, FeatureSnapshot
from datetime import datetime, timezone
from decimal import Decimal


def test_no_trade_ce_pe_unclear():
  code = classify_no_trade_code(
    extra={"mi_no_trade_recommended": True, "ce_pe_verdict": "both_weak"},
    trade_allowed=False,
  )
  assert code == "NO_TRADE_CE_PE_UNCLEAR"


def test_no_trade_data_quality():
  code = classify_no_trade_code(
    veto_reasons=["DATA_QUALITY_INVALID"],
    trade_allowed=False,
  )
  assert code == "NO_TRADE_DATA_QUALITY"


def test_early_bird_context_scores():
  from algoflat.market_intelligence.context import MarketIntelligenceContext

  ctx = MarketIntelligenceContext(
    features=FeatureSnapshot(
      ts=datetime.now(tz=timezone.utc),
      nifty_spot=Decimal("24500"),
      session_vwap=Decimal("24490"),
      bias_5m=Bias.BULLISH,
      extra={},
    )
  )
  out = build_early_bird_context(
    ctx,
    spot={"momentum_5m_pct": 0.02},
    fut={"futures_momentum_5m_pct": 0.12, "futures_volume_strength": 1.3},
    breadth={"advance_pct": 58},
    sectors={"sector_breadth_pct": 60},
    cfg={"min_signals": 3},
  )
  assert out["early_bird_active"] is True
  assert out["may_place_order"] is False


def test_reversal_context_elevated():
  from algoflat.market_intelligence.context import MarketIntelligenceContext

  ctx = MarketIntelligenceContext(
    features=FeatureSnapshot(
      ts=datetime.now(tz=timezone.utc),
      nifty_spot=Decimal("24500"),
      session_vwap=Decimal("24400"),
      bias_5m=Bias.BULLISH,
      extra={"levels": {"at_resistance": True}, "distance_to_vwap_points": 30},
    )
  )
  out = build_reversal_context(
    ctx,
    spot={"momentum_5m_pct": 0.1},
    fut={"futures_momentum_5m_pct": -0.08},
    breadth={"advance_pct": 40},
    sectors={"sector_breadth_pct": 38},
    divergence={"nifty_breadth_divergence": "narrow_rally"},
    cfg={"min_signals": 2},
  )
  assert out["reversal_risk_elevated"] is True
  assert out["may_place_order"] is False
