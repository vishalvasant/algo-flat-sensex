"""Unit tests for market intelligence engine (New-Engine phases 1–10)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from algoflat.config import AppConfig
from algoflat.market_intelligence.context import MarketIntelligenceContext
from algoflat.market_intelligence.engine import MarketIntelligenceEngine
from algoflat.models.events import Bias, FeatureSnapshot


def _features(**extra) -> FeatureSnapshot:
  base = {
    "cash_index_spot": 24500.0,
    "fut_spot": 24520.0,
    "spot_fut": {
      "futures_basis": 20.0,
      "spot_fut_alignment": "bullish",
      "fut_bias": "bullish",
      "spot_bias": "bullish",
    },
  }
  base.update(extra)
  return FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    nifty_spot=Decimal("24520"),
    session_vwap=Decimal("24480"),
    bias_5m=Bias.BULLISH,
    extra=base,
  )


def test_mi_engine_builds_core_blocks():
  config = AppConfig()
  engine = MarketIntelligenceEngine(config)
  ctx = MarketIntelligenceContext(
    features=_features(),
    index_ltps={"INDIAVIX": 14.5, "NIFTY": 24500.0, "BANKNIFTY": 52000.0},
    index_opens={"INDIAVIX": 14.0, "NIFTY": 24450.0, "BANKNIFTY": 51800.0},
    chain_snapshot={
      "atm_ce": {"ltp": 120.0, "volume": 1000, "oi": 5000, "spread_pct": 2.0, "delta": 0.45},
      "atm_pe": {"ltp": 95.0, "volume": 800, "oi": 4200, "spread_pct": 2.5, "delta": -0.42},
    },
  )
  snap = engine.build(ctx)
  assert snap.get("enabled") is True
  assert snap["spot"]["available"] is True
  assert snap["fut"]["available"] is True
  assert snap["vix"]["available"] is True
  assert snap["premium"]["available"] is True
  assert "ce_strength" in (snap.get("ce_pe") or {})
  assert snap["data_quality"]["state"] in ("DATA_QUALITY_OK", "DATA_QUALITY_DEGRADED")


def test_mi_apply_to_features_extra():
  config = AppConfig()
  engine = MarketIntelligenceEngine(config)
  mi = {
    "ce_pe": {"ce_strength": 62, "pe_strength": 48, "verdict": "ce_better", "no_trade_recommended": False},
    "global": {"global_sentiment_score": 0.2},
    "vix": {"vix_level": 14.5},
    "sectors": {"sector_breadth_pct": 55.0},
    "divergence": {"nifty_breadth_divergence": False},
    "fut": {"spot_fut_alignment": "bullish"},
  }
  out = engine.apply_to_features_extra({"cash_index_spot": 24500}, mi)
  assert out["ce_strength"] == 62
  assert out["ce_pe_verdict"] == "ce_better"
  assert out["market_intelligence"] == mi


def test_mi_both_weak_flags_no_trade():
  config = AppConfig()
  engine = MarketIntelligenceEngine(config)
  ctx = MarketIntelligenceContext(
    features=_features(),
    chain_snapshot={
      "atm_ce": {"ltp": 5.0, "spread_pct": 20.0},
      "atm_pe": {"ltp": 4.0, "spread_pct": 18.0},
    },
  )
  snap = engine.build(ctx)
  ce_pe = snap.get("ce_pe") or {}
  assert ce_pe.get("verdict") in ("both_weak", "no_clear_advantage", "pe_better", "ce_better")
