from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from algoflat.models.events import QuoteUpdate
from algoflat.position.manager import OpenPosition, PositionManager


def _make_manager() -> PositionManager:
    config = MagicMock()
    config.symbols = {"exchange_options": "NFO"}
    config.execution = {"product": "MIS"}
    config.position_exit = {
        "min_hold_seconds": 0,
        "max_ltp_jump_pct_per_tick": 15,
        "exit_confirm_ticks": 2,
        "adverse_move_pct_from_entry": 4,
        "confidence_adverse_scaling": False,
        "trail_step_arm_mfe_pct": 4,
        "trail_step_buffer_pct": 2,
    }
    config.runtime = {"exit_eval_interval_seconds": 0}
    broker = MagicMock()
    journal = MagicMock()
    risk = MagicMock()
    risk.is_force_exit_time = MagicMock(return_value=False)
    market_data = MagicMock()
    return PositionManager(config, broker, journal, risk, market_data)


def _open_pos(pm: PositionManager, *, entry: Decimal = Decimal("20"), token: str = "111") -> None:
    pos_id = uuid4()
    pm._open[pos_id] = OpenPosition(
        position_id=pos_id,
        order_id=uuid4(),
        candidate_signal_id=None,
        instrument_token=token,
        tsym="NIFTY28JUL26P24000",
        option_side="PE",
        quantity=390,
        entry_price=entry,
        entry_ts=datetime.now(tz=timezone.utc),
        premium_deployed=entry * Decimal(390),
        setup_type="opening_range_breakout",
        mode="live",
        last_good_ltp=entry,
    )


@pytest.mark.asyncio
async def test_rejects_spike_quote_and_skips_exit():
    pm = _make_manager()
    _open_pos(pm)
    pos_id = next(iter(pm._open.keys()))
    pm._close_position = AsyncMock()

    spike = QuoteUpdate(
        exchange="NFO",
        instrument_token="111",
        tsym="NIFTY28JUL26P24000",
        ltp=Decimal("5"),
        ts=datetime.now(tz=timezone.utc),
    )
    await pm.on_quote(spike)

    pos = pm._open[pos_id]
    assert pos.last_good_ltp == Decimal("20")
    pm._close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_soft_exit_requires_confirm_ticks():
    pm = _make_manager()
    _open_pos(pm)
    pos_id = next(iter(pm._open.keys()))
    pm._close_position = AsyncMock()

    quote = QuoteUpdate(
        exchange="NFO",
        instrument_token="111",
        tsym="NIFTY28JUL26P24000",
        ltp=Decimal("19.9"),
        ts=datetime.now(tz=timezone.utc),
    )
    warm = QuoteUpdate(
        exchange="NFO",
        instrument_token="111",
        tsym="NIFTY28JUL26P24000",
        ltp=Decimal("20.5"),
        ts=datetime.now(tz=timezone.utc),
    )
    await pm.on_quote(warm)
    await pm.on_quote(quote)
    assert pm._close_position.await_count == 0
    await pm.on_quote(quote)

    assert pm._close_position.await_count == 1
    pm._close_position.assert_awaited_with(pos_id, Decimal("19.9"), "momentum_trail")


@pytest.mark.asyncio
async def test_reconcile_skips_adopt_during_exit_cooldown():
    pm = _make_manager()
    pm._config.position_exit["post_exit_adopt_cooldown_seconds"] = 120
    pm._last_exit_by_token["12345"] = datetime.now(tz=timezone.utc)
    broker_row = {
        "instrument_token": "12345",
        "tsym": "NIFTY28JUL26C24000",
        "quantity": 65,
        "entry_price": 100.0,
    }

    with patch.object(pm, "_load_open_from_db", AsyncMock(return_value=False)):
        with patch(
            "algoflat.position.manager.get_execution_mode",
            return_value="live",
        ):
            result = await pm.reconcile_broker_holdings([broker_row])

    assert result["adopted"] == 0
    assert pm.open_count == 0


def test_entry_blocked_while_pending_exit():
    pm = _make_manager()
    pm._pending_exit_tokens.add("tok-ce")
    assert pm.entry_blocked_for_token("tok-ce") is True
    assert pm.entry_blocked_for_token("other") is False


def test_loss_exit_extends_token_cooldown():
    pm = _make_manager()
    pm._config.risk = {
        "cooldown_after_exit_minutes": 2,
        "loss_exit_cooldown_minutes": 10,
    }
    token = "tok-ce"
    pm._last_exit_by_token[token] = datetime.now(tz=timezone.utc)
    pm._last_exit_any = datetime.now(tz=timezone.utc)
    pm._last_loss_exit_by_token[token] = datetime.now(tz=timezone.utc)
    assert pm.in_cooldown(token) is True
