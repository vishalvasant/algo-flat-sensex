"""Chain quote hydration from option_data."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from algoflat.config import get_config
from algoflat.main import TradingEngineApp
from algoflat.models.events import QuoteUpdate
from algoflat.option_data.layer import OptionDataLayer


def test_hydrate_chain_quotes_updates_ltp_from_option_data():
    app = TradingEngineApp.__new__(TradingEngineApp)
    app.option_data = OptionDataLayer(get_config())

    ts = datetime(2026, 7, 24, 6, 0, tzinfo=timezone.utc)
    app.option_data.update_from_quote(
        QuoteUpdate(
            ts=ts,
            exchange="NFO",
            instrument_token="99",
            tsym="NIFTY28JUL26P24000",
            ltp=Decimal("120.5"),
            source="rest",
        )
    )

    rows = app._hydrate_chain_quotes(
        [{"token": "99", "ltp": 100.0, "strike": 76500, "option_type": "CE"}]
    )
    assert rows[0]["ltp"] == 120.5
    assert rows[0]["last_update_ts"] == ts.isoformat()
