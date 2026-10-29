from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID

import structlog

from algoflat.broker.base import BrokerAdapter
from algoflat.config import AppConfig
from algoflat.journal.writer import JournalWriter
from algoflat.models.events import (
  CandidateSignal,
  ExecutionRequest,
  OrderUpdate,
  TradingMode,
)
from algoflat.risk.engine import EntrySizing, RiskEngine
from algoflat.notifications.trade_alerts import build_trade_entry_alert
from algoflat.runtime.trading_mode import get_execution_mode

logger = structlog.get_logger(__name__)


class ExecutionEngine:
  def __init__(
    self,
    config: AppConfig,
    broker: BrokerAdapter,
    journal: JournalWriter,
    risk: RiskEngine,
  ) -> None:
    self._config = config
    self._broker = broker
    self._journal = journal
    self._risk = risk
    self._exec_cfg = config.execution

  async def enter(
    self,
    signal: CandidateSignal,
    sizing: EntrySizing,
  ) -> tuple[UUID, UUID, OrderUpdate]:
    exchange = signal.scanner_metadata.get("exchange", self._config.symbols["exchange_options"])
    client_id = f"AF-{signal.id.hex[:12]}"
    request = ExecutionRequest(
      client_order_id=client_id,
      ts=signal.ts,
      candidate_signal_id=signal.id,
      instrument_token=signal.instrument_token,
      exchange=exchange,
      tsym=signal.tsym,
      side="BUY",
      quantity=sizing.quantity,
      order_type=self._exec_cfg.get("order_type", "MKT"),
      limit_price=sizing.entry_ltp,
      product=self._exec_cfg.get("product", "MIS"),
      reference_ltp=sizing.entry_ltp,
      mode=TradingMode.LIVE if get_execution_mode() == "live" else TradingMode.PAPER,
    )

    order_id = await self._journal.write_order_created(request, signal.id)
    update = await self._broker.place_order(request)
    if update.status == "REJECTED":
      raise RuntimeError(update.rejection_reason or "broker rejected order")
    await self._journal.write_order_filled(order_id, update)

    fill = await self._resolve_entry_fill(order_id, update, sizing)
    position_id = await self._journal.write_position_opened(
      order_id=order_id,
      signal=signal,
      fill_price=fill,
      quantity=sizing.quantity,
      stop_loss=None,
      target=None,
      mode=request.mode.value,
    )
    entry_alert = await build_trade_entry_alert(
      self._risk,
      tsym=signal.tsym,
      fill=fill or Decimal("0"),
      quantity=sizing.quantity,
      setup_type=signal.setup_type,
      broker=self._broker,
    )
    await self._journal.write_notification(
      "trade",
      entry_alert.severity,
      entry_alert.title,
      entry_alert.message,
      related_entity="position",
      related_id=str(position_id),
    )
    logger.info(
      "entry_filled",
      mode=get_execution_mode(),
      tsym=signal.tsym,
      qty=sizing.quantity,
      fill=str(fill),
      position_id=str(position_id),
    )
    return position_id, order_id, update

  async def _resolve_entry_fill(
    self,
    order_id: UUID,
    update: OrderUpdate,
    sizing: EntrySizing,
  ) -> Decimal:
    db_fill = await self._journal.fetch_order_fill_price(order_id)
    if db_fill is not None and db_fill > 0:
      return db_fill
    if update.fill_price is not None and update.fill_price > 0:
      return update.fill_price
    if get_execution_mode() != "live":
      return sizing.entry_ltp
    raise RuntimeError(
      f"live_entry_missing_fill_price order_id={order_id} tsym={update.client_order_id}"
    )
