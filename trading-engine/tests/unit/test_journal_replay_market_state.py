"""Unit tests for journal replay market_state helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from algoflat.models.events import Candle, CandleInterval
from algoflat.replay.market_state import bar_for_ts, bar_fraction, minute_start


def _candle(ts: datetime, o: str, h: str, l: str, c: str) -> Candle:
    return Candle(
        instrument_token="NIFTY",
        ts=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(l),
        close=Decimal(c),
        volume=100,
        interval=CandleInterval.M1,
    )


def test_bar_for_ts_finds_containing_minute():
    t0 = datetime(2026, 7, 28, 4, 45, tzinfo=timezone.utc)
    bars = [
        _candle(t0, "100", "101", "99", "100.5"),
        _candle(datetime(2026, 7, 28, 4, 46, tzinfo=timezone.utc), "100.5", "102", "100", "101"),
    ]
    hit = bar_for_ts(bars, datetime(2026, 7, 28, 4, 45, 30, tzinfo=timezone.utc))
    assert hit is not None
    assert hit.ts == t0


def test_bar_fraction_mid_minute():
    t0 = datetime(2026, 7, 28, 4, 45, tzinfo=timezone.utc)
    bar = _candle(t0, "100", "101", "99", "100.5")
    frac = bar_fraction(bar, datetime(2026, 7, 28, 4, 45, 30, tzinfo=timezone.utc))
    assert Decimal("0.5") <= frac <= Decimal("0.51")


def test_minute_start_strips_seconds():
    ts = datetime(2026, 7, 28, 10, 15, 42, tzinfo=timezone.utc)
    assert minute_start(ts) == datetime(2026, 7, 28, 10, 15, tzinfo=timezone.utc)
