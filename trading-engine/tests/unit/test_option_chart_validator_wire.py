"""Ensure option chart confirm runs independently of option_entry_veto."""
from __future__ import annotations

from algoflat.config import AppConfig
from algoflat.models.events import Bias, CandidateSignal, FeatureSnapshot, OptionState
from algoflat.validator.engine import RuleValidator
from datetime import datetime, timezone
from decimal import Decimal


def test_option_chart_runs_when_entry_veto_disabled() -> None:
  config = AppConfig()
  config.validator["option_entry_veto"] = {"enabled": False}
  config.validator["option_chart_confirm"] = {
    "enabled": True,
    "allow_if_no_bars": False,
    "min_closed_m1_bars": 99,
    "apply_setups": ["vwap_reclaim"],
  }
  validator = RuleValidator(config)
  feat = FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    nifty_spot=Decimal("25000"),
    bias_5m=Bias.BULLISH,
  )
  signal = CandidateSignal(
    ts=feat.ts,
    setup_type="vwap_reclaim",
    side="CE",
    instrument_token="1",
    tsym="TEST",
    strategy_version="t",
    feature_snapshot=feat,
    confidence=70,
  )
  opt = OptionState(instrument_token="1", tsym="TEST", ltp=Decimal("100"))
  result = validator.validate(
    signal,
    opt,
    has_open_for_token=False,
    in_cooldown=False,
    kill_switch=False,
    option_m1=[],
  )
  assert "option_confirmation_unavailable" in result.rejection_reasons
