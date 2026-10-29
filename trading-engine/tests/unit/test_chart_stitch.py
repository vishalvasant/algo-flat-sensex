"""Futures chart candle stitching — front month must win overlaps."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from algoflat.market_data.engine import MarketDataEngine
from algoflat.models.events import Candle, CandleInterval


def _bar(token: str, minute: int, close: str) -> Candle:
    return Candle(
        instrument_token=token,
        ts=datetime(2026, 7, 31, 9, minute, tzinfo=timezone.utc),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1000,
        interval=CandleInterval.M5,
    )


def test_stitch_prefers_front_month_over_far_months():
    by_token = {
        "48704": [_bar("48704", 55, "24680")],  # Oct
        "58072": [_bar("58072", 55, "24434.2")],  # Aug front
        "68407": [_bar("68407", 55, "24560.6")],  # Sep
    }
    stitched = MarketDataEngine.stitch_contract_candles(
        by_token, preferred_token="58072"
    )
    assert len(stitched) == 1
    assert stitched[0].instrument_token == "58072"
    assert stitched[0].close == Decimal("24434.2")


def test_stitch_fills_gaps_from_older_contract():
    by_token = {
        "111": [
            _bar("111", 40, "24000"),
            _bar("111", 45, "24010"),
        ],
        "222": [
            _bar("222", 50, "24100"),
            _bar("222", 55, "24110"),
        ],
    }
    stitched = MarketDataEngine.stitch_contract_candles(
        by_token, preferred_token="222"
    )
    assert [float(c.close) for c in stitched] == [24000.0, 24010.0, 24100.0, 24110.0]
    assert stitched[-1].instrument_token == "222"
