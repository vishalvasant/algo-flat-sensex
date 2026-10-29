"""§26A scan timing and strategy-specific entry confirmation."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from algoflat.models.events import Bias, CandidateSignal, FeatureSnapshot
from algoflat.trading.entry_confirmation import EntryConfirmationTracker
from algoflat.validator.premium_behaviour import PremiumBehaviourTracker


def _signal(setup: str = "vwap_reclaim") -> CandidateSignal:
  feat = FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    nifty_spot=Decimal("24700"),
    bias_5m=Bias.BULLISH,
  )
  return CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type=setup,
    side="CE",
    instrument_token="tok1",
    tsym="NIFTY",
    strategy_version="t",
    feature_snapshot=feat,
    confidence=80,
  )


def test_setup_specific_required_scans():
  tracker = EntryConfirmationTracker(
    required_scans=2,
    setup_overrides={"vwap_reclaim": {"required_scans": 3}},
  )
  sig = _signal("vwap_reclaim")
  out, reason = tracker.consider(sig)
  assert out is None
  assert reason == "entry_confirm_wait scan=1/3"


def test_fast_confirm_on_quote_without_changing_scan_interval():
  tracker = EntryConfirmationTracker(
    required_scans=2,
    fast_confirm_enabled=True,
    fast_confirm_apply_setups=frozenset({"momentum_continuation"}),
  )
  sig = _signal("momentum_continuation")
  tracker.consider(sig)  # 1/2 — pending

  prem = PremiumBehaviourTracker(max_samples=8)
  now = datetime.now(tz=timezone.utc)
  for s, p in [(100.0, 50.0), (100.5, 50.8), (101.0, 51.2), (101.5, 51.8)]:
    prem.record("tok1", s, p, now)

  fast, reason = tracker.try_fast_confirm("tok1", premium_tracker=prem)
  assert fast is not None
  assert reason == "fast_confirm_premium"
  timing = (fast.scanner_metadata or {}).get("confirmation_timing") or {}
  assert timing.get("confirm_path") == "fast_quote_premium"
