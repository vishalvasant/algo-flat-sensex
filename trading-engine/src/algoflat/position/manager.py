from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Any
from uuid import UUID, uuid4

import structlog

from algoflat.broker.base import BrokerAdapter
from algoflat.config import AppConfig
from algoflat.journal.writer import JournalWriter
from algoflat.market_data.engine import MarketDataEngine
from algoflat.models.events import CandleInterval, ExecutionRequest, QuoteUpdate, TradingMode
from algoflat.position.exit_rules import (
  ExitDecision,
  _momentum_trail_floor,
  evaluate_momentum_exit,
)
from algoflat.symbols_util import lot_size_for_tsym
from algoflat.risk.engine import RiskEngine
from algoflat.risk.greeks_sizing import greeks_confirmation_multiplier
from algoflat.notifications.trade_alerts import build_trade_exit_alert
from algoflat.runtime.trading_mode import get_execution_mode

logger = structlog.get_logger(__name__)


@dataclass
class OpenPosition:
  position_id: UUID
  order_id: UUID
  candidate_signal_id: UUID | None
  instrument_token: str
  tsym: str
  option_side: str
  quantity: int
  entry_price: Decimal
  entry_ts: datetime
  premium_deployed: Decimal
  setup_type: str
  mode: str = "paper"
  is_expiry_day: bool = False
  confidence: int | None = None
  mfe: Decimal = Decimal("0")
  mae: Decimal = Decimal("0")
  last_quote_ts: datetime | None = None
  last_good_ltp: Decimal | None = None
  worst_ltp_since_eval: Decimal | None = None
  persisted_mfe: Decimal = Decimal("0")
  signal_snapshot: dict = field(default_factory=dict)
  entry_meta: dict = field(default_factory=dict)


class PositionManager:
  def __init__(
    self,
    config: AppConfig,
    broker: BrokerAdapter,
    journal: JournalWriter,
    risk: RiskEngine,
    market_data: MarketDataEngine,
  ) -> None:
    self._config = config
    self._broker = broker
    self._journal = journal
    self._risk = risk
    self._market_data = market_data
    self._open: dict[UUID, OpenPosition] = {}
    self._last_exit_by_token: dict[str, datetime] = {}
    self._last_exit_any: datetime | None = None
    self._regime_primary: str | None = None
    self._pending_flips: list[dict] = []
    self._trade_close_hook = None
    self._trade_open_hook = None
    self._broker_absent_streak: dict[str, int] = {}
    self._exit_confirm_streak: dict[UUID, tuple[str, int]] = {}
    self._pending_exit_tokens: set[str] = set()
    self._last_loss_exit_by_token: dict[str, datetime] = {}
    self._exit_backoff_until: dict[str, datetime] = {}
    runtime = getattr(config, "runtime", None) or {}
    self._exit_eval_interval = float(
      runtime.get("exit_eval_interval_seconds", 3)
    )
    self._last_exit_eval: dict[UUID, datetime] = {}
    mark_cfg = runtime.get("option_mark") or {}
    self._mark_enabled = bool(mark_cfg.get("enabled", True))
    self._prefer_mid = bool(mark_cfg.get("prefer_mid", True))
    self._ema_alpha = Decimal(str(mark_cfg.get("ema_alpha", 0.35)))
    self._mark_ema: dict[UUID, Decimal] = {}

  def exit_eval_interval_seconds(self) -> float:
    return self._exit_eval_interval

  def set_trade_close_hook(self, hook) -> None:
    self._trade_close_hook = hook

  def set_trade_open_hook(self, hook) -> None:
    """Called after a new position is registered (sync; may schedule async work)."""
    self._trade_open_hook = hook

  def set_regime(self, primary: str | None) -> None:
    self._regime_primary = primary

  @property
  def open_positions(self) -> list[OpenPosition]:
    return list(self._open.values())

  @property
  def has_open_position(self) -> bool:
    return bool(self._open)

  @property
  def open_count(self) -> int:
    return len(self._open)

  def has_open_for_token(self, instrument_token: str) -> bool:
    return any(p.instrument_token == instrument_token for p in self._open.values())

  def entry_blocked_for_token(self, instrument_token: str) -> bool:
    """Block entry while open, exiting, or in post-exit cooldown."""
    if instrument_token in self._pending_exit_tokens:
      return True
    if self.has_open_for_token(instrument_token):
      return True
    return self.in_cooldown(instrument_token)

  def position_by_token(self, instrument_token: str) -> OpenPosition | None:
    for pos in self._open.values():
      if pos.instrument_token == instrument_token:
        return pos
    return None

  def register_adopted(
    self,
    position_id: UUID,
    order_id: UUID,
    *,
    instrument_token: str,
    tsym: str,
    option_side: str,
    quantity: int,
    fill_price: Decimal,
    mode: str = "live",
  ) -> None:
    prem = fill_price * Decimal(quantity)
    self._open[position_id] = OpenPosition(
      position_id=position_id,
      order_id=order_id,
      candidate_signal_id=None,
      instrument_token=instrument_token,
      tsym=tsym,
      option_side=option_side,
      quantity=quantity,
      entry_price=fill_price,
      entry_ts=datetime.now(tz=timezone.utc),
      premium_deployed=prem,
      setup_type="broker_adopted",
      mode=mode,
      signal_snapshot={},
      last_good_ltp=fill_price,
    )
    if self._trade_open_hook is not None:
      try:
        self._trade_open_hook(self._open[position_id])
      except Exception:
        logger.exception("trade_open_hook_failed", tsym=tsym)

  def total_deployed(self) -> Decimal:
    return sum((p.premium_deployed for p in self._open.values()), Decimal("0"))

  def unrealized_pnl(self, option_data, *, mode: str | None = None) -> Decimal:
    total = Decimal("0")
    for pos in self._open.values():
      if mode is not None and pos.mode != mode:
        continue
      state = option_data.get(pos.instrument_token)
      ltp = state.ltp if state and state.ltp is not None else pos.entry_price
      if pos.last_good_ltp is not None and pos.last_good_ltp > 0:
        ltp = pos.last_good_ltp
      total += (ltp - pos.entry_price) * pos.quantity
    return total

  def register_open(
    self,
    position_id: UUID,
    order_id: UUID,
    signal_id: UUID | None,
    signal,
    sizing,
    fill_price: Decimal,
    *,
    is_expiry_day: bool = False,
    mode: str | None = None,
  ) -> None:
    confidence = signal.confidence
    if confidence is None:
      confidence = int(signal.scanner_metadata.get("confidence", 0) or 0)
    meta = signal.scanner_metadata or {}
    pick = meta.get("strike_pick") or {}
    risk_cfg = self._config.risk if isinstance(getattr(self._config, "risk", None), dict) else {}
    greeks_mult = greeks_confirmation_multiplier(signal, None, risk_cfg)
    entry_meta = {
      "greeks_mult": float(greeks_mult),
      "volume": meta.get("volume") or meta.get("option_volume"),
      "oi": meta.get("oi") or meta.get("option_oi"),
      "spread_pct": pick.get("spread_pct") or meta.get("spread_pct"),
      "delta": pick.get("delta"),
    }
    self._open[position_id] = OpenPosition(
      position_id=position_id,
      order_id=order_id,
      candidate_signal_id=signal_id,
      instrument_token=signal.instrument_token,
      tsym=signal.tsym,
      option_side=signal.side,
      quantity=sizing.quantity,
      entry_price=fill_price,
      entry_ts=datetime.now(tz=timezone.utc),
      premium_deployed=sizing.premium_required,
      setup_type=signal.setup_type,
      mode=mode or get_execution_mode(),
      is_expiry_day=is_expiry_day,
      confidence=confidence or None,
      signal_snapshot=signal.feature_snapshot.model_dump(mode="json"),
      entry_meta=entry_meta,
      last_good_ltp=fill_price,
    )
    if self._trade_open_hook is not None:
      try:
        self._trade_open_hook(self._open[position_id])
      except Exception:
        logger.exception("trade_open_hook_failed", tsym=signal.tsym)

  async def register_open_from_order(
    self,
    position_id: UUID,
    order_id: UUID,
    signal_id: UUID | None,
    signal,
    sizing,
    update,
    *,
    is_expiry_day: bool = False,
    mode: str | None = None,
  ) -> Decimal:
    """Register position using fill price from the placed order only."""
    fill = await self._entry_fill_from_order(order_id, update, sizing)
    self.register_open(
      position_id,
      order_id,
      signal_id,
      signal,
      sizing,
      fill,
      is_expiry_day=is_expiry_day,
      mode=mode,
    )
    await self._journal.update_position_entry_price(position_id, fill)
    return fill

  async def _entry_fill_from_order(self, order_id: UUID, update, sizing) -> Decimal:
    db_fill = await self._journal.fetch_order_fill_price(order_id)
    if db_fill is not None and db_fill > 0:
      return db_fill
    fill = getattr(update, "fill_price", None)
    if fill is not None and fill > 0:
      return fill
    if get_execution_mode() != "live":
      return sizing.entry_ltp
    raise RuntimeError(f"live_entry_missing_fill_price order_id={order_id}")

  async def refresh_entry_prices_from_orders(
    self,
    *,
    trade_book_rows: list[dict] | None = None,
  ) -> int:
    """Re-sync open position entry from broker trade-book fills (never broker net avg)."""
    from algoflat.broker.broker_trades import fill_price_for_broker_order, open_lot_from_trade_book

    updated = 0
    for pos in list(self._open.values()):
      if pos.mode != "live":
        continue
      fill, broker_order_id = await self._journal.fetch_order_fill_details(pos.order_id)
      # Prefer the exact BUY order fills when we have a broker order id.
      if trade_book_rows and broker_order_id:
        matched = fill_price_for_broker_order(
          trade_book_rows, broker_order_id, side="BUY"
        )
        if matched is not None:
          fill = matched[0]
      # Adopted / stale DB fills have no broker id — use open-lot VWAP for this qty.
      if trade_book_rows and (broker_order_id is None or fill is None or fill <= 0):
        lot = open_lot_from_trade_book(trade_book_rows, pos.tsym)
        if lot is not None and lot[1] == pos.quantity:
          fill = lot[0]
      elif trade_book_rows and fill is not None and fill > 0:
        # Even with an order fill, correct when open-lot (same qty) disagrees —
        # e.g. AF-ADOPT reused an older trial fill price.
        lot = open_lot_from_trade_book(trade_book_rows, pos.tsym)
        if lot is not None and lot[1] == pos.quantity and lot[0] != fill:
          fill = lot[0]
      if fill is None or fill <= 0 or fill == pos.entry_price:
        continue
      logger.warning(
        "position_entry_corrected",
        tsym=pos.tsym,
        old=str(pos.entry_price),
        new=str(fill),
        order_id=str(pos.order_id),
        broker_order_id=broker_order_id,
      )
      pos.entry_price = fill
      pos.last_good_ltp = fill
      pos.premium_deployed = fill * Decimal(pos.quantity)
      await self._journal.update_position_entry_price(pos.position_id, fill)
      updated += 1
    return updated

  def entry_price_for_position(self, pos: OpenPosition) -> Decimal:
    """Entry used for P&L / trails — always the in-memory order fill."""
    return pos.entry_price

  def _stable_mark(
    self,
    *,
    pos_id: UUID,
    ltp: Decimal,
    bid: Decimal | None,
    ask: Decimal | None,
  ) -> Decimal:
    """Mid or EMA mark so one WS flicker cannot trail-stop the book."""
    if not self._mark_enabled:
      return ltp
    sample = ltp
    if self._prefer_mid and bid is not None and ask is not None and ask > bid:
      sample = ((bid + ask) / Decimal("2")).quantize(Decimal("0.05"))
    prev = self._mark_ema.get(pos_id)
    if prev is None:
      mark = sample
    else:
      mark = (
        self._ema_alpha * sample + (Decimal("1") - self._ema_alpha) * prev
      ).quantize(Decimal("0.05"))
    self._mark_ema[pos_id] = mark
    return mark

  def _ltp_is_sane(self, pos: OpenPosition, ltp: Decimal) -> bool:
    if ltp <= 0:
      return False
    exit_cfg = self._config.position_exit
    max_jump_pct = Decimal(str(exit_cfg.get("max_ltp_jump_pct_per_tick", 15))) / Decimal("100")
    ref = pos.last_good_ltp if pos.last_good_ltp and pos.last_good_ltp > 0 else pos.entry_price
    if ref <= 0:
      return True
    return abs(ltp - ref) / ref <= max_jump_pct

  def _confirm_exit(self, pos_id: UUID, reason: str) -> bool:
    # Session-end flatten does not wait. Every other exit needs a second agreeing eval.
    if reason == "force_exit":
      return True
    required = int(self._config.position_exit.get("exit_confirm_ticks", 2))
    if required <= 1:
      return True
    prev = self._exit_confirm_streak.get(pos_id)
    streak = prev[1] + 1 if prev and prev[0] == reason else 1
    self._exit_confirm_streak[pos_id] = (reason, streak)
    return streak >= required

  def _cooldown_seconds(self, *, seconds_key: str, minutes_key: str, default_minutes: int) -> int:
    risk = self._config.risk
    if seconds_key in risk:
      return max(0, int(risk[seconds_key]))
    return max(0, int(risk.get(minutes_key, default_minutes))) * 60

  def _in_exit_cooldown(self, token: str) -> bool:
    last = self._last_exit_by_token.get(token)
    if last is None:
      return False
    cooldown = int(self._config.position_exit.get("post_exit_adopt_cooldown_seconds", 20))
    return (datetime.now(tz=timezone.utc) - last).total_seconds() < cooldown

  async def on_quote(self, quote: QuoteUpdate) -> None:
    if not self._open or quote.ltp is None:
      return

    exit_cfg = self._config.position_exit
    force = self._risk.is_force_exit_time()
    now = quote.ts or datetime.now(tz=timezone.utc)
    if now.tzinfo is None:
      now = now.replace(tzinfo=timezone.utc)

    for pos_id in list(self._open.keys()):
      pos = self._open.get(pos_id)
      if pos is None or quote.instrument_token != pos.instrument_token:
        continue

      backoff_until = self._exit_backoff_until.get(pos.instrument_token)
      if backoff_until is not None and now < backoff_until:
        continue

      ltp = quote.ltp
      if not self._ltp_is_sane(pos, ltp):
        logger.warning(
          "position_quote_rejected",
          tsym=pos.tsym,
          ltp=str(ltp),
          last_good=str(pos.last_good_ltp or pos.entry_price),
        )
        continue

      pos.last_quote_ts = now
      pos.last_good_ltp = ltp
      # Trail arms and trips on traded LTP. A bid-ask mid must not lift the stop above the print.
      pos.mfe = max(pos.mfe, ltp - pos.entry_price)
      pos.mae = min(pos.mae, ltp - pos.entry_price)
      if pos.worst_ltp_since_eval is None:
        pos.worst_ltp_since_eval = ltp
      else:
        pos.worst_ltp_since_eval = min(pos.worst_ltp_since_eval, ltp)

      last_eval = self._last_exit_eval.get(pos_id)
      eval_due = (
        self._exit_eval_interval <= 0
        or force
        or last_eval is None
        or (now - last_eval).total_seconds() >= self._exit_eval_interval
      )
      if not eval_due:
        continue
      self._last_exit_eval[pos_id] = now
      if pos.mfe > pos.persisted_mfe:
        pos.persisted_mfe = pos.mfe
        try:
          await self._journal.update_position_extremes(pos.position_id, pos.mfe, pos.mae)
        except Exception:
          logger.exception("position_mfe_persist_failed", tsym=pos.tsym)

      # Worst print since the previous check, so a wick through the trail is not missed.
      stop_px = min(ltp, pos.worst_ltp_since_eval or ltp)
      pos.worst_ltp_since_eval = ltp

      option_m1 = self._market_data.candles_for_token(pos.instrument_token, CandleInterval.M1)
      decision = evaluate_momentum_exit(
        option_side=pos.option_side,
        entry_price=pos.entry_price,
        entry_ts=pos.entry_ts,
        current_ltp=stop_px,
        mfe_points=pos.mfe,
        market_data=self._market_data,
        cfg=exit_cfg,
        force_exit=force,
        regime_primary=self._regime_primary,
        quantity=pos.quantity,
        is_expiry_day=pos.is_expiry_day,
        confidence=pos.confidence,
        setup_type=pos.setup_type,
        entry_meta=pos.entry_meta,
        now=now,
        option_m1=option_m1,
      )
      trail = _momentum_trail_floor(pos.entry_price, pos.mfe, exit_cfg)
      trail_reason = (
        "profit_trail"
        if exit_cfg.get("profit_trail_lock_pct") is not None
        else "momentum_trail"
      )
      if (
        not decision.should_exit
        and trail is not None
        and stop_px <= trail
      ):
        decision = ExitDecision(True, trail_reason)
      if decision.should_exit:
        reason = decision.reason or "exit"
        if not self._confirm_exit(pos_id, reason):
          streak = self._exit_confirm_streak.get(pos_id, (reason, 0))[1]
          if reason in ("momentum_trail", "profit_trail") and trail is None:
            trail = _momentum_trail_floor(pos.entry_price, pos.mfe, exit_cfg)
          logger.info(
            "exit_confirm_pending",
            tsym=pos.tsym,
            reason=reason,
            streak=streak,
            required=int(exit_cfg.get("exit_confirm_ticks", 2)),
            ltp=str(ltp),
            stop_px=str(stop_px),
            trail_floor=str(trail) if trail is not None else None,
          )
          continue
        self._exit_confirm_streak.pop(pos_id, None)
        self._mark_ema.pop(pos_id, None)
        closed_side = pos.option_side
        await self._close_position(pos_id, ltp, reason)
        from algoflat.position.exit_rules import should_queue_reversal_flip

        if should_queue_reversal_flip(
          exit_reason=reason,
          option_side=closed_side,
          market_data=self._market_data,
          exit_cfg=exit_cfg,
        ):
          opposite = "PE" if closed_side == "CE" else "CE"
          self._pending_flips.append(
            {
              "side": opposite,
              "from_side": closed_side,
              "reason": "trend_reversal_flip",
              "ts": datetime.now(tz=timezone.utc),
            }
          )
          logger.info(
            "trend_reversal_flip_queued",
            from_side=closed_side,
            to_side=opposite,
          )
      else:
        self._exit_confirm_streak.pop(pos_id, None)
        await self._maybe_scale_out(pos, ltp, exit_cfg)

  async def flatten(self, reason: str = "kill_switch") -> None:
    for pos_id in list(self._open.keys()):
      pos = self._open[pos_id]
      ltp = pos.entry_price
      cache = getattr(self._broker, "_ltp_cache", None)
      if cache and pos.instrument_token in cache:
        ltp = cache[pos.instrument_token]
      await self._close_position(pos_id, ltp, reason)

  def _resolve_ltp(self, pos: OpenPosition, preferred: Decimal | None = None) -> Decimal:
    if preferred is not None:
      return preferred
    cache = getattr(self._broker, "_ltp_cache", None)
    if cache and pos.instrument_token in cache:
      return cache[pos.instrument_token]
    return pos.entry_price

  async def manual_exit(
    self,
    position_id: UUID,
    exit_price: Decimal | None = None,
  ) -> dict:
    """Square-off one open position at current LTP (or provided price)."""
    pos = self._open.get(position_id)
    if pos is None:
      raise KeyError("position_not_found")
    return await self._manual_exit_position(pos, position_id, exit_price)

  async def manual_exit_by_token(
    self,
    instrument_token: str,
    exit_price: Decimal | None = None,
    exit_reason: str = "manual_exit",
  ) -> dict:
    pos = self.position_by_token(instrument_token)
    if pos is None:
      raise KeyError("position_not_found")
    return await self._manual_exit_position(pos, pos.position_id, exit_price, exit_reason)

  async def _manual_exit_position(
    self,
    pos: OpenPosition,
    position_id: UUID,
    exit_price: Decimal | None,
    exit_reason: str = "manual_exit",
  ) -> dict:
    ltp = self._resolve_ltp(pos, exit_price)
    tsym = pos.tsym
    qty = pos.quantity
    entry = pos.entry_price
    fill = await self._close_position(position_id, ltp, exit_reason)
    pnl = (fill - entry) * qty
    return {
      "ok": True,
      "position_id": str(position_id),
      "tsym": tsym,
      "quantity": qty,
      "entry_price": float(entry),
      "exit_price": float(fill),
      "pnl": float(pnl),
      "exit_reason": exit_reason,
    }

  def _partial_sell_qty(self, pos: OpenPosition, exit_cfg: dict) -> int:
    if pos.entry_meta.get("partial_booked"):
      return 0
    if not exit_cfg.get("partial_exit_enabled", False):
      return 0
    if pos.entry_price <= 0:
      return 0
    arm = Decimal(str(exit_cfg.get("partial_exit_mfe_pct", 3))) / Decimal("100")
    if pos.mfe < pos.entry_price * arm:
      return 0
    lot = lot_size_for_tsym(pos.tsym)
    if lot <= 0 or pos.quantity < lot * 2:
      return 0
    fraction = Decimal(str(exit_cfg.get("partial_exit_fraction", "0.5")))
    raw = int((Decimal(pos.quantity) * fraction).to_integral_value(rounding=ROUND_FLOOR))
    sell_qty = (raw // lot) * lot
    if sell_qty < lot:
      sell_qty = lot
    if pos.quantity - sell_qty < lot:
      return 0
    return sell_qty

  async def _maybe_scale_out(self, pos: OpenPosition, ltp: Decimal, exit_cfg: dict) -> None:
    """Sell part of a winner, keep the rest under a wider trail."""
    sell_qty = self._partial_sell_qty(pos, exit_cfg)
    if sell_qty <= 0 or pos.instrument_token in self._pending_exit_tokens:
      return
    self._pending_exit_tokens.add(pos.instrument_token)
    try:
      now = datetime.now(tz=timezone.utc)
      client_id = f"AF-P-{uuid4().hex[:12]}"
      request = ExecutionRequest(
        client_order_id=client_id,
        ts=now,
        candidate_signal_id=pos.candidate_signal_id,
        instrument_token=pos.instrument_token,
        exchange=self._config.symbols["exchange_options"],
        tsym=pos.tsym,
        side="SELL",
        quantity=sell_qty,
        order_type="MKT",
        limit_price=ltp,
        product=self._config.execution.get("product", "MIS"),
        reference_ltp=ltp,
        mode=TradingMode.LIVE if get_execution_mode() == "live" else TradingMode.PAPER,
      )
      exit_order_id = await self._journal.write_order_created(request, pos.candidate_signal_id)
      update = await self._broker.place_order(request)
      await self._journal.write_order_filled(exit_order_id, update)
      if update.status == "REJECTED":
        raise RuntimeError(update.rejection_reason or "broker rejected partial exit")
      fill = update.fill_price or ltp
      pnl = (fill - pos.entry_price) * sell_qty
      held = int((now - pos.entry_ts).total_seconds())
      await self._journal.write_closed_trade(
        position=pos,
        exit_ts=now,
        exit_price=fill,
        pnl=pnl,
        exit_reason="partial_target",
        hold_seconds=held,
        mode=pos.mode,
        quantity=sell_qty,
      )
      sold_premium = pos.entry_price * Decimal(sell_qty)
      pos.quantity -= sell_qty
      pos.premium_deployed = pos.entry_price * Decimal(pos.quantity)
      pos.entry_meta["partial_booked"] = True
      await self._journal.update_position_quantity(pos.position_id, pos.quantity)
      await self._risk.release_capital(sold_premium, pnl, "NIFTY")
      logger.info(
        "position_partial_exit",
        tsym=pos.tsym,
        sold_qty=sell_qty,
        remaining=pos.quantity,
        pnl=str(pnl),
        fill=str(fill),
      )
    except Exception:
      logger.exception("partial_exit_failed", tsym=pos.tsym)
    finally:
      self._pending_exit_tokens.discard(pos.instrument_token)

  async def _close_position(self, position_id: UUID, exit_price: Decimal, exit_reason: str) -> Decimal:
    pos = self._open.pop(position_id, None)
    if pos is None:
      return exit_price
    self._last_exit_eval.pop(position_id, None)
    self._exit_confirm_streak.pop(position_id, None)
    self._mark_ema.pop(position_id, None)

    now = datetime.now(tz=timezone.utc)
    # Block re-entry while the sell is in flight (_pending_exit_tokens). Cooldown
    # timestamps are set only after a successful fill so a rejected exit does not
    # leave an open position under an active post-exit cooldown.
    self._pending_exit_tokens.add(pos.instrument_token)
    try:
      fill = await self._close_position_inner(pos, exit_price, exit_reason, exit_started_at=now)
      self._exit_backoff_until.pop(pos.instrument_token, None)
      return fill
    except Exception:
      self._open[position_id] = pos
      # Avoid hammering Flattrade with rejected exit retries every eval tick.
      backoff_sec = int(self._config.position_exit.get("exit_reject_backoff_seconds", 20))
      self._exit_backoff_until[pos.instrument_token] = now + timedelta(seconds=max(5, backoff_sec))
      raise
    finally:
      self._pending_exit_tokens.discard(pos.instrument_token)

  async def _close_position_inner(
    self,
    pos: OpenPosition,
    exit_price: Decimal,
    exit_reason: str,
    *,
    exit_started_at: datetime | None = None,
  ) -> Decimal:
    position_id = pos.position_id
    now = datetime.now(tz=timezone.utc)
    hold_seconds = int((now - pos.entry_ts).total_seconds())

    # Unique per attempt — Flattrade remarks max ~20 chars; retries must not reuse id.
    client_id = f"AF-E-{uuid4().hex[:12]}"
    request = ExecutionRequest(
      client_order_id=client_id,
      ts=now,
      candidate_signal_id=pos.candidate_signal_id,
      instrument_token=pos.instrument_token,
      exchange=self._config.symbols["exchange_options"],
      tsym=pos.tsym,
      side="SELL",
      quantity=pos.quantity,
      order_type="MKT",
      limit_price=exit_price,
      product=self._config.execution.get("product", "MIS"),
      reference_ltp=exit_price,
      mode=TradingMode.LIVE if get_execution_mode() == "live" else TradingMode.PAPER,
    )
    exit_order_id = await self._journal.write_order_created(request, pos.candidate_signal_id)
    update = await self._broker.place_order(request)
    await self._journal.write_order_filled(exit_order_id, update)
    if update.status == "REJECTED":
      raise RuntimeError(update.rejection_reason or "broker rejected exit order")

    fill = update.fill_price or exit_price
    if update.fill_price is None or update.fill_price <= 0:
      logger.warning(
        "exit_fill_missing_using_reference",
        tsym=pos.tsym,
        reference=str(exit_price),
        broker_order_id=update.broker_order_id,
      )
    pnl = (fill - pos.entry_price) * pos.quantity
    exit_at = datetime.now(tz=timezone.utc)
    self._last_exit_by_token[pos.instrument_token] = exit_at
    self._last_exit_any = exit_at

    await self._journal.write_position_closed(
      position_id=pos.position_id,
      mfe=pos.mfe,
      mae=pos.mae,
    )
    await self._journal.write_closed_trade(
      position=pos,
      exit_ts=now,
      exit_price=fill,
      pnl=pnl,
      exit_reason=exit_reason,
      hold_seconds=hold_seconds,
      mode=pos.mode,
    )
    await self._risk.release_capital(
      pos.premium_deployed,
      pnl,
      "NIFTY",
    )

    exit_alert = await build_trade_exit_alert(
        self._risk,
        tsym=pos.tsym,
        fill=fill,
        trade_pnl=pnl,
        quantity=pos.quantity,
        exit_reason=exit_reason,
        broker=self._broker,
      )
    await self._journal.write_notification(
      "trade",
      exit_alert.severity,
      exit_alert.title,
      exit_alert.message,
      related_entity="position",
      related_id=str(pos.position_id),
    )
    logger.info("position_exit", tsym=pos.tsym, pnl=str(pnl), reason=exit_reason, mode=pos.mode, fill=str(fill))
    if pnl < 0:
      self._last_loss_exit_by_token[pos.instrument_token] = exit_at
    if self._trade_close_hook is not None:
      try:
        self._trade_close_hook(
          pos.setup_type, pnl, exit_reason, is_expiry_day=pos.is_expiry_day
        )
      except Exception:
        logger.exception("trade_close_hook_failed", tsym=pos.tsym)
    return fill

  async def _close_external(
    self,
    position_id: UUID,
    exit_price: Decimal,
    exit_reason: str,
  ) -> None:
    """Close PM state when the broker holding was already squared off manually."""
    pos = self._open.pop(position_id, None)
    if pos is None:
      return

    now = datetime.now(tz=timezone.utc)
    hold_seconds = int((now - pos.entry_ts).total_seconds())
    fill = exit_price
    pnl = (fill - pos.entry_price) * pos.quantity

    await self._journal.write_position_closed(
      position_id=pos.position_id,
      mfe=pos.mfe,
      mae=pos.mae,
    )
    await self._journal.write_closed_trade(
      position=pos,
      exit_ts=now,
      exit_price=fill,
      pnl=pnl,
      exit_reason=exit_reason,
      hold_seconds=hold_seconds,
      mode=pos.mode,
    )
    await self._risk.release_capital(
      pos.premium_deployed,
      pnl,
      "NIFTY",
    )
    from algoflat.notifications.trade_alerts import _option_label, _pnl_short

    leg = _option_label(pos.tsym)
    await self._journal.write_notification(
      "trade",
      "info" if pnl >= 0 else "warning",
      f"Exit · {'WIN' if pnl >= 0 else 'LOSS'} {_pnl_short(pnl)}",
      f"{pos.mode.upper()} · {leg}\n"
      f"Broker square-off · Sold @ ₹{fill:.2f}\n"
      f"Trade {_pnl_short(pnl)}",
      related_entity="position",
      related_id=str(pos.position_id),
    )
    logger.info(
      "position_external_close",
      tsym=pos.tsym,
      pnl=str(pnl),
      reason=exit_reason,
      mode=pos.mode,
    )
    self._last_exit_by_token[pos.instrument_token] = now
    self._last_exit_any = now
    if pnl < 0:
      self._last_loss_exit_by_token[pos.instrument_token] = now

  def in_cooldown(self, instrument_token: str | None = None) -> bool:
    cooldown_sec = self._cooldown_seconds(
      seconds_key="cooldown_after_exit_seconds",
      minutes_key="cooldown_after_exit_minutes",
      default_minutes=3,
    )
    loss_sec = self._cooldown_seconds(
      seconds_key="loss_exit_cooldown_seconds",
      minutes_key="loss_exit_cooldown_minutes",
      default_minutes=10,
    )
    if cooldown_sec <= 0 and loss_sec <= 0:
      return False
    now = datetime.now(tz=timezone.utc)
    # Global cooldown after any exit — blocks strike-hopping thrash.
    if cooldown_sec > 0 and self._last_exit_any is not None:
      if (now - self._last_exit_any).total_seconds() < cooldown_sec:
        return True
    if instrument_token:
      last_loss = self._last_loss_exit_by_token.get(instrument_token)
      if last_loss is not None and loss_sec > 0:
        if (now - last_loss).total_seconds() < loss_sec:
          return True
      last = self._last_exit_by_token.get(instrument_token)
      if last is None:
        return False
      if cooldown_sec <= 0:
        return False
      return (now - last).total_seconds() < cooldown_sec
    return False

  def clear_cooldowns(self) -> None:
    """Drop exit cooldowns (used after paper account reset)."""
    self._last_exit_by_token.clear()
    self._last_exit_any = None
    self._last_loss_exit_by_token.clear()

  def stale_holding_tokens(self, max_age_sec: float = 3.0) -> list[str]:
    """Holdings with no fresh quote — WS may have dropped the token."""
    now = datetime.now(tz=timezone.utc)
    stale: list[str] = []
    for pos in self._open.values():
      last = pos.last_quote_ts
      if last is None:
        age = (now - pos.entry_ts).total_seconds()
      else:
        if last.tzinfo is None:
          last = last.replace(tzinfo=timezone.utc)
        age = (now - last).total_seconds()
      if age > max_age_sec:
        stale.append(pos.instrument_token)
    return stale

  async def _load_open_from_db(self, instrument_token: str) -> bool:
    import re

    from algoflat.db.connection import get_pool

    if self.has_open_for_token(instrument_token):
      return True

    pool = get_pool()
    async with pool.acquire() as conn:
      row = await conn.fetchrow(
        """
        SELECT
          p.id,
          p.order_id,
          p.instrument_token,
          p.tsym,
          p.side,
          p.quantity,
          COALESCE(o.fill_price, p.entry_price) AS entry_price,
          p.entry_ts,
          p.mfe,
          p.mae,
          p.mode,
          o.candidate_signal_id,
          cs.setup_type
        FROM positions p
        LEFT JOIN orders o ON o.id = p.order_id
        LEFT JOIN candidate_signals cs ON cs.id = o.candidate_signal_id
        WHERE p.status = 'OPEN' AND p.instrument_token = $1
        ORDER BY p.entry_ts DESC
        LIMIT 1
        """,
        instrument_token,
      )

    if row is None:
      return False

    tsym = str(row["tsym"])
    m = re.search(r"([CP])(\d+)\s*$", tsym.upper())
    option_side = "CE" if m and m.group(1) == "C" else "PE" if m else "CE"
    qty = int(row["quantity"])
    entry = Decimal(str(row["entry_price"]))
    pos_id = row["id"]
    self._open[pos_id] = OpenPosition(
      position_id=pos_id,
      order_id=row["order_id"],
      candidate_signal_id=row["candidate_signal_id"],
      instrument_token=row["instrument_token"],
      tsym=tsym,
      option_side=option_side,
      quantity=qty,
      entry_price=entry,
      entry_ts=row["entry_ts"],
      premium_deployed=entry * Decimal(qty),
      setup_type=row["setup_type"] or "rehydrated",
      mode=str(row["mode"] or get_execution_mode()),
      mfe=Decimal(str(row["mfe"] or 0)),
      mae=Decimal(str(row["mae"] or 0)),
      signal_snapshot={},
    )
    logger.info("position_loaded_from_db", tsym=tsym, token=instrument_token)
    return True

  async def _lookup_algo_fill_price(
    self,
    tsym: str,
    *,
    trade_book_rows: list[dict] | None = None,
    expected_qty: int | None = None,
  ) -> Decimal | None:
    """Entry fill for adopt/rehydrate — trade-book open lot first, never stale ADOPT/trial."""
    from algoflat.broker.broker_trades import open_lot_from_trade_book

    if trade_book_rows:
      lot = open_lot_from_trade_book(trade_book_rows, tsym)
      if lot is not None:
        price, qty = lot
        if expected_qty is None or qty == expected_qty:
          return price

    from algoflat.db.connection import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
      # Skip synthetic AF-ADOPT rows and exits; require a real broker order id.
      if expected_qty is not None:
        row = await conn.fetchrow(
          """
          SELECT fill_price
          FROM orders
          WHERE tsym = $1
            AND side = 'BUY'
            AND status = 'COMPLETE'
            AND fill_price IS NOT NULL
            AND fill_price > 0
            AND quantity = $2
            AND broker_order_id IS NOT NULL
            AND broker_order_id <> ''
            AND client_order_id NOT LIKE 'AF-ADOPT%'
            AND (ts_created AT TIME ZONE 'Asia/Kolkata')::date
                = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
          ORDER BY ts_filled DESC NULLS LAST, ts_created DESC
          LIMIT 1
          """,
          tsym,
          expected_qty,
        )
      else:
        row = await conn.fetchrow(
          """
          SELECT fill_price
          FROM orders
          WHERE tsym = $1
            AND side = 'BUY'
            AND status = 'COMPLETE'
            AND fill_price IS NOT NULL
            AND fill_price > 0
            AND broker_order_id IS NOT NULL
            AND broker_order_id <> ''
            AND client_order_id NOT LIKE 'AF-ADOPT%'
            AND (ts_created AT TIME ZONE 'Asia/Kolkata')::date
                = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
          ORDER BY ts_filled DESC NULLS LAST, ts_created DESC
          LIMIT 1
          """,
          tsym,
        )
    if row is None or row["fill_price"] is None:
      return None
    return Decimal(str(row["fill_price"]))

  async def _adopt_broker_row(
    self,
    broker_row: dict,
    *,
    trade_book_rows: list[dict] | None = None,
  ) -> bool:
    from algoflat.broker.flattrade import _option_side_from_tsym

    token = str(broker_row.get("instrument_token") or "").strip()
    tsym = str(broker_row.get("tsym") or "").strip()
    if not token or not tsym:
      return False

    qty = int(broker_row.get("quantity") or 0)
    if qty <= 0:
      return False

    entry = await self._lookup_algo_fill_price(
      tsym,
      trade_book_rows=trade_book_rows,
      expected_qty=qty,
    )
    if entry is None or entry <= 0:
      logger.warning(
        "broker_adopt_skipped_no_order_fill",
        tsym=tsym,
        token=token,
      )
      return False

    position_id, order_id = await self._journal.write_adopted_position(
      instrument_token=token,
      tsym=tsym,
      quantity=qty,
      fill_price=entry,
      mode="live",
    )
    self.register_adopted(
      position_id,
      order_id,
      instrument_token=token,
      tsym=tsym,
      option_side=_option_side_from_tsym(tsym),
      quantity=qty,
      fill_price=entry,
      mode="live",
    )
    logger.warning(
      "broker_position_adopted",
      tsym=tsym,
      token=token,
      quantity=qty,
      entry=str(entry),
    )
    return True

  async def reconcile_broker_holdings(
    self,
    broker_rows: list[dict],
    *,
    option_data: Any | None = None,
    trade_book_rows: list[dict] | None = None,
  ) -> dict[str, int]:
    """Keep PositionManager aligned with Flattrade so trails and exits work in live mode."""
    if get_execution_mode() != "live":
      return {"matched": 0, "adopted": 0, "external_closed": 0}

    broker_by_token = {
      str(row.get("instrument_token") or "").strip(): row
      for row in broker_rows
      if str(row.get("instrument_token") or "").strip()
    }

    external_closed = 0
    seen_tokens = set(broker_by_token.keys())
    for pos_id in list(self._open.keys()):
      pos = self._open.get(pos_id)
      if pos is None or pos.mode != "live":
        continue
      token = pos.instrument_token
      if token in broker_by_token:
        self._broker_absent_streak.pop(token, None)
        continue
      streak = self._broker_absent_streak.get(token, 0) + 1
      self._broker_absent_streak[token] = streak
      if streak < 2:
        continue
      exit_price = pos.entry_price
      if option_data is not None:
        state = option_data.get(token)
        if state is not None and state.ltp is not None:
          exit_price = state.ltp
      await self._close_external(pos_id, exit_price, "broker_closed_externally")
      self._broker_absent_streak.pop(token, None)
      external_closed += 1

    for token in seen_tokens:
      if not self.has_open_for_token(token):
        self._broker_absent_streak.pop(token, None)

    adopted = 0
    matched = 0
    for token, broker_row in broker_by_token.items():
      if self.has_open_for_token(token):
        matched += 1
        continue
      if token in self._pending_exit_tokens:
        continue
      if self._in_exit_cooldown(token):
        continue
      if await self._load_open_from_db(token):
        adopted += 1
        continue
      if await self._adopt_broker_row(broker_row, trade_book_rows=trade_book_rows):
        adopted += 1

    if adopted or external_closed:
      logger.info(
        "broker_positions_reconciled",
        matched=matched,
        adopted=adopted,
        external_closed=external_closed,
        pm_count=self.open_count,
        broker_count=len(broker_by_token),
      )
    await self.refresh_entry_prices_from_orders(trade_book_rows=trade_book_rows)
    return {
      "matched": matched,
      "adopted": adopted,
      "external_closed": external_closed,
    }

  async def rehydrate_open_positions(self) -> int:
    """Reload OPEN rows from DB after engine restart so trails keep monitoring."""
    import re

    from algoflat.db.connection import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
      rows = await conn.fetch(
        """
        SELECT
          p.id,
          p.order_id,
          p.instrument_token,
          p.tsym,
          p.side,
          p.quantity,
          COALESCE(o.fill_price, p.entry_price) AS entry_price,
          p.entry_ts,
          p.mfe,
          p.mae,
          p.mode,
          o.candidate_signal_id,
          cs.setup_type
        FROM positions p
        LEFT JOIN orders o ON o.id = p.order_id
        LEFT JOIN candidate_signals cs ON cs.id = o.candidate_signal_id
        WHERE p.status = 'OPEN'
        ORDER BY p.entry_ts ASC
        """
      )

    self._open.clear()
    for row in rows:
      qty = int(row["quantity"])
      entry = Decimal(str(row["entry_price"]))
      tsym = str(row["tsym"])
      m = re.search(r"([CP])(\d+)\s*$", tsym.upper())
      option_side = "CE" if m and m.group(1) == "C" else "PE" if m else "CE"
      prem = entry * Decimal(qty)
      pos_id = row["id"]
      self._open[pos_id] = OpenPosition(
        position_id=pos_id,
        order_id=row["order_id"],
        candidate_signal_id=row["candidate_signal_id"],
        instrument_token=row["instrument_token"],
        tsym=tsym,
        option_side=option_side,
        quantity=qty,
        entry_price=entry,
        entry_ts=row["entry_ts"],
        premium_deployed=prem,
        setup_type=row["setup_type"] or "rehydrated",
        mode=str(row["mode"] or get_execution_mode()),
        mfe=Decimal(str(row["mfe"] or 0)),
        mae=Decimal(str(row["mae"] or 0)),
        persisted_mfe=Decimal(str(row["mfe"] or 0)),
        signal_snapshot={},
      )

    logger.info(
      "positions_rehydrated",
      count=len(self._open),
      tokens=[p.instrument_token for p in self._open.values()],
    )
    return len(self._open)

  def pop_pending_flips(self) -> list[dict]:
    flips = list(self._pending_flips)
    self._pending_flips.clear()
    return flips
