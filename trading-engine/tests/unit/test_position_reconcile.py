from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from algoflat.position.manager import OpenPosition, PositionManager


def _make_manager() -> PositionManager:
    config = MagicMock()
    config.symbols = {"exchange_options": "NFO"}
    config.execution = {"product": "MIS"}
    config.position_exit = {}
    config.risk = {"cooldown_after_exit_minutes": 0}
    broker = MagicMock()
    journal = MagicMock()
    journal.write_adopted_position = AsyncMock(
        return_value=(uuid4(), uuid4())
    )
    journal.fetch_order_fill_price = AsyncMock(return_value=None)
    journal.fetch_order_fill_details = AsyncMock(return_value=(None, None))
    journal.update_position_entry_price = AsyncMock()
    risk = MagicMock()
    risk.is_force_exit_time = MagicMock(return_value=False)
    market_data = MagicMock()
    return PositionManager(config, broker, journal, risk, market_data)


@pytest.mark.asyncio
async def test_reconcile_adopts_broker_only_holding():
    pm = _make_manager()
    broker_row = {
        "instrument_token": "12345",
        "tsym": "NIFTY28JUL26C24000",
        "quantity": 65,
        "entry_price": 100.0,
    }

    with patch.object(pm, "_load_open_from_db", AsyncMock(return_value=False)):
        with patch.object(pm, "_lookup_algo_fill_price", AsyncMock(return_value=Decimal("100"))):
            with patch(
                "algoflat.position.manager.get_execution_mode",
                return_value="live",
            ):
                result = await pm.reconcile_broker_holdings([broker_row])

    assert result["adopted"] == 1
    assert pm.open_count == 1
    pos = pm.open_positions[0]
    assert pos.instrument_token == "12345"
    assert pos.setup_type == "broker_adopted"


@pytest.mark.asyncio
async def test_reconcile_skips_adopt_without_order_fill():
    pm = _make_manager()
    broker_row = {
        "instrument_token": "12345",
        "tsym": "NIFTY28JUL26C24000",
        "quantity": 65,
        "entry_price": 100.0,
    }

    with patch.object(pm, "_load_open_from_db", AsyncMock(return_value=False)):
        with patch.object(pm, "_lookup_algo_fill_price", AsyncMock(return_value=None)):
            with patch(
                "algoflat.position.manager.get_execution_mode",
                return_value="live",
            ):
                result = await pm.reconcile_broker_holdings([broker_row])

    assert result["adopted"] == 0
    assert pm.open_count == 0


@pytest.mark.asyncio
async def test_reconcile_external_close_requires_two_misses():
    pm = _make_manager()
    pos_id = uuid4()
    pm._open[pos_id] = OpenPosition(
        position_id=pos_id,
        order_id=uuid4(),
        candidate_signal_id=None,
        instrument_token="99999",
        tsym="NIFTY28JUL26C24000",
        option_side="CE",
        quantity=65,
        entry_price=Decimal("100"),
        entry_ts=datetime.now(tz=timezone.utc),
        premium_deployed=Decimal("6500"),
        setup_type="vwap_reclaim",
        mode="live",
    )
    pm._close_external = AsyncMock()

    with patch(
        "algoflat.position.manager.get_execution_mode",
        return_value="live",
    ):
        first = await pm.reconcile_broker_holdings([])
        second = await pm.reconcile_broker_holdings([])

    assert first["external_closed"] == 0
    assert second["external_closed"] == 1
    pm._close_external.assert_awaited_once()
