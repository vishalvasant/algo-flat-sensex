"""Unit tests for journal replay trade comparison."""
from __future__ import annotations

from algoflat.replay.compare import compare_closed_trades


def _trade(
    *,
    entry_epoch: float,
    tsym: str = "NIFTY28JUL26C24000",
    side: str = "CE",
    pnl: float = 100.0,
) -> dict:
    return {
        "entry_ist": "2026-07-28 10:00:00",
        "exit_ist": "2026-07-28 10:15:00",
        "entry_epoch": entry_epoch,
        "tsym": tsym,
        "side": side,
        "setup_type": "vwap_reclaim",
        "lots": 1,
        "pnl": pnl,
        "exit_reason": "target_hit",
    }


def test_compare_trades_matches_within_tolerance():
    live = [_trade(entry_epoch=1000.0, pnl=150.0)]
    replay = [_trade(entry_epoch=1050.0, pnl=120.0)]
    out = compare_closed_trades(live, replay, tolerance_sec=120.0)
    assert out["matched_count"] == 1
    assert out["match_rate_pct"] == 100.0
    assert out["pnl_direction_agreement_pct"] == 100.0


def test_compare_trades_live_only_when_outside_tolerance():
    live = [_trade(entry_epoch=1000.0)]
    replay = [_trade(entry_epoch=2000.0)]
    out = compare_closed_trades(live, replay, tolerance_sec=60.0)
    assert out["live_only_count"] == 1
    assert out["replay_only_count"] == 1


def test_compare_trades_pnl_direction_mismatch():
    live = [_trade(entry_epoch=1000.0, pnl=100.0)]
    replay = [_trade(entry_epoch=1005.0, pnl=-50.0)]
    out = compare_closed_trades(live, replay)
    assert out["matched_count"] == 1
    assert out["pnl_direction_agreement_pct"] == 0.0
