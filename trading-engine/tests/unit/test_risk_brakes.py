"""Risk brake — entries_blocked must persist after consecutive losses."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from algoflat.risk.engine import RiskEngine


@pytest.mark.asyncio
async def test_maybe_block_entries_on_consecutive_losses():
  config = SimpleNamespace(
    risk={
      "max_consecutive_losses": 3,
      "max_daily_loss": 2500,
      "block_entries_on_consecutive_losses": True,
      "block_entries_on_daily_loss": True,
    },
    symbols={"underlying": "NIFTY"},
  )
  engine = RiskEngine(config)  # type: ignore[arg-type]
  conn = AsyncMock()
  row = {
    "starting_capital": Decimal("25000"),
    "consecutive_losses": 3,
    "realized_pnl": Decimal("-500"),
    "entries_blocked": False,
  }
  await engine._maybe_block_entries(conn, date.today(), "NIFTY", row)
  conn.execute.assert_called_once()
  sql = conn.execute.call_args[0][0]
  assert "entries_blocked = TRUE" in sql
  assert conn.execute.call_args[0][3] == "max_consecutive_losses"


def test_max_daily_loss_is_30pct_of_starting():
  config = SimpleNamespace(
    risk={
      "max_daily_loss_pct_of_starting": 30,
      "max_daily_loss": 0,
      "block_entries_on_daily_loss": True,
    },
    symbols={"underlying": "NIFTY"},
  )
  engine = RiskEngine(config)  # type: ignore[arg-type]
  assert engine._max_daily_loss_limit(Decimal("28386.50")) == Decimal("8515.95")
  assert engine._max_daily_loss_limit(Decimal("10000")) == Decimal("3000.00")


@pytest.mark.asyncio
async def test_maybe_block_entries_on_pct_daily_loss():
  config = SimpleNamespace(
    risk={
      "max_consecutive_losses": 0,
      "max_daily_loss_pct_of_starting": 30,
      "max_daily_loss": 0,
      "block_entries_on_consecutive_losses": False,
      "block_entries_on_daily_loss": True,
    },
    symbols={"underlying": "NIFTY"},
  )
  engine = RiskEngine(config)  # type: ignore[arg-type]
  conn = AsyncMock()
  # 30% of 10000 = 3000 → block when realized <= -3000
  row = {
    "starting_capital": Decimal("10000"),
    "consecutive_losses": 0,
    "realized_pnl": Decimal("-3000"),
    "entries_blocked": False,
  }
  await engine._maybe_block_entries(conn, date.today(), "NIFTY", row)
  conn.execute.assert_called_once()
  assert conn.execute.call_args[0][3] == "max_daily_loss"
