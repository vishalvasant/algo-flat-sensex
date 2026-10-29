"""Tests for expiry distance (DTE) playbook."""
from __future__ import annotations

from datetime import date

from algoflat.strategy.expiry_distance import (
  classify_expiry_bucket,
  days_to_expiry,
  resolve_expiry_allowlist,
)


def _cfg():
  return {
    "enabled": True,
    "thresholds": {
      "fresh_series_min_dte": 6,
      "theta_week_min_dte": 3,
      "theta_week_max_dte": 5,
      "theta_week_late_max_dte": 4,
      "pre_expiry_max_dte": 2,
    },
    "buckets": {
      "fresh_series": ["gap_fade", "delta_momentum"],
      "theta_week": ["vwap_trend", "opening_range_breakout"],
      "theta_week_late": ["vwap_trend", "peak_reversal_fade"],
      "pre_expiry": ["momentum_continuation"],
      "expiry_day": ["peak_reversal_fade"],
    },
  }


def test_days_to_expiry_tuesday_weekly():
  # Tue 28 Jul expiry viewed from Wed 22 Jul (after roll)
  assert days_to_expiry(date(2026, 7, 22), date(2026, 7, 28)) == 6


def test_classify_fresh_series():
  assert classify_expiry_bucket(6, cfg=_cfg()) == "fresh_series"


def test_classify_theta_week_thursday():
  assert classify_expiry_bucket(5, cfg=_cfg()) == "theta_week"


def test_classify_theta_week_late_friday():
  assert classify_expiry_bucket(4, cfg=_cfg()) == "theta_week_late"


def test_classify_pre_expiry_monday():
  assert classify_expiry_bucket(1, cfg=_cfg()) == "pre_expiry"


def test_resolve_blocks_gap_fade_in_theta_week():
  allowed, dte, bucket, logs = resolve_expiry_allowlist(
    session_date=date(2026, 7, 23),
    expiry_date=date(2026, 7, 28),
    strategy={"expiry_distance": _cfg()},
  )
  assert dte == 5
  assert bucket == "theta_week"
  assert allowed == {"vwap_trend", "opening_range_breakout"}
  assert "gap_fade" not in (allowed or set())
  assert any("expiry_dte=5" in line for line in logs)
