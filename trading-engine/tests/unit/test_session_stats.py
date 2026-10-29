from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from algoflat.risk.session_stats import SessionTradeStats, _consecutive_losses


def test_consecutive_losses_trailing():
    pnls = [
        Decimal("10"),
        Decimal("-5"),
        Decimal("-3"),
        Decimal("2"),
        Decimal("-1"),
        Decimal("-4"),
    ]
    assert _consecutive_losses(pnls) == 2


@pytest.mark.asyncio
async def test_fetch_session_trade_stats_live_only():
    from algoflat.risk import session_stats as mod

    rows = [
        {"pnl": Decimal("100")},
        {"pnl": Decimal("-40")},
        {"pnl": Decimal("25")},
    ]

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch.object(mod, "get_pool", return_value=pool):
        stats = await mod.fetch_session_trade_stats("live", trade_date=date(2026, 7, 27))

    assert stats == SessionTradeStats(
        realized_pnl=Decimal("85"),
        trade_count=3,
        consecutive_losses=0,
    )
