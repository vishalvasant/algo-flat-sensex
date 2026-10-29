from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from algoflat.main import TradingEngineApp
from algoflat.models.events import Candle, CandleInterval, Instrument


def _candle(token: str, close: float, minute: int) -> Candle:
    return Candle(
        instrument_token=token,
        ts=datetime(2026, 8, 29, 4, minute, tzinfo=timezone.utc),
        open=Decimal(str(close)),
        high=Decimal(str(close + 1)),
        low=Decimal(str(close - 1)),
        close=Decimal(str(close)),
        volume=10,
        interval=CandleInterval.M1,
    )


@pytest.mark.asyncio
async def test_option_chart_aggregates_m1_when_m5_missing(monkeypatch):
    app = TradingEngineApp.__new__(TradingEngineApp)
    app._universe = SimpleNamespace(
        instruments=[
            Instrument(
                exchange="NFO",
                token="12345",
                tsym="NIFTY29AUG2624600CE",
                underlying="NIFTY",
                strike=Decimal("24600"),
                option_type="CE",
                lot_size=75,
            )
        ]
    )
    app.option_data = MagicMock()
    app.option_data.get.return_value = None
    app._is_market_open = MagicMock(return_value=False)

    m1 = [_candle("12345", 100 + i, i) for i in range(10)]

    async def _load(tok, exch, interval, **kwargs):
        if interval == CandleInterval.M5:
            return []
        if interval == CandleInterval.M1:
            return m1
        return []

    app._load_option_interval_candles = AsyncMock(side_effect=_load)

    out = await app.get_instrument_chart_bars(
        "12345",
        exchange="NFO",
        tsym="NIFTY29AUG2624600CE",
        underlying="NIFTY",
        minutes=5,
        days=1,
    )

    assert out["price_source"] == "option"
    assert out["fut_tsym"] == "NIFTY29AUG2624600CE"
    assert len(out["bars"]) >= 1
    assert out["bars"][-1]["close"] < 500


def test_resolve_chart_instrument_uses_universe_exchange():
    app = TradingEngineApp.__new__(TradingEngineApp)
    app._universe = SimpleNamespace(
        instruments=[
            Instrument(
                exchange="BFO",
                token="999",
                tsym="BANKNIFTY29AUG2655200CE",
                underlying="BANKNIFTY",
                strike=Decimal("55200"),
                option_type="CE",
                lot_size=30,
            )
        ]
    )
    tok, exch, tsym = app._resolve_chart_instrument("999", None, "NFO")
    assert tok == "999"
    assert exch == "BFO"
    assert tsym == "BANKNIFTY29AUG2655200CE"
