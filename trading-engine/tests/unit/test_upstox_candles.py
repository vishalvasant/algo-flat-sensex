"""Upstox candle interval handling."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from algoflat.broker.upstox import UpstoxAdapter
from algoflat.config import AppConfig
from algoflat.models.events import Candle, CandleInterval


def _m1_bar(offset_min: int) -> Candle:
    ts = datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc) + timedelta(minutes=offset_min)
    return Candle(
        instrument_token="12345",
        ts=ts,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=100,
        interval=CandleInterval.M1,
    )


@pytest.mark.asyncio
async def test_m5_aggregated_from_m1():
    adapter = UpstoxAdapter(AppConfig())
    start = datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 28, 5, 0, tzinfo=timezone.utc)
    m1 = [_m1_bar(i) for i in range(10)]
    adapter._fetch_m1_candles = AsyncMock(return_value=m1)  # type: ignore[method-assign]

    m5 = await adapter.get_candles("NFO", "12345", CandleInterval.M5, start, end)
    assert len(m5) == 2
    assert m5[0].interval == CandleInterval.M5
    adapter._fetch_m1_candles.assert_awaited_once()
