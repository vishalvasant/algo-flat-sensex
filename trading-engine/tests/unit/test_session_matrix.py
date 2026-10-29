"""Tests for session matrix playbook."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from algoflat.models.events import Bias, FeatureSnapshot, MarketRegime
from algoflat.strategy.session_matrix import (
  classify_session_bucket,
  resolve_session_allowlist,
)


def _features(**extra):
  base = {
    "gap_points": 0,
    "distance_to_vwap_points": 10,
    "structure_5m": "hhhl",
    "counter_bias_signals": [],
    "bias_confidence_mismatch": False,
  }
  base.update(extra)
  return FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    nifty_spot=Decimal("24500"),
    session_vwap=Decimal("24480"),
    bias_5m=Bias.BULLISH,
    extra=base,
  )


def _regime(primary="trending_up", session_phase="mid"):
  return MarketRegime(
    ts=datetime.now(tz=timezone.utc),
    primary=primary,
    probabilities={primary: 100},
    trade_allowed=True,
    risk_score=30,
    health={"session_phase": session_phase},
  )


def _matrix_cfg():
  return {
    "enabled": True,
    "min_gap_points": 35,
    "strong_open_extension_points": 15,
    "fade_extension_points": 20,
    "default_bucket": "flat_trend",
    "buckets": {
      "gap_up_strong_open": ["gap_and_go", "opening_range_breakout"],
      "gap_up_fade": ["gap_fade", "peak_reversal_fade"],
      "flat_trend": ["vwap_trend", "momentum_continuation"],
      "sideways_range": ["level_rejection", "peak_reversal_fade"],
      "expiry_day": ["expiry_scalping"],
    },
  }


def test_gap_up_strong_open_bucket():
  feat = _features(gap_points=50, distance_to_vwap_points=20)
  bucket = classify_session_bucket(
    feat, _regime(session_phase="open"), cfg=_matrix_cfg()
  )
  assert bucket == "gap_up_strong_open"


def test_gap_up_fade_bucket_on_extension():
  feat = _features(
    gap_points=50,
    distance_to_vwap_points=25,
    counter_bias_signals=["m5_bearish_engulf", "m5_momentum_down"],
    bias_confidence_mismatch=True,
  )
  # Bearish bias during gap-up open → fade (not strong_open continuation).
  feat = FeatureSnapshot(
    ts=feat.ts,
    nifty_spot=feat.nifty_spot,
    session_vwap=feat.session_vwap,
    bias_5m=Bias.BEARISH,
    extra=feat.extra,
  )
  bucket = classify_session_bucket(
    feat, _regime(session_phase="open"), cfg=_matrix_cfg()
  )
  assert bucket == "gap_up_fade"


def test_gap_bucket_does_not_persist_after_open():
  """Live bug: gap_down_fade stuck all day blocked trend setups after recovery."""
  feat = _features(
    gap_points=-50,
    distance_to_vwap_points=40,
    counter_bias_signals=["m5_bullish_engulf", "m5_momentum_up"],
  )
  cfg = _matrix_cfg()
  cfg["gap_buckets_open_only"] = True
  mid = classify_session_bucket(
    feat, _regime(primary="trending_up", session_phase="mid"), cfg=cfg
  )
  late = classify_session_bucket(
    feat, _regime(primary="trending_up", session_phase="late"), cfg=cfg
  )
  assert mid == "flat_trend"
  assert late == "flat_trend"
  open_b = classify_session_bucket(
    feat, _regime(primary="trending_up", session_phase="open"), cfg=cfg
  )
  assert open_b == "gap_down_fade"


def test_resolve_session_allowlist():
  strategy = {"session_matrix": _matrix_cfg()}
  allowed, bucket, logs = resolve_session_allowlist(
    _features(gap_points=50, distance_to_vwap_points=20),
    _regime(session_phase="open"),
    strategy=strategy,
  )
  assert bucket == "gap_up_strong_open"
  assert allowed == {"gap_and_go", "opening_range_breakout"}
  assert any("session_bucket=" in line for line in logs)
