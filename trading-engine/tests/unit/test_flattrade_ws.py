"""Flattrade WebSocket helpers — subscription order and tick merge."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from algoflat.broker.flattrade import FlattradeAdapter
from algoflat.broker.flattrade_ws import FlattradeMarketSocket
from algoflat.config import AppConfig


def test_subscription_keys_bfo_first():
    ordered = FlattradeMarketSocket._order_bfo_first(
        ["NFO|1", "BFO|2", "NFO|3", "BFO|4", "NSE|5"]
    )
    assert ordered[:2] == ["BFO|2", "BFO|4"]
    assert ordered[2:] == ["NFO|1", "NFO|3", "NSE|5"]


def test_ping_interval_greater_than_timeout():
    assert FlattradeMarketSocket.PING_INTERVAL_SEC > FlattradeMarketSocket.PING_TIMEOUT_SEC


def test_feed_update_merges_incremental_tf_ticks():
    adapter = FlattradeAdapter(AppConfig())
    loop = asyncio.new_event_loop()
    adapter._loop = loop
    received = []

    def _cb(quote):
        received.append(quote)

    adapter._quote_callback = _cb
    adapter._register_subscription_keys(["NFO|840246"])

    adapter._handle_feed_update(
        {
            "t": "tk",
            "e": "NFO",
            "tk": "840246",
            "ts": "NIFTY28JUL26C24000",
            "lp": "120.50",
        }
    )
    adapter._handle_feed_update(
        {
            "t": "tf",
            "e": "NFO",
            "tk": "840246",
            "lp": "121.00",
        }
    )
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()

    assert len(received) == 2
    assert received[-1].ltp == Decimal("121.00")
    assert received[-1].instrument_token == "840246"
    assert received[-1].source == "websocket"
