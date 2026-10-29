from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from algoflat.broker.routed import RoutedBrokerAdapter
from algoflat.models.events import ExecutionRequest, TradingMode
from algoflat.risk.engine import DailyRiskSnapshot, RiskEngine, overlay_live_limits
from algoflat.runtime import trading_mode as tm


@pytest.fixture(autouse=True)
def reset_mode():
    tm.init_execution_mode("paper")
    yield
    tm.init_execution_mode("paper")


def test_overlay_live_limits_replaces_capital_only():
    snap = DailyRiskSnapshot(
        trade_date=date.today(),
        starting_capital=Decimal("1000"),
        available_capital=Decimal("500"),
        deployed_capital=Decimal("200"),
        realized_pnl=Decimal("50"),
        trade_count=3,
        consecutive_losses=1,
        kill_switch=False,
        entries_blocked=False,
        block_reason=None,
    )
    limits = MagicMock(
        cash=Decimal("25000"),
        available=Decimal("18000"),
        margin_used=Decimal("7000"),
        equity=Decimal("32000"),
    )
    out = overlay_live_limits(snap, limits)
    assert out.available_capital == Decimal("18000")
    assert out.starting_capital == Decimal("32000")
    assert out.realized_pnl == Decimal("50")
    assert out.equity == Decimal("32050")
    assert out.trade_count == 3


@pytest.mark.asyncio
async def test_routed_broker_uses_paper_by_default():
    live = MagicMock()
    live.place_order = AsyncMock()
    paper = MagicMock()
    paper.place_order = AsyncMock(return_value="paper-fill")
    broker = RoutedBrokerAdapter(live, paper)
    req = ExecutionRequest(
        client_order_id="AF-test",
        ts=datetime.now(tz=timezone.utc),
        instrument_token="1",
        exchange="NFO",
        tsym="NIFTYTEST",
        side="BUY",
        quantity=50,
        order_type="LMT",
        limit_price=Decimal("100"),
        product="MIS",
        reference_ltp=Decimal("100"),
        mode=TradingMode.PAPER,
    )
    result = await broker.place_order(req)
    paper.place_order.assert_awaited_once_with(req)
    live.place_order.assert_not_called()
    assert result == "paper-fill"


@pytest.mark.asyncio
async def test_routed_broker_uses_live_when_enabled():
    tm.init_execution_mode("live")
    live = MagicMock()
    live.place_order = AsyncMock(return_value="live-fill")
    paper = MagicMock()
    paper.place_order = AsyncMock()
    broker = RoutedBrokerAdapter(live, paper)
    req = ExecutionRequest(
        client_order_id="AF-test",
        ts=datetime.now(tz=timezone.utc),
        instrument_token="1",
        exchange="NFO",
        tsym="NIFTYTEST",
        side="BUY",
        quantity=50,
        order_type="LMT",
        limit_price=Decimal("100"),
        product="MIS",
        reference_ltp=Decimal("100"),
        mode=TradingMode.LIVE,
    )
    result = await broker.place_order(req)
    live.place_order.assert_awaited_once_with(req)
    assert result == "live-fill"


@pytest.mark.asyncio
async def test_sync_live_account_from_broker_updates_capital(monkeypatch):
    tm.init_execution_mode("live")
    config = MagicMock()
    config.symbols = {"underlying": "NIFTY"}
    risk = RiskEngine(config)  # type: ignore[arg-type]

    base_snap = DailyRiskSnapshot(
        trade_date=date.today(),
        starting_capital=Decimal("1000"),
        available_capital=Decimal("500"),
        deployed_capital=Decimal("200"),
        realized_pnl=Decimal("10"),
        trade_count=2,
        consecutive_losses=0,
        kill_switch=False,
        entries_blocked=False,
        block_reason=None,
    )
    risk.ensure_daily_state = AsyncMock(return_value=base_snap)  # type: ignore[method-assign]

    from algoflat.risk import engine as risk_engine
    from algoflat.risk import session_stats as ss

    monkeypatch.setattr(
        ss,
        "fetch_session_trade_stats",
        AsyncMock(
            return_value=MagicMock(
                trade_count=2,
                consecutive_losses=0,
                realized_pnl=Decimal("10"),
            )
        ),
    )

    conn = AsyncMock()
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    from algoflat.db import connection as db_conn

    monkeypatch.setattr(db_conn, "get_pool", lambda: pool)
    monkeypatch.setattr(risk_engine, "get_pool", lambda: pool)
    monkeypatch.setattr(
        risk_engine,
        "fetch_session_trade_stats",
        AsyncMock(
            return_value=MagicMock(
                trade_count=2,
                consecutive_losses=0,
                realized_pnl=Decimal("10"),
            )
        ),
    )

    broker_day = MagicMock(realized_pnl=Decimal("125"), total_pnl=Decimal("130"))
    limits = MagicMock(
        cash=Decimal("25000"),
        available=Decimal("18000"),
        margin_used=Decimal("7000"),
        equity=Decimal("32000"),
    )
    live_broker = MagicMock()
    live_broker.fetch_broker_snapshot = AsyncMock(return_value=(limits, broker_day, []))

    out = await risk.sync_live_account_from_broker(live_broker)

    assert out is not None
    assert out.available_capital == Decimal("18000")
    assert out.realized_pnl == Decimal("125")
    assert out.trade_count == 2
    conn.execute.assert_awaited_once()
