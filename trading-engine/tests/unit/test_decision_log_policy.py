from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from algoflat.models.events import MarketRegime, StrategyDecision
from algoflat.trading.decision_log_policy import DecisionLogPolicy, extract_pipeline_scores


def _decision(
  *,
  trade_allowed: bool = False,
  reason: str = "decision_pipeline_veto: no_trade_preferred",
  regime: str = "sideways",
  warnings: list[str] | None = None,
) -> StrategyDecision:
  return StrategyDecision(
    ts=datetime.now(tz=timezone.utc),
    selected_strategy="NO_TRADE" if not trade_allowed else "vwap_pullback",
    trade_allowed=trade_allowed,
    selected_reason=reason,
    regime=MarketRegime(ts=datetime.now(tz=timezone.utc), primary=regime),
    warnings=warnings or [],
  )


def test_always_logs_trade_signals():
  policy = DecisionLogPolicy(enabled=True, repeat_seconds=120)
  now = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)
  d = _decision(trade_allowed=True, reason="selected vwap_pullback confidence=88")
  ok, _ = policy.prepare_log(d, now=now)
  assert ok is True


def test_suppresses_identical_no_trade_until_repeat():
  policy = DecisionLogPolicy(enabled=True, repeat_seconds=120)
  base = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)
  d = _decision()

  ok1, _ = policy.prepare_log(d, now=base)
  ok2, _ = policy.prepare_log(d, now=base + timedelta(seconds=10))
  ok3, extra = policy.prepare_log(d, now=base + timedelta(seconds=120))

  assert ok1 is True
  assert ok2 is False
  assert ok3 is True
  assert extra.get("suppressed_duplicate_scans") == 1


def test_logs_when_fingerprint_changes():
  policy = DecisionLogPolicy(enabled=True, repeat_seconds=120)
  base = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)
  d1 = _decision(regime="sideways")
  d2 = _decision(regime="trending_down")

  assert policy.prepare_log(d1, now=base)[0] is True
  assert policy.prepare_log(d1, now=base + timedelta(seconds=10))[0] is False
  ok, extra = policy.prepare_log(d2, now=base + timedelta(seconds=20))
  assert ok is True
  assert extra.get("suppressed_duplicate_scans") == 1


def test_extract_pipeline_scores_from_warning():
  decision = MagicMock()
  decision.warnings = [
    "decision={'ce_score': 19, 'pe_score': 27, 'no_trade_score': 45, 'veto_reasons': ['no_trade_preferred']}"
  ]
  assert extract_pipeline_scores(decision) == {
    "ce_score": 19,
    "pe_score": 27,
    "no_trade_score": 45,
  }
