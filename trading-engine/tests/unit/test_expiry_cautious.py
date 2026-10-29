"""Tests for expiry-day cautious entry gates."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from algoflat.models.events import Bias, CandidateSignal, FeatureSnapshot
from algoflat.validator.expiry_cautious import expiry_cautious_rejection_reasons

IST = ZoneInfo("Asia/Kolkata")


def _signal(setup: str, confidence: int, mtf: int = 70) -> CandidateSignal:
    extra = {"mtf_score_pe": mtf, "mtf_score_ce": mtf}
    return CandidateSignal(
        id=uuid4(),
        ts=datetime.now(tz=timezone.utc),
        tsym="NIFTY28JUL26P24000",
        instrument_token="1",
        side="PE",
        setup_type=setup,
        confidence=confidence,
        strategy_version="test",
        feature_snapshot=FeatureSnapshot(
            ts=datetime.now(tz=timezone.utc),
            nifty_spot=Decimal("24000"),
            session_vwap=Decimal("24100"),
            bias_5m=Bias.BEARISH,
            extra=extra,
        ),
    )


def _cfg(**overrides):
    base = {
        "enabled": True,
        "min_confidence": 80,
        "min_mtf_score": 55,
        "allowed_setups": [],
        "blocked_setups": ["mean_reversion", "reversal"],
    }
    base.update(overrides)
    return base


def test_expiry_blocks_low_confidence():
    reasons = expiry_cautious_rejection_reasons(_signal("momentum_continuation", 79), _cfg())
    assert "expiry_confidence_too_low" in reasons


def test_expiry_allows_momentum_on_expiry_day():
    reasons = expiry_cautious_rejection_reasons(
        _signal("momentum_continuation", 85, mtf=60),
        _cfg(),
    )
    assert reasons == []


def test_expiry_blocks_denylisted_setup():
    reasons = expiry_cautious_rejection_reasons(
        _signal("mean_reversion", 90, mtf=70),
        _cfg(),
    )
    assert "expiry_setup_blocked" in reasons


def test_expiry_whitelist_still_works_when_set():
    reasons = expiry_cautious_rejection_reasons(
        _signal("opening_range_breakout", 90, mtf=70),
        _cfg(allowed_setups=["vwap_reclaim"]),
    )
    assert "expiry_setup_not_allowed" in reasons


def test_expiry_allows_quality_reclaim():
    reasons = expiry_cautious_rejection_reasons(
        _signal("vwap_reclaim", 90, mtf=70),
        _cfg(allowed_setups=["vwap_reclaim"]),
    )
    assert reasons == []


def test_expiry_blocks_entries_after_entry_end_time():
    reasons = expiry_cautious_rejection_reasons(
        _signal("momentum_continuation", 95, mtf=80),
        _cfg(entry_end_time="14:30"),
        now=datetime(2026, 7, 28, 14, 35, tzinfo=IST),
    )
    assert reasons == ["expiry_entry_window_closed"]


def test_expiry_allows_entries_before_entry_end_time():
    reasons = expiry_cautious_rejection_reasons(
        _signal("momentum_continuation", 90, mtf=70),
        _cfg(entry_end_time="14:30"),
        now=datetime(2026, 7, 28, 11, 0, tzinfo=IST),
    )
    assert reasons == []
