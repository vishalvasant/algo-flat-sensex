from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from algoflat.features.indicators import aggregate_from_m5
from algoflat.models.events import Candle, CandleInterval

IST = ZoneInfo("Asia/Kolkata")


def _m5(hour: int, minute: int, close: str) -> Candle:
    ts = datetime(2026, 7, 23, hour, minute, tzinfo=IST).astimezone(timezone.utc)
    px = Decimal(close)
    return Candle(
        instrument_token="1",
        ts=ts,
        open=px,
        high=px,
        low=px,
        close=px,
        volume=100,
        interval=CandleInterval.M5,
    )


def test_aggregate_from_m5_aligns_to_ist_15m_buckets():
    m5 = [
        _m5(9, 15, "100"),
        _m5(9, 20, "101"),
        _m5(9, 25, "102"),
        _m5(9, 30, "103"),
        _m5(9, 35, "104"),
    ]
    bars = aggregate_from_m5(m5, 15)
    assert len(bars) == 2
    first_ist = bars[0].ts.astimezone(IST)
    second_ist = bars[1].ts.astimezone(IST)
    assert first_ist.hour == 9 and first_ist.minute == 15
    assert second_ist.hour == 9 and second_ist.minute == 30
    assert bars[0].close == Decimal("102")
    assert bars[1].close == Decimal("104")
    assert bars[0].interval == CandleInterval.M15
