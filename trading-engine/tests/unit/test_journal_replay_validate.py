"""Unit tests for journal replay validate mode."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from algoflat.models.events import Bias, FeatureSnapshot
from algoflat.replay.loader import DayJournal, JournalSignal, SessionFunnel
from algoflat.replay.validate import revalidate_journal


def _signal(*, passed: bool, setup: str = "vwap_reclaim") -> JournalSignal:
    sid = uuid4()
    ts = datetime(2026, 7, 28, 10, 30, tzinfo=timezone.utc)
    feat = FeatureSnapshot(
        ts=ts,
        nifty_spot=Decimal("24000"),
        session_vwap=Decimal("23980"),
        bias_5m=Bias.BULLISH,
        setup_3m="reclaim",
        trigger_1m="bull",
    )
    return JournalSignal(
        id=sid,
        ts=ts,
        setup_type=setup,
        side="CE",
        instrument_token="12345",
        tsym="NIFTY28JUL26C24000",
        strategy_version="test_v1",
        feature_snapshot=feat,
        scanner_metadata={"option_ltp": "42.5", "confidence": 80},
        confidence=80,
        validator_passed=passed,
        rejection_reasons=[] if passed else ["cooldown_active"],
        validator_version="validator_v1.0.0",
        order_status=None,
        order_filled=False,
        fill_price=None,
        order_mode=None,
    )


def test_revalidate_runs_on_journal():
    journal = DayJournal(
        day=datetime(2026, 7, 28).date(),
        underlying="NIFTY",
        signals=[_signal(passed=True), _signal(passed=False)],
        funnel=SessionFunnel(candidate_signals=2),
    )
    report = revalidate_journal(journal, is_expiry_day=True)
    assert report.total == 2
    assert report.comparable == 2
    assert 0 <= report.agreement_pct <= 100
