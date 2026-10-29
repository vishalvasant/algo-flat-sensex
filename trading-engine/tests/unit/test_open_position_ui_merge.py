"""Managed open positions must use order-fill entry, not broker net avg."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from algoflat.main import TradingEngineApp
from algoflat.position.manager import OpenPosition


def _pm_pos(*, token: str, qty: int, entry: str) -> OpenPosition:
  return OpenPosition(
    position_id=uuid4(),
    order_id=uuid4(),
    candidate_signal_id=None,
    instrument_token=token,
    tsym="NIFTY15SEP26P23350",
    option_side="PE",
    quantity=qty,
    entry_price=Decimal(entry),
    entry_ts=datetime.now(tz=timezone.utc),
    premium_deployed=Decimal(entry) * qty,
    setup_type="momentum_continuation",
    mode="live",
    last_good_ltp=Decimal(entry),
    mfe=Decimal("1.50"),
  )


def test_ui_merge_keeps_algo_entry_when_broker_avg_differs():
  app = object.__new__(TradingEngineApp)
  app.option_data = MagicMock()
  app.option_data.get.return_value = SimpleNamespace(ltp=Decimal("60.00"))
  app._lot_size_for_position = lambda *_a, **_k: 65
  app._position_trail_fields = lambda pos, ltp: {
    "trail_floor": 58.5,
    "mfe_points": float(pos.mfe),
    "pnl_points": float(ltp - pos.entry_price),
  }

  pos = _pm_pos(token="47292", qty=65, entry="56.35")
  app.orchestrator = SimpleNamespace(
    positions=SimpleNamespace(open_positions=[pos])
  )
  app._broker_open_positions = [
    {
      "instrument_token": "47292",
      "tsym": "NIFTY15SEP26P23350",
      "side": "PE",
      "quantity": 260,
      "lot_size": 65,
      "lots": 4,
      "entry_price": 60.0,
      "broker_avg_price": 60.0,
      "entry_price_source": "broker_display_only",
      "current_ltp": 60.0,
      "unrealized_pnl": 0.0,
      "premium_deployed": 15600.0,
      "setup_type": "broker",
      "mode": "live",
      "source": "broker",
    }
  ]

  with patch("algoflat.main.is_live_execution", return_value=True):
    rows = TradingEngineApp.open_positions_for_ui(app)

  assert len(rows) == 1
  row = rows[0]
  assert row["entry_price"] == 56.35
  assert row["entry_price_source"] == "order_fill"
  assert row["quantity"] == 65
  assert row["lots"] == 1
  assert row["broker_quantity"] == 260
  assert row["broker_entry_price"] == 60.0
  assert row["broker_qty_mismatch"] is True
  assert abs(row["unrealized_pnl"] - (60.0 - 56.35) * 65) < 0.01
