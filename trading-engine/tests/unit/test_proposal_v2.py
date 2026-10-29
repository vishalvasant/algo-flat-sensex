"""Tests for proposal v2 enhancements."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from algoflat.journal.analytics import StrategyLearner
from algoflat.models.events import CandidateSignal, FeatureSnapshot, MarketRegime
from algoflat.quality.gate import QualityGate
from algoflat.scanner.vwap_reclaim import VwapReclaimScanner
from algoflat.strategy.router import StrategyRouter
from algoflat.trading.entry_confirmation import EntryConfirmationTracker
from algoflat.validator.premium_behaviour import (
  PremiumBehaviourTracker,
  premium_responsive_rejection_reasons,
)


def _signal(setup: str = "vwap_reclaim", token: str = "CE1") -> CandidateSignal:
  return CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type=setup,
    side="CE",
    instrument_token=token,
    tsym="NIFTYCE",
    strategy_version="t",
    feature_snapshot=FeatureSnapshot(ts=datetime.now(tz=timezone.utc)),
    confidence=80,
  )


def test_entry_confirmation_requires_two_scans():
  tracker = EntryConfirmationTracker(enabled=True, required_scans=2)
  sig = _signal()
  out, reason = tracker.consider(sig)
  assert out is None
  assert reason and "1/2" in reason
  out2, _ = tracker.consider(sig)
  assert out2 is not None
  assert out2.setup_type == "vwap_reclaim"


def test_entry_confirmation_high_confidence_bypass():
  tracker = EntryConfirmationTracker(
    enabled=True, required_scans=2, high_confidence_bypass=90
  )
  sig = _signal()
  sig = sig.model_copy(update={"confidence": 95})
  out, reason = tracker.consider(sig)
  assert out is not None
  assert reason is None


def test_entry_confirmation_resets_when_signal_missing():
  tracker = EntryConfirmationTracker(enabled=True, required_scans=2)
  sig = _signal()
  tracker.consider(sig)
  tracker.consider(None)
  out, reason = tracker.consider(sig)
  assert out is None
  assert reason and "1/2" in reason


def test_learner_pauses_after_session_losses():
  learner = StrategyLearner(
    path=None, pause_after_session_losses=3, pause_rest_of_session=True
  )
  learner.begin_session()
  for _ in range(3):
    learner.record_trade("vwap_reclaim", -50)
  assert learner.is_paused("vwap_reclaim")


def test_premium_not_responsive_rejects():
  tracker = PremiumBehaviourTracker(max_samples=8)
  now = datetime.now(tz=timezone.utc)
  token = "CE1"
  tracker.record(token, 24000.0, 100.0, now)
  tracker.record(token, 24100.0, 100.2, now)
  sig = _signal(token=token)
  reasons = premium_responsive_rejection_reasons(
    sig,
    tracker,
    {
      "enabled": True,
      "min_spot_move_pct": 0.03,
      "min_premium_move_pct": 0.5,
      "lookback_samples": 2,
      "apply_regimes": ["expiry_behaviour"],
      "apply_setups": ["vwap_reclaim"],
    },
    is_expiry_day=True,
    regime_primary="trending_up",
  )
  assert "premium_not_responsive" in reasons


def test_router_expiry_mode_filters_strategies():
  cfg = SimpleNamespace(
    strategy={
      "router": {
        "min_confidence": 50,
        "enabled_strategies": ["vwap_reclaim", "expiry_scalping", "level_rejection"],
      },
      "regime": {
        "filter_mode": "hard",
        "expiry_mode": {
          "enabled": True,
          "allowed_strategies": ["expiry_scalping", "level_rejection"],
        },
      },
      "strategy_version": "t",
      "strike_selection": {"enabled": False},
    },
    symbols={"strike_step": 50},
    validator={},
    risk={},
    runtime={},
  )
  scanners = [VwapReclaimScanner(cfg)]  # type: ignore[arg-type]
  from algoflat.scanner.library import build_strategy_scanners

  scanners = build_strategy_scanners(cfg)  # type: ignore[arg-type]
  router = StrategyRouter(cfg, scanners, QualityGate(cfg))  # type: ignore[arg-type]
  regime = MarketRegime(
    ts=datetime.now(tz=timezone.utc),
    primary="expiry_behaviour",
    probabilities={"expiry_behaviour": 100.0},
    trade_allowed=True,
    risk_score=30,
    reasons=[],
    health={},
  )
  features = FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    nifty_spot=Decimal("24000"),
    session_vwap=Decimal("23950"),
  )
  universe = SimpleNamespace(
    atm_ce=None,
    atm_pe=None,
    atm_strike=Decimal("24000"),
    instruments=[],
    expiry_symbol="",
  )
  active, logs = router._active_scanners(
    regime, features, is_expiry_day=True
  )
  names = {s.name for s in active}
  assert "vwap_reclaim" not in names
  assert "expiry_scalping" in names or "level_rejection" in names
  assert any("expiry_mode" in x for x in logs)
