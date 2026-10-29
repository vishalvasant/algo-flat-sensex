"""Unit tests for journal replay signal comparison."""
from __future__ import annotations

from algoflat.replay.compare import compare_signal_decisions


def _row(
    *,
    ts_epoch: float,
    setup: str = "vwap_reclaim",
    side: str = "CE",
    tsym: str = "NIFTY28JUL26C24000",
    passed: bool = True,
) -> dict:
    return {
        "ts_ist": "2026-07-28 10:00:00",
        "ts_epoch": ts_epoch,
        "setup_type": setup,
        "side": side,
        "tsym": tsym,
        "validator_passed": passed,
        "rejection_reasons": [] if passed else ["spread_wide"],
        "entered": passed,
    }


def test_compare_matches_within_window():
    live = [_row(ts_epoch=1000.0, passed=True)]
    replay = [_row(ts_epoch=1005.0, passed=True)]
    out = compare_signal_decisions(live, replay, window_sec=10.0)
    assert out["matched_count"] == 1
    assert out["live_only_count"] == 0
    assert out["replay_only_count"] == 0
    assert out["validator_agreement_pct"] == 100.0


def test_compare_flags_validator_mismatch():
    live = [_row(ts_epoch=1000.0, passed=True)]
    replay = [_row(ts_epoch=1001.0, passed=False)]
    out = compare_signal_decisions(live, replay)
    assert out["matched_count"] == 1
    assert len(out["validator_mismatch"]) == 1
    assert out["validator_agreement_pct"] == 0.0


def test_compare_live_only_when_no_match():
    live = [_row(ts_epoch=1000.0, setup="breakout")]
    replay = [_row(ts_epoch=1000.0, setup="vwap_reclaim")]
    out = compare_signal_decisions(live, replay)
    assert out["live_only_count"] == 1
    assert out["replay_only_count"] == 1
