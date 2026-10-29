from algoflat.market_data.engine import (
    FRESH_SETUP_MIN_DISTINCT_DAYS,
    needs_fresh_setup_backfill,
)


def test_needs_fresh_setup_when_no_candles():
    need, reason = needs_fresh_setup_backfill({"count": 0, "distinct_days": 0})
    assert need is True
    assert reason == "no_candles_in_lookback"


def test_needs_fresh_setup_when_sparse():
    need, reason = needs_fresh_setup_backfill(
        {"count": 120, "distinct_days": FRESH_SETUP_MIN_DISTINCT_DAYS - 1}
    )
    assert need is True
    assert reason == "sparse_history"


def test_skips_fresh_setup_when_week_of_data():
    need, reason = needs_fresh_setup_backfill(
        {"count": 1200, "distinct_days": FRESH_SETUP_MIN_DISTINCT_DAYS}
    )
    assert need is False
    assert reason == "ok"
