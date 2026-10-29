"""Tests for session loss guard and reversal flip helpers."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from algoflat.position.exit_rules import should_queue_reversal_flip, vwap_flipped_against
from algoflat.validator.session_loss_guard import SessionLossGuard


def test_session_loss_guard_blocks_after_expiry_orb_loss():
  guard = SessionLossGuard()
  cfg = {
    "session_loss_guard": {
      "enabled": True,
      "expiry_day": {
        "block_after_losses": 1,
        "aggressive_setups": ["opening_range_breakout", "expiry_scalping"],
        "trigger_exit_reasons": ["adverse_momentum"],
        "blocked_setups": ["opening_range_breakout", "expiry_scalping"],
      },
    }
  }
  guard.record_loss(
    "opening_range_breakout",
    is_expiry_day=True,
    exit_reason="adverse_momentum",
    cfg=cfg,
  )
  from algoflat.models.events import CandidateSignal, FeatureSnapshot
  from datetime import datetime, timezone

  sig = CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type="expiry_scalping",
    side="PE",
    instrument_token="1",
    tsym="T",
    strategy_version="t",
    feature_snapshot=FeatureSnapshot(ts=datetime.now(tz=timezone.utc)),
  )
  reasons = guard.rejection_reasons(sig, is_expiry_day=True, cfg=cfg)
  assert "expiry_day_aggressive_setup_paused" in reasons


def test_should_queue_flip_on_adverse_when_vwap_flipped():
  md = MagicMock()
  md.spot_ltp = Decimal("116")
  md.session_vwap_value = Decimal("110")
  exit_cfg = {
    "flip_on_trend_reversal": True,
    "bias_flip_exit": True,
    "bias_flip_buffer_points": 5,
    "flip_exit_reasons": ["adverse_momentum"],
  }
  assert should_queue_reversal_flip(
    exit_reason="adverse_momentum",
    option_side="PE",
    market_data=md,
    exit_cfg=exit_cfg,
  )
  assert not should_queue_reversal_flip(
    exit_reason="momentum_trail",
    option_side="PE",
    market_data=md,
    exit_cfg=exit_cfg,
  )


def test_vwap_flipped_against_ce():
  md = MagicMock()
  md.spot_ltp = Decimal("95")
  md.session_vwap_value = Decimal("100")
  assert vwap_flipped_against(option_side="CE", market_data=md, exit_cfg={"bias_flip_buffer_points": 0})
