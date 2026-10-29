from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from algoflat.contract_selector.scripmaster import nearest_future_from_scripmaster
from algoflat.symbols_util import (
    _is_futidx_row,
    index_spot_context,
    price_context,
    resolve_index_future_token,
    uses_dual_index_spot,
    uses_futures_price,
)


def test_uses_futures_price():
    assert uses_futures_price({"price_source": "futures"})
    assert not uses_futures_price({"price_source": "index"})
    assert not uses_futures_price({})


def test_price_context_futures():
    cfg = {
        "price_source": "futures",
        "exchange_futures": "NFO",
        "exchange_spot": "NFO",
        "spot_token": "61093",
    }
    assert price_context(cfg) == ("NFO", "61093")


def test_is_futidx_row_nifty():
    row = {"instname": "FUTIDX", "tsym": "NIFTY28JUL26F", "token": "61093"}
    assert _is_futidx_row(row, "NIFTY")
    assert not _is_futidx_row({"instname": "OPTIDX", "tsym": "NIFTY28JUL26C25000"}, "NIFTY")


@pytest.mark.asyncio
async def test_resolve_index_future_picks_nearest():
    broker = AsyncMock()
    broker.search_scrip = AsyncMock(
        return_value=[
            {
                "instname": "FUTIDX",
                "tsym": "NIFTY25SEP26F",
                "token": "70001",
                "exd": "25-SEP-2026",
            },
            {
                "instname": "FUTIDX",
                "tsym": "NIFTY28AUG26F",
                "token": "61093",
                "exd": "28-AUG-2026",
            },
        ]
    )
    result = await resolve_index_future_token(broker, "NIFTY", "NFO")
    assert result == ("61093", "NIFTY28AUG26F")


def test_nearest_future_from_scripmaster_rows():
    rows = [
        {
            "Symbol": "NIFTY",
            "Instrument": "FUTIDX",
            "Tradingsymbol": "NIFTY31JUL26F",
            "Token": "61093",
            "Expiry": "31-JUL-2026",
        },
        {
            "Symbol": "NIFTY",
            "Instrument": "FUTIDX",
            "Tradingsymbol": "NIFTY28AUG26F",
            "Token": "70001",
            "Expiry": "28-AUG-2026",
        },
        {
            "Symbol": "NIFTY",
            "Instrument": "OPTIDX",
            "Tradingsymbol": "NIFTY31JUL26C25000",
            "Token": "1",
            "Expiry": "31-JUL-2026",
        },
    ]
    from algoflat.contract_selector import scripmaster as sm

    original = sm.download_index_rows
    sm.download_index_rows = lambda exchange="NFO", url=None: rows  # type: ignore[assignment]
    try:
        as_of = date(2026, 7, 22)
        out = nearest_future_from_scripmaster(
            underlying="NIFTY", exchange="NFO", as_of=as_of
        )
        assert out == ("61093", "NIFTY31JUL26F")
    finally:
        sm.download_index_rows = original


def test_uses_dual_index_spot():
    cfg = {
        "price_source": "futures",
        "index_spot_exchange": "NSE",
        "index_spot_token": "26000",
        "spot_token": "61093",
    }
    assert uses_dual_index_spot(cfg)
    assert index_spot_context(cfg) == ("NSE", "26000")
    assert not uses_dual_index_spot({"price_source": "index"})


def test_fut_expiry_label_from_tsym():
    from algoflat.symbols_util import fut_expiry_label_from_tsym

    assert fut_expiry_label_from_tsym("NIFTY31JUL26F") == "31 JUL"
    assert fut_expiry_label_from_tsym("nifty28jul26f") == "28 JUL"
