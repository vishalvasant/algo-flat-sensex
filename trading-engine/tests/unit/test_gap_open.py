"""Tests for gap open mode."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from algoflat.features.gap import active_gap_allowlist, gap_class
from algoflat.features.setups import _gap_and_go, _gap_fade
from algoflat.models.events import Bias, Candle, CandleInterval
from algoflat.trading.entry_confirmation import in_open_gap_entry_window
from algoflat.validator.gap_open_traps import gap_open_trap_reasons

IST = ZoneInfo("Asia/Kolkata")


def _candle(close: float, *, high: float | None = None, low: float | None = None) -> Candle:
  c = Decimal(str(close))
  return Candle(
    instrument_token="1",
    ts=datetime.now(tz=timezone.utc),
    open=c,
    high=Decimal(str(high if high is not None else close + 2)),
    low=Decimal(str(low if low is not None else close - 2)),
    close=c,
    volume=100,
    interval=CandleInterval.M1,
  )


def test_gap_class_thresholds():
  assert gap_class(40.0, min_gap=35) == "gap_up"
  assert gap_class(-50.0, min_gap=35) == "gap_down"
  assert gap_class(10.0, min_gap=35) == "flat"


def test_gap_and_go_uses_config_threshold():
  m1 = [_candle(100 + i) for i in range(12)]
  ind = {"gap_points": 36.0}
  gcfg = {"min_gap_points": 35}
  assert _gap_and_go(m1, ind, Bias.BULLISH, gcfg) == "bull"


def test_gap_fade_on_rejection_at_or_high():
  m1 = [
    _candle(100),
    _candle(101),
    _candle(102, high=103),
    _candle(101, high=102),
    _candle(100.5, high=101.5, low=100),
  ]
  m1[-1] = Candle(
    instrument_token="1",
    ts=datetime.now(tz=timezone.utc),
    open=Decimal("101"),
    high=Decimal("102"),
    low=Decimal("100"),
    close=Decimal("100.5"),
    volume=100,
    interval=CandleInterval.M1,
  )
  spot = Decimal("101")
  ind = {"gap_points": 40.0, "or_high": 102.0, "or_low": 98.0}
  gcfg = {"min_gap_points": 35, "rejection_proximity_points": 15}
  assert _gap_fade(m1, spot, Decimal("100"), ind, gcfg) == "bear"


def test_gap_open_blocks_ce_on_gap_down_inside_or():
  from algoflat.models.events import CandidateSignal, FeatureSnapshot

  feat = FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    nifty_spot=Decimal("24100"),
    bias_5m=Bias.BEARISH,
    extra={"gap_points": -50.0, "or_low": 24050.0, "or_high": 24120.0},
  )
  sig = CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type="vwap_trend",
    side="CE",
    instrument_token="t",
    tsym="CE",
    strategy_version="t",
    feature_snapshot=feat,
  )
  reasons = gap_open_trap_reasons(
    sig,
    {
      "gap_open": {
        "enabled": True,
        "min_gap_points": 35,
        "trend_blocked_setups": ["vwap_trend"],
      }
    },
  )
  assert "gap_down_block_ce_until_or_break" in reasons


def test_gap_open_allows_ce_after_or_high_break():
  from algoflat.models.events import CandidateSignal, FeatureSnapshot

  feat = FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    nifty_spot=Decimal("24150"),
    bias_5m=Bias.BULLISH,
    extra={"gap_points": -50.0, "or_low": 24050.0, "or_high": 24120.0},
  )
  sig = CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type="vwap_trend",
    side="CE",
    instrument_token="t",
    tsym="CE",
    strategy_version="t",
    feature_snapshot=feat,
  )
  reasons = gap_open_trap_reasons(
    sig,
    {
      "gap_open": {
        "enabled": True,
        "min_gap_points": 35,
        "trend_blocked_setups": ["vwap_trend"],
      }
    },
  )
  assert "gap_down_block_ce_until_or_break" not in reasons


def test_legacy_gap_bounds_still_block_ce_after_or_high_break():
  from algoflat.models.events import CandidateSignal, FeatureSnapshot

  feat = FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    nifty_spot=Decimal("24150"),
    bias_5m=Bias.BULLISH,
    extra={"gap_points": -50.0, "or_low": 24050.0, "or_high": 24120.0},
  )
  sig = CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type="vwap_trend",
    side="CE",
    instrument_token="t",
    tsym="CE",
    strategy_version="t",
    feature_snapshot=feat,
  )
  reasons = gap_open_trap_reasons(
    sig,
    {
      "gap_open": {
        "enabled": True,
        "min_gap_points": 35,
        "trend_blocked_setups": ["vwap_trend"],
        "legacy_wrong_or_bounds": True,
      }
    },
  )
  assert "gap_down_block_ce_until_or_break" in reasons


def test_open_gap_entry_window():
  inside = datetime(2026, 7, 29, 9, 40, tzinfo=IST)
  outside = datetime(2026, 7, 29, 10, 0, tzinfo=IST)
  assert in_open_gap_entry_window(inside, open_window_minutes=15) is True
  assert in_open_gap_entry_window(outside, open_window_minutes=15) is False


def test_gap_mode_allowlist():
  cfg = {
    "enabled": True,
    "min_gap_points": 35,
    "gap_up_allowed_strategies": ["gap_and_go", "gap_fade"],
  }
  allowed = active_gap_allowlist(40.0, cfg)
  assert allowed == {"gap_and_go", "gap_fade"}
