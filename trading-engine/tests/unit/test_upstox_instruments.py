"""Unit tests for Upstox broker helpers."""
from __future__ import annotations

from algoflat.broker.instruments import from_upstox_instrument_key, to_upstox_instrument_key


def test_nifty_index_key():
    assert to_upstox_instrument_key("NSE", "26000") == "NSE_INDEX|Nifty 50"


def test_nfo_option_key():
    assert to_upstox_instrument_key("NFO", "45278") == "NSE_FO|45278"


def test_india_vix_key():
    assert to_upstox_instrument_key("NSE", "India VIX") == "NSE_INDEX|India VIX"


def test_roundtrip_nse_index():
    ex, tok = from_upstox_instrument_key("NSE_INDEX|India VIX")
    assert ex == "NSE"
    assert tok == "India VIX"
