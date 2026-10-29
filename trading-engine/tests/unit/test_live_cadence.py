"""Live cadence parity: periodic entry scans and throttled exit evaluation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from algoflat.models.events import QuoteUpdate
from algoflat.position.manager import OpenPosition, PositionManager
from algoflat.trading.orchestrator import TradingOrchestrator


def _pm_config(*, exit_interval: float = 2.0) -> MagicMock:
    config = MagicMock()
    config.symbols = {"exchange_options": "NFO"}
    config.execution = {"product": "MIS"}
    config.position_exit = {
        "min_hold_seconds": 0,
        "max_ltp_jump_pct_per_tick": 15,
        "exit_confirm_ticks": 1,
        "adverse_move_pct_from_entry": 4,
        "confidence_adverse_scaling": False,
        "trail_step_arm_mfe_pct": 4,
        "trail_step_buffer_pct": 2,
    }
    config.runtime = {"exit_eval_interval_seconds": exit_interval}
    return config


def _open_pos(pm: PositionManager, *, entry: Decimal = Decimal("100"), token: str = "111") -> None:
    pos_id = uuid4()
    pm._open[pos_id] = OpenPosition(
        position_id=pos_id,
        order_id=uuid4(),
        candidate_signal_id=None,
        instrument_token=token,
        tsym="NIFTY04AUG26C24350",
        option_side="CE",
        quantity=65,
        entry_price=entry,
        entry_ts=datetime.now(tz=timezone.utc),
        premium_deployed=entry * Decimal(65),
        setup_type="opening_range_breakout",
        mode="live",
        last_good_ltp=entry,
    )


@pytest.mark.asyncio
async def test_exit_eval_throttled_to_interval():
    pm = PositionManager(_pm_config(exit_interval=2.0), MagicMock(), MagicMock(), MagicMock(), MagicMock())
    _open_pos(pm)
    pos_id = next(iter(pm._open.keys()))

    async def _close_and_remove(close_pos_id, *_args, **_kwargs):
        pm._open.pop(close_pos_id, None)

    pm._close_position = AsyncMock(side_effect=_close_and_remove)

    base = datetime(2026, 7, 30, 10, 0, 0, tzinfo=timezone.utc)
    quote1 = QuoteUpdate(
        exchange="NFO",
        instrument_token="111",
        tsym="NIFTY04AUG26C24350",
        ltp=Decimal("95"),
        ts=base,
    )
    quote2 = QuoteUpdate(
        exchange="NFO",
        instrument_token="111",
        tsym="NIFTY04AUG26C24350",
        ltp=Decimal("94"),
        ts=base + timedelta(seconds=1),
    )
    await pm.on_quote(quote1)
    await pm.on_quote(quote2)
    assert pm._close_position.await_count == 1


@pytest.mark.asyncio
async def test_orchestrator_skips_entry_scan_on_quote_when_disabled():
    config = MagicMock()
    config.runtime = {
        "scan_interval_seconds": 10,
        "entry_scan_on_quote": False,
    }
    config.strategy = {"router": {"log_every_decision": False}, "entry_confirmation": {}}
    config.validator = {"premium_responsive": {}}
    config.symbols = {"strike_step": 50}

    orch = TradingOrchestrator.__new__(TradingOrchestrator)
    orch._config = config
    orch._entry_scan_on_quote = False
    orch._scan_lock = __import__("asyncio").Lock()
    orch._universe = MagicMock()
    orch.positions = MagicMock()
    orch.positions._pending_flips = []
    orch._scan_for_entry = AsyncMock()
    orch._process_pending_flips = AsyncMock()
    orch._market_open = MagicMock(return_value=True)

    quote = QuoteUpdate(
        exchange="NFO",
        instrument_token="111",
        tsym="X",
        ltp=Decimal("10"),
        ts=datetime.now(tz=timezone.utc),
    )
    orch.positions.on_quote = AsyncMock()
    await orch.on_quote(quote)

    orch._scan_for_entry.assert_not_awaited()
    orch.positions.on_quote.assert_awaited_once()
