"""NIFTY header spot — cash index vs futures separation."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from algoflat.config import get_config
from algoflat.main import TradingEngineApp
from algoflat.models.events import Candle, CandleInterval


def _nifty_app() -> TradingEngineApp:
    app = TradingEngineApp.__new__(TradingEngineApp)
    app.config = get_config()
    app._active_underlying = "NIFTY"
    app._ticker_only_symbols = set()
    app._index_spot_ltps = {}
    app._futures_spot_ltps = {}
    app._sticky_index_spots = {}
    app._session_open_by_symbol = {}
    app._prev_close_by_symbol = {}
    app._universes = {}
    app._fut_contract_meta = {"NIFTY": {"expiry_label": "31JUL26", "tsym": "NIFTY31JUL26F"}}
    app._chain_item_cache = {}
    app.contract_selector = MagicMock()
    app.contract_selector.retarget_atm = lambda uni, spot, strike_step: uni
    app.contract_selector.atm_strike_for_spot = MagicMock()
    app.option_data = MagicMock()
    app.option_data.get = MagicMock(return_value=None)
    app.market_data = MagicMock()
    app.market_data.spot_ltp = Decimal("25120")
    app.market_data.candles = MagicMock(return_value=[])
    app.market_data.candles_for_token = MagicMock(return_value=[])
    return app


def test_header_spot_uses_cash_index_not_futures():
    app = _nifty_app()
    app._index_spot_ltps["NIFTY"] = Decimal("25000")
    app._futures_spot_ltps["NIFTY"] = Decimal("25120")

    assert app._header_spot("NIFTY") == Decimal("25000")
    assert app._trading_spot("NIFTY") == Decimal("25120")


def test_atm_spot_uses_futures_not_cash_index():
    app = _nifty_app()
    app._index_spot_ltps["NIFTY"] = Decimal("25000")
    app._futures_spot_ltps["NIFTY"] = Decimal("25120")

    assert app._atm_spot("NIFTY") == Decimal("25120")


def test_commodity_row_spot_matches_cash_session_open():
    app = _nifty_app()
    app._index_spot_ltps["NIFTY"] = Decimal("25050")
    app._futures_spot_ltps["NIFTY"] = Decimal("25120")
    app._session_open_by_symbol["NIFTY"] = {date.today(): Decimal("24900")}
    app._prev_close_by_symbol["NIFTY"] = Decimal("24900")

    row = app._commodity_snapshot_row(
        "NIFTY",
        None,
        default_step=50,
        default_band=250,
        default_atm_steps=5,
    )
    assert row["spot_ltp"] == 25050.0
    assert row["trading_spot_ltp"] == 25120.0
    assert row["session_open"] == 24900.0
    assert row["prev_close"] == 24900.0
    assert row["change"] == 150.0


def test_header_change_uses_previous_close_not_session_open():
    """Broker day change is LTP − previous close (Flattrade ``c``), not today's open."""
    app = _nifty_app()
    app._index_spot_ltps["NIFTY"] = Decimal("23014.60")
    app._session_open_by_symbol["NIFTY"] = {date.today(): Decimal("23065.10")}
    app._prev_close_by_symbol["NIFTY"] = Decimal("22997.35")

    row = app._commodity_snapshot_row(
        "NIFTY",
        None,
        default_step=50,
        default_band=250,
        default_atm_steps=5,
    )
    assert row["session_open"] == 23065.10
    assert row["prev_close"] == 22997.35
    assert round(row["change"], 2) == 17.25
    assert round(row["change_pct"], 2) == 0.08


def test_fut_card_uses_futures_session_open():
    app = _nifty_app()
    app._futures_spot_ltps["NIFTY"] = Decimal("25120")
    app._session_open_by_symbol["NIFTY_FUT"] = {date.today(): Decimal("25000")}
    app._prev_close_by_symbol["NIFTY_FUT"] = Decimal("25000")

    row = app._fut_header_snapshot_row("NIFTY")
    assert row is not None
    assert row["underlying"] == "NIFTY_FUT"
    assert row["spot_ltp"] == 25120.0
    assert row["session_open"] == 25000.0
    assert row["change"] == 120.0


def test_sector_session_open_does_not_reuse_nifty_candles():
    app = _nifty_app()
    nifty_bar = Candle(
        instrument_token="26000",
        ts=datetime(2026, 10, 8, 3, 45, tzinfo=timezone.utc),
        open=Decimal("22599"),
        high=Decimal("22620"),
        low=Decimal("22480"),
        close=Decimal("22450"),
        volume=0,
        interval=CandleInterval.M1,
    )
    app.market_data.candles = MagicMock(return_value=[nifty_bar])
    app.market_data.candles_for_token = MagicMock(return_value=[])

    exchange, token, cache_key = app._session_open_context("BANKNIFTY")
    assert exchange == "NSE"
    assert token == "26009"
    assert cache_key == "BANKNIFTY"
    assert app._session_open_for_symbol("BANKNIFTY") is None
    app.market_data.candles.assert_not_called()
    app.market_data.candles_for_token.assert_called_with("26009", CandleInterval.M1)


def test_quote_day_open_replaces_a_wrong_session_open():
    app = _nifty_app()
    app._session_open_by_symbol["BANKNIFTY"] = {date.today(): Decimal("22599")}
    app._lock_quote_day_open("BANKNIFTY", {"o": "54880.15", "lp": "54921.20"})
    assert app._session_open_for_symbol("BANKNIFTY") == Decimal("54880.15")


def test_session_open_uses_cash_token_for_nifty():
    app = _nifty_app()
    cash_bar = Candle(
        instrument_token="26000",
        ts=datetime(2026, 7, 22, 3, 45, tzinfo=timezone.utc),
        open=Decimal("24800"),
        high=Decimal("24850"),
        low=Decimal("24790"),
        close=Decimal("24820"),
        volume=0,
        interval=CandleInterval.M1,
    )
    app.market_data.candles_for_token = MagicMock(return_value=[cash_bar])

    exchange, token, cache_key = app._session_open_context("NIFTY")
    assert exchange == "NSE"
    assert token == "26000"
    assert cache_key == "NIFTY"

    open_px = app._session_open_for_symbol("NIFTY")
    assert open_px == Decimal("24800")
    app.market_data.candles_for_token.assert_called_with("26000", CandleInterval.M1)


def test_trading_spot_prefers_coalesced_futures_over_junk_md():
    app = _nifty_app()
    app.market_data.spot_ltp = Decimal("27602")  # after-hours junk
    app._futures_spot_ltps["NIFTY"] = Decimal("24434.20")  # last candle
    app._is_market_open = MagicMock(return_value=False)
    assert app._trading_spot("NIFTY") == Decimal("24434.20")


@pytest.mark.asyncio
async def test_coalesce_rejects_after_hours_junk_lp():
    app = _nifty_app()
    app._is_market_open = MagicMock(return_value=False)
    app._last_close_from_db = AsyncMock(return_value=None)
    app._last_cash_index_close = AsyncMock(return_value=Decimal("24366.70"))

    trusted = await app._coalesce_spot_with_db(
        "NIFTY",
        Decimal("26830.60"),
        token="26000",
        exchange="NSE",
        token_kind="cash",
        prev_close=Decimal("24383.60"),
    )
    assert trusted == Decimal("24366.70")


def test_watchlist_snapshot_prefers_cash_spot_ltp():
    app = _nifty_app()
    app._index_spot_ltps["NIFTY"] = Decimal("25000")
    app._futures_spot_ltps["NIFTY"] = Decimal("25120")
    app._build_header_commodity_rows = MagicMock(
        return_value=[
            {
                "underlying": "NIFTY",
                "spot_ltp": 25000.0,
                "trading_spot_ltp": 25120.0,
                "atm_strike": 25000.0,
                "expiry_symbol": "31JUL26",
                "items": [],
            }
        ]
    )
    app.open_positions_for_ui = MagicMock(return_value=[])
    app._universe = None
    app._side_confidence_snapshot = MagicMock(return_value={})
    app._feed_mode = "rest"
    app.broker = MagicMock(websocket_open=False)
    app._is_market_open = MagicMock(return_value=False)

    snap = app.get_watchlist_snapshot()
    assert snap["spot_ltp"] == 25000.0
