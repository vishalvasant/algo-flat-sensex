from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from algoflat.position.manager import PositionManager


def _make_manager() -> PositionManager:
    config = MagicMock()
    config.symbols = {"exchange_options": "NFO"}
    config.execution = {"product": "MIS"}
    config.position_exit = {}
    broker = MagicMock()
    journal = MagicMock()
    journal.fetch_order_fill_price = AsyncMock(return_value=Decimal("24.60"))
    journal.update_position_entry_price = AsyncMock()
    risk = MagicMock()
    market_data = MagicMock()
    return PositionManager(config, broker, journal, risk, market_data)


@pytest.mark.asyncio
async def test_register_open_from_order_uses_db_fill():
    pm = _make_manager()
    order_id = uuid4()
    position_id = uuid4()
    signal = MagicMock()
    signal.confidence = 90
    signal.scanner_metadata = {}
    signal.instrument_token = "1"
    signal.tsym = "NIFTY28JUL26P24000"
    signal.side = "PE"
    signal.setup_type = "vwap_trend"
    signal.feature_snapshot.model_dump.return_value = {}
    sizing = MagicMock()
    sizing.quantity = 390
    sizing.premium_required = Decimal("9594")
    update = MagicMock()
    update.fill_price = Decimal("99.99")

    fill = await pm.register_open_from_order(
        position_id,
        order_id,
        uuid4(),
        signal,
        sizing,
        update,
        mode="live",
    )

    assert fill == Decimal("24.60")
    pos = pm._open[position_id]
    assert pos.entry_price == Decimal("24.60")
    assert pos.last_good_ltp == Decimal("24.60")
    pm._journal.update_position_entry_price.assert_awaited_once_with(
        position_id, Decimal("24.60")
    )
