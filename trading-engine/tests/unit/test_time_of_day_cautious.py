"""Tests for trading window and time-of-day cautious gates."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from algoflat.market_session import is_entry_window_open
from algoflat.models.events import Bias, CandidateSignal, FeatureSnapshot
from algoflat.validator.time_of_day_cautious import (
    time_of_day_cautious_rejection_reasons,
    time_of_day_lot_cap,
    tod_dead_zone_rejection_reasons,
)

IST = ZoneInfo("Asia/Kolkata")
VALIDATOR = {
    "entry_start_time": "09:30",
    "entry_end_time": "15:15",
}

TOD_CFG = {
    "enabled": True,
    "phases": {
        "open": {
            "start": "09:30",
            "end": "10:15",
            "min_confidence": 82,
            "allowed_setups": ["opening_range_breakout", "momentum_continuation"],
        },
        "mid": {"start": "10:15", "end": "14:30"},
        "late": {
            "start": "14:30",
            "end": "15:15",
            "min_confidence": 80,
            "max_lots": 1,
            "blocked_setups": ["gap_and_go"],
        },
    },
}


def _signal(setup: str, conf: int = 85) -> CandidateSignal:
    return CandidateSignal(
        ts=datetime.now(tz=IST),
        setup_type=setup,
        side="CE",
        instrument_token="1",
        tsym="NIFTY",
        feature_snapshot=FeatureSnapshot(
            ts=datetime.now(tz=IST),
            nifty_spot=24000,
            session_vwap=23980,
            bias_5m=Bias.BULLISH,
            extra={"mtf_score_ce": 60},
        ),
        strategy_version="test",
        confidence=conf,
    )


def test_entry_window_blocks_before_930():
    t = datetime(2026, 7, 24, 9, 20, tzinfo=IST)
    assert not is_entry_window_open(VALIDATOR, now=t)


def test_entry_window_allows_midday():
    t = datetime(2026, 7, 24, 12, 0, tzinfo=IST)
    assert is_entry_window_open(VALIDATOR, now=t)


def test_entry_window_blocks_after_1515():
    t = datetime(2026, 7, 24, 15, 20, tzinfo=IST)
    assert not is_entry_window_open(VALIDATOR, now=t)


def test_open_phase_blocks_unlisted_setup():
    t = datetime(2026, 7, 24, 9, 45, tzinfo=IST)
    reasons = time_of_day_cautious_rejection_reasons(
        _signal("vwap_reclaim", 90), TOD_CFG, now=t
    )
    assert "tod_open_setup_not_allowed" in reasons


def test_late_phase_caps_lots():
    t = datetime(2026, 7, 24, 14, 45, tzinfo=IST)
    assert time_of_day_lot_cap(TOD_CFG, now=t) == 1


def test_late_phase_blocks_setup():
    t = datetime(2026, 7, 24, 14, 50, tzinfo=IST)
    reasons = time_of_day_cautious_rejection_reasons(
        _signal("gap_and_go", 90), TOD_CFG, now=t
    )
    assert "tod_late_setup_blocked" in reasons


DEAD_CFG = {
    "enabled": True,
    "hard_windows": [{"start": "11:30", "end": "12:15"}, {"start": "13:00", "end": "13:15"}],
    "soft_windows": [{"start": "10:45", "end": "11:30"}, {"start": "12:15", "end": "13:00"}, {"start": "13:45", "end": "14:00"}],
    "soft_min_nifty_5m_range": 15,
}


def test_hard_dead_zone_blocks():
    t = datetime(2026, 10, 16, 11, 45, tzinfo=IST)
    assert tod_dead_zone_rejection_reasons(DEAD_CFG, now=t) == ["tod_hard_dead_zone"]


def test_soft_dead_zone_blocks_without_thrust():
    t = datetime(2026, 10, 16, 10, 50, tzinfo=IST)
    assert tod_dead_zone_rejection_reasons(DEAD_CFG, now=t, spot_m1=[]) == ["tod_soft_no_thrust"]


def test_outside_dead_zone_allows():
    t = datetime(2026, 10, 16, 10, 0, tzinfo=IST)
    assert tod_dead_zone_rejection_reasons(DEAD_CFG, now=t) == []
