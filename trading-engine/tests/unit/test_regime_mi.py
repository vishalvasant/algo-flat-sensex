"""Regime classifier MI enrichment (Phase 9)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from algoflat.config import AppConfig
from algoflat.models.events import Bias, Candle, CandleInterval, FeatureSnapshot
from algoflat.regime.classifier import RegimeClassifier


def _candle(close: str) -> Candle:
  return Candle(
    ts=datetime.now(tz=timezone.utc),
    interval=CandleInterval.M1,
    instrument_token="NIFTY_FUT",
    open=Decimal(close),
    high=Decimal(close),
    low=Decimal(close),
    close=Decimal(close),
    volume=1000,
  )


def test_regime_uses_mi_vix_expansion():
  config = AppConfig()
  clf = RegimeClassifier(config)
  features = FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    nifty_spot=Decimal("24500"),
    session_vwap=Decimal("24400"),
    bias_5m=Bias.BULLISH,
    extra={
      "market_intelligence": {
        "enabled": True,
        "vix": {"available": True, "volatility_regime": "expansion", "vix_level": 18.0},
        "breadth": {"available": True, "advance_pct": 65.0},
        "sectors": {"available": True, "sectors_positive_count": 6, "sector_breadth_pct": 66.0},
        "fut": {"spot_fut_alignment": "bullish"},
      }
    },
  )
  m1 = [_candle("24500") for _ in range(20)]
  m5 = [_candle("24500") for _ in range(10)]
  regime = clf.classify(features, m1, m5)
  assert "mi_vix_expansion" in regime.reasons
  assert regime.probabilities.get("high_volatility", 0) > 0
