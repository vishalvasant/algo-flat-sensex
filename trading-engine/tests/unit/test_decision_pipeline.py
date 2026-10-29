"""Unit tests for Fix Specification v1.0 decision pipeline."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from algoflat.decision.extension import (
  classify_extension_state,
  extension_veto_reasons,
)
from algoflat.decision.market_state import (
  DEAD_RANGE,
  classify_sideways_substate,
  detect_transition_state,
  sideways_veto_reasons,
)
from algoflat.decision.pipeline import DecisionPipeline
from algoflat.decision.side_selection import ScoredCandidate, select_best_side
from algoflat.models.events import (
  Bias,
  CandidateSignal,
  FeatureSnapshot,
  MarketRegime,
  OptionState,
)


def _features(**extra) -> FeatureSnapshot:
  return FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    nifty_spot=Decimal("24700"),
    session_vwap=Decimal("24650"),
    bias_5m=Bias.BEARISH,
    extra=extra,
  )


def _regime(**kwargs) -> MarketRegime:
  defaults = dict(
    ts=datetime.now(tz=timezone.utc),
    primary="trending_down",
    probabilities={"trending_down": 60, "sideways": 25},
    trade_allowed=True,
    risk_score=40,
    health={"structure_5m": "lllh"},
  )
  defaults.update(kwargs)
  return MarketRegime(**defaults)


def _signal(setup: str, side: str = "PE", conf: int = 85) -> CandidateSignal:
  return CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type=setup,
    side=side,
    instrument_token="tok1",
    tsym="NIFTY11AUG26P24700",
    strategy_version="test",
    feature_snapshot=_features(),
    confidence=conf,
    scanner_metadata={"confidence": conf},
  )


def _config():
  return SimpleNamespace(
    strategy={
      "decision_pipeline": {
        "enabled": True,
        "enforce": True,
        "extension": {
          "enabled": True,
          "moderate_points": 15,
          "high_points": 25,
          "extreme_points": 35,
        },
        "market_state": {
          "sideways": {"enabled": True, "dead_range_max_points": 18},
          "transition": {"enabled": True, "block_continuation_in_transition": True},
        },
        "chase_filter": {"enabled": True, "max_extension_without_retest": 30},
        "side_selection": {"enabled": True, "min_edge_over_no_trade": 5},
        "expected_value": {"enabled": True, "min_ev_inr": -5},
        "data_quality": {"enabled": True},
      }
    }
  )


def test_extension_blocks_late_continuation():
  feat = _features(distance_to_vwap_points=40, structure_5m="mixed")
  regime = _regime(health={"structure_5m": "mixed"})
  state = classify_extension_state(feat, regime, cfg={"enabled": True})
  assert state in ("EXTREME", "EXTREME_EXHAUSTION")
  sig = _signal("momentum_continuation")
  reasons = extension_veto_reasons(sig, state, feat, cfg={"enabled": True})
  assert "extension_blocks_late_continuation" in reasons


def test_reversal_allowed_at_extension_with_signals():
  feat = _features(distance_to_vwap_points=40, reversal_signal_count=3)
  state = classify_extension_state(feat, _regime(), cfg={"enabled": True})
  sig = _signal("peak_reversal_fade")
  reasons = extension_veto_reasons(sig, state, feat, cfg={"enabled": True})
  assert "reversal_needs_extension" not in reasons


def test_dead_range_prefers_no_trade():
  regime = _regime(primary="sideways", probabilities={"sideways": 80})
  feat = _features(range_5m_points=12, chain={"ce_premium_decaying": True, "pe_premium_decaying": True})
  regime.health["range_5m_points"] = 12
  sub = classify_sideways_substate(feat, regime, cfg={"dead_range_max_points": 18})
  assert sub == DEAD_RANGE
  assert sideways_veto_reasons(sub, "momentum_continuation", cfg={"enabled": True})


def test_no_trade_beats_weak_ce():
  feat = _features(weak_option_response=True)
  regime = _regime(risk_score=80)
  ce = ScoredCandidate("vwap_trend", _signal("vwap_trend", "CE", 70), 70, {})
  result = select_best_side(
    [ce],
    feat,
    regime,
    cfg={"enabled": True, "min_edge_over_no_trade": 5, "no_trade": {"base_score": 75}},
  )
  assert result.selected is None
  assert result.no_trade_score >= 70


def test_verdict_does_not_veto_only_real_side():
  feat = _features(ce_pe_verdict="pe_better")
  regime = _regime()
  ce = ScoredCandidate("vwap_trend", _signal("vwap_trend", "CE", 100), 100, {})
  result = select_best_side(
    [ce],
    feat,
    regime,
    cfg={
      "enabled": True,
      "min_edge_over_no_trade": 5,
      "veto_against_ce_pe_verdict": True,
      "no_trade": {"base_score": 45},
    },
  )
  assert result.selected is not None
  assert result.selected.signal.side == "CE"
  assert not any(line.startswith("veto_ce_while_pe_stronger") for line in result.logs)


def test_pipeline_vetoes_high_extension_continuation():
  pipe = DecisionPipeline(_config())
  feat = _features(distance_to_vwap_points=45)
  regime = _regime()
  cand = ScoredCandidate("momentum_continuation", _signal("momentum_continuation"), 88, {})
  out = pipe.evaluate([cand], feat, regime, candles_stale=False, last_refresh_ok=True)
  assert not out.approved
  assert out.veto_reasons


def test_transition_blocks_momentum():
  feat = _features(structure_5m="mixed")
  regime = _regime(primary="trending_down", probabilities={"sideways": 30, "trending_down": 50})
  transition = detect_transition_state(feat, regime, cfg={"enabled": True, "min_sideways_probability": 20})
  assert transition == "TRANSITION_DOWN"


def test_data_quality_invalid_on_stale_candles():
  from algoflat.decision.data_quality import assess_data_quality

  state, failures = assess_data_quality(
    features=_features(),
    option=OptionState(instrument_token="t", tsym="X", ltp=Decimal("100")),
    candles_stale=True,
    last_refresh_ok=False,
    cfg={"enabled": True},
  )
  assert state == "DATA_QUALITY_INVALID"
  assert failures
