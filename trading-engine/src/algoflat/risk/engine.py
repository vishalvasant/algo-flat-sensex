from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from algoflat.config import AppConfig
from algoflat.db.connection import get_pool
from algoflat.db.paper_account import ensure_paper_account
from algoflat.models.events import CandidateSignal, OptionState
from algoflat.risk.greeks_sizing import greeks_confirmation_multiplier
from algoflat.risk.session_stats import SessionTradeStats, fetch_session_trade_stats
from algoflat.runtime.trading_mode import is_live_execution
from algoflat.notifications.telegram import maybe_send_telegram_alert
from algoflat.symbols_util import (
  capital_for,
  list_underlyings,
  lot_size_for_tsym,
  risk_underlying_key,
  total_account_capital,
  uses_pooled_capital,
)

logger = structlog.get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")


@dataclass
class EntrySizing:
  approved: bool
  quantity: int
  lot_size: int
  entry_ltp: Decimal
  premium_required: Decimal
  rejection_reason: str | None = None
  lots: int = 0
  confidence: int = 0


def _sizing_cfg(risk_cfg: dict) -> dict:
  return risk_cfg.get("confidence_lot_sizing") or {}


def strategy_lot_cap(risk_cfg: dict, setup_type: str | None) -> int | None:
  """Per-setup max lots from risk_config.strategy_lot_caps."""
  if not setup_type:
    return None
  caps = risk_cfg.get("strategy_lot_caps") or {}
  raw = caps.get(setup_type)
  if raw is None:
    return None
  return max(1, int(raw))


def confidence_capital_pct(risk_cfg: dict, confidence: int) -> Decimal:
  """Map confidence → % of available capital to deploy (dynamic mode)."""
  sizing = _sizing_cfg(risk_cfg)
  base = int(sizing.get("base_confidence", 78))
  top = int(sizing.get("max_confidence", 100))
  min_pct = Decimal(str(sizing.get("min_capital_pct", 25)))
  max_pct = Decimal(str(sizing.get("max_capital_pct", 90)))
  conf = max(base, min(int(confidence), top))
  aggressive_from = int(sizing.get("aggressive_deploy_min_confidence", 90))
  if aggressive_from > 0 and conf >= aggressive_from:
    return max_pct
  if top <= base:
    return max_pct
  span = Decimal(conf - base) / Decimal(top - base)
  return min_pct + span * (max_pct - min_pct)


def _tier_lots_for_confidence(risk_cfg: dict, confidence: int) -> int:
  """Legacy fixed tier → lots mapping."""
  default = max(1, int(risk_cfg.get("default_lots", 1)))
  sizing = _sizing_cfg(risk_cfg)
  tiers = list(sizing.get("tiers") or [])
  tiers_sorted = sorted(
    tiers,
    key=lambda t: int(t.get("min_confidence", 0)),
    reverse=True,
  )
  chosen = default
  for tier in tiers_sorted:
    if confidence >= int(tier.get("min_confidence", 0)):
      chosen = max(1, int(tier.get("lots", default)))
      break
  max_lots = int(sizing.get("max_lots", chosen))
  if max_lots > 0:
    return max(1, min(chosen, max_lots))
  return max(1, chosen)


def lots_for_confidence(
  risk_cfg: dict,
  confidence: int,
  *,
  entry_ltp: Decimal | None = None,
  lot_size: int | None = None,
  available: Decimal | None = None,
  deployed: Decimal | None = None,
  equity: Decimal | None = None,
  position_equity_pct: Decimal | None = None,
) -> int:
  """Lots for confidence — dynamic capital % or legacy tiers."""
  sizing = _sizing_cfg(risk_cfg)
  if not sizing.get("enabled", False):
    return max(1, int(risk_cfg.get("default_lots", 1)))

  mode = str(sizing.get("mode", "dynamic")).lower()
  if mode == "dynamic" and entry_ltp is not None and lot_size is not None:
    if available is None or deployed is None or equity is None:
      available = available if available is not None else Decimal("25000")
      equity = equity if equity is not None else available
      deployed = deployed if deployed is not None else Decimal("0")
    lots, _ = fit_lots_to_capital(
      risk_cfg,
      confidence=confidence,
      entry_ltp=entry_ltp,
      lot_size=lot_size,
      available=available,
      deployed=deployed,
      equity=equity,
      position_equity_pct=position_equity_pct,
    )
    return max(0, lots)

  return _tier_lots_for_confidence(risk_cfg, confidence)


def fit_lots_to_capital(
  risk_cfg: dict,
  *,
  confidence: int,
  entry_ltp: Decimal,
  lot_size: int,
  available: Decimal,
  deployed: Decimal,
  equity: Decimal,
  greeks_mult: Decimal = Decimal("1"),
  position_equity_pct: Decimal | None = None,
) -> tuple[int, Decimal]:
  """Size entry: confidence % of available margin × greeks confirmation → max lots."""
  sizing = _sizing_cfg(risk_cfg)
  max_pct = Decimal(str(risk_cfg.get("max_premium_pct_of_available", 65)))
  deploy_cap = equity * Decimal(str(risk_cfg.get("max_deployed_pct_of_equity", 85))) / Decimal("100")
  room = deploy_cap - deployed
  hard_cap = min(available * max_pct / Decimal("100"), room, available)
  if position_equity_pct is not None and position_equity_pct > 0:
    hard_cap = min(hard_cap, equity * position_equity_pct / Decimal("100"))

  def _ok(n: int) -> Decimal | None:
    if n < 1 or lot_size < 1 or entry_ltp <= 0:
      return None
    prem = entry_ltp * lot_size * n
    if prem <= available and prem <= hard_cap:
      return prem
    return None

  cost_per_lot = entry_ltp * lot_size
  if cost_per_lot <= 0:
    return 0, Decimal("0")

  g_mult = max(Decimal("0"), min(Decimal("1"), greeks_mult))

  if sizing.get("enabled", False) and str(sizing.get("mode", "dynamic")).lower() == "dynamic":
    pct = confidence_capital_pct(risk_cfg, confidence)
    # High confidence → up to 90% of available margin, scaled by greeks confirmation.
    budget = min(available * pct / Decimal("100"), hard_cap) * g_mult
    lots = int(budget // cost_per_lot)
    cap = int(sizing.get("max_lots", 0))
    if cap > 0:
      lots = min(lots, cap)
    if lots < 1 and _ok(1) is not None:
      lots = 1
    if lots < 1:
      return 0, Decimal("0")
    prem = _ok(lots)
    if prem is None:
      while lots >= 1:
        prem = _ok(lots)
        if prem is not None:
          break
        lots -= 1
    if lots < 1 or prem is None:
      return 0, Decimal("0")
    return lots, prem

  # Legacy tier mode
  target = _tier_lots_for_confidence(risk_cfg, confidence)
  max_lots = int(sizing.get("max_lots", target)) or target
  lots = target
  while lots >= 1 and _ok(lots) is None:
    lots -= 1
  if lots < 1:
    return 0, Decimal("0")

  scale_min = int(sizing.get("scale_to_max_lots_min_confidence", 92))
  if confidence >= scale_min and max_lots > 0:
    while lots < max_lots and _ok(lots + 1) is not None:
      lots += 1

  prem = _ok(lots)
  assert prem is not None
  return lots, prem


@dataclass
class DailyRiskSnapshot:
  trade_date: date
  starting_capital: Decimal
  available_capital: Decimal
  deployed_capital: Decimal
  realized_pnl: Decimal
  trade_count: int
  consecutive_losses: int
  kill_switch: bool
  entries_blocked: bool
  block_reason: str | None = None
  day_pnl_total: Decimal | None = None

  @property
  def equity(self) -> Decimal:
    # Live broker equity already includes intraday realized P&L.
    if self.day_pnl_total is not None:
      return self.starting_capital
    return self.starting_capital + self.realized_pnl

  @property
  def auto_trade_enabled(self) -> bool:
    return not self.kill_switch and not self.entries_blocked


def risk_cfg_for_sizing(risk_cfg: dict, *, is_expiry_day: bool) -> dict:
  """On expiry, cap confidence-scaled deploy at max_capital_pct (e.g. 50% vs 75%)."""
  if not is_expiry_day:
    return risk_cfg
  expiry_sizing = risk_cfg.get("expiry_day_sizing") or {}
  if not expiry_sizing.get("enabled", False):
    return risk_cfg
  cap_raw = expiry_sizing.get("max_capital_pct")
  if cap_raw is None:
    return risk_cfg
  cap_int = int(cap_raw)
  sizing = dict(risk_cfg.get("confidence_lot_sizing") or {})
  sizing["max_capital_pct"] = cap_int
  merged = dict(risk_cfg)
  merged["confidence_lot_sizing"] = sizing
  merged["max_premium_pct_of_available"] = min(
    int(risk_cfg.get("max_premium_pct_of_available", 75)),
    cap_int,
  )
  merged["max_deployed_pct_of_equity"] = min(
    int(risk_cfg.get("max_deployed_pct_of_equity", 75)),
    cap_int,
  )
  return merged


def overlay_live_limits(snapshot: DailyRiskSnapshot, limits) -> DailyRiskSnapshot:
  """Replace capital fields with broker margin while keeping risk counters."""
  broker_equity = getattr(limits, "equity", None)
  if broker_equity is None:
    broker_equity = limits.cash
  return DailyRiskSnapshot(
    trade_date=snapshot.trade_date,
    starting_capital=broker_equity,
    available_capital=limits.available,
    deployed_capital=limits.margin_used,
    realized_pnl=snapshot.realized_pnl,
    trade_count=snapshot.trade_count,
    consecutive_losses=snapshot.consecutive_losses,
    kill_switch=snapshot.kill_switch,
    entries_blocked=snapshot.entries_blocked,
    block_reason=snapshot.block_reason,
    day_pnl_total=snapshot.day_pnl_total,
  )


def apply_session_stats(
  snapshot: DailyRiskSnapshot,
  stats: SessionTradeStats,
) -> DailyRiskSnapshot:
  """Replace trade counters with mode-specific closed-trade aggregates."""
  return DailyRiskSnapshot(
    trade_date=snapshot.trade_date,
    starting_capital=snapshot.starting_capital,
    available_capital=snapshot.available_capital,
    deployed_capital=snapshot.deployed_capital,
    realized_pnl=stats.realized_pnl,
    trade_count=stats.trade_count,
    consecutive_losses=stats.consecutive_losses,
    kill_switch=snapshot.kill_switch,
    entries_blocked=snapshot.entries_blocked,
    block_reason=snapshot.block_reason,
    day_pnl_total=snapshot.day_pnl_total,
  )


class RiskEngine:
  def __init__(self, config: AppConfig) -> None:
    self._config = config
    self._risk = config.risk

  def _today_ist(self) -> date:
    return datetime.now(IST).date()

  def _max_daily_loss_limit(self, starting_capital: Decimal | None = None) -> Decimal:
    """Daily loss cap — identical for paper and live (0 = disabled).

    Prefer ``max_daily_loss_pct_of_starting`` × that day's starting capital
    (e.g. 30 → 30% of open equity). Falls back to fixed ``max_daily_loss`` INR
    when pct is unset/0 or starting capital is missing.
    """
    pct_raw = self._risk.get("max_daily_loss_pct_of_starting")
    if pct_raw is not None and starting_capital is not None and starting_capital > 0:
      pct = Decimal(str(pct_raw))
      if pct > 0:
        return (starting_capital * pct / Decimal("100")).quantize(Decimal("0.01"))
    return Decimal(str(self._risk.get("max_daily_loss", 0)))

  async def ensure_daily_state(self, underlying: str | None = None) -> DailyRiskSnapshot:
    ul = risk_underlying_key(self._config, underlying)
    capital = (
      total_account_capital(self._config)
      if uses_pooled_capital(self._config)
      else capital_for(self._config, ul)
    )
    await ensure_paper_account(capital, underlying=ul)
    today = self._today_ist()
    pool = get_pool()
    async with pool.acquire() as conn:
      row = await conn.fetchrow(
        """
        SELECT trade_date, starting_capital, available_capital, deployed_capital,
               realized_pnl, trade_count, consecutive_losses, kill_switch,
               entries_blocked, block_reason
        FROM daily_risk_state WHERE trade_date = $1 AND underlying = $2
        """,
        today,
        ul,
      )
      # Drop a persisted brake once its config is off, so a restart unblocks entries.
      consec_off = int(self._risk.get("max_consecutive_losses", 0)) <= 0 or not bool(
        self._risk.get("block_entries_on_consecutive_losses", True)
      )
      stale_consec = (
        row is not None
        and bool(row["entries_blocked"])
        and row["block_reason"] == "max_consecutive_losses"
        and consec_off
      )
      start_cap = (
        Decimal(str(row["starting_capital"] or 0)) if row is not None else Decimal("0")
      )
      stale_daily = (
        row is not None
        and bool(row["entries_blocked"])
        and row["block_reason"] == "max_daily_loss"
        and self._max_daily_loss_limit(start_cap) <= 0
      )
      if stale_consec or stale_daily:
        await conn.execute(
          """
          UPDATE daily_risk_state SET
            entries_blocked = FALSE,
            block_reason = NULL,
            updated_at = now()
          WHERE trade_date = $1 AND underlying = $2
          """,
          today,
          ul,
        )
        row = await conn.fetchrow(
          """
          SELECT trade_date, starting_capital, available_capital, deployed_capital,
                 realized_pnl, trade_count, consecutive_losses, kill_switch,
                 entries_blocked, block_reason
          FROM daily_risk_state WHERE trade_date = $1 AND underlying = $2
          """,
          today,
          ul,
        )
    assert row is not None
    return DailyRiskSnapshot(
      trade_date=row["trade_date"],
      starting_capital=Decimal(str(row["starting_capital"] or capital)),
      available_capital=Decimal(str(row["available_capital"] or capital)),
      deployed_capital=Decimal(str(row["deployed_capital"] or 0)),
      realized_pnl=Decimal(str(row["realized_pnl"] or 0)),
      trade_count=int(row["trade_count"] or 0),
      consecutive_losses=int(row["consecutive_losses"] or 0),
      kill_switch=bool(row["kill_switch"]),
      entries_blocked=bool(row["entries_blocked"]),
      block_reason=row["block_reason"],
    )

  async def snapshot_for_execution(
    self,
    *,
    live_broker: Any | None = None,
    underlying: str | None = None,
  ) -> DailyRiskSnapshot:
    """Risk counters from DB; live capital + broker day P&L from Flattrade."""
    snap = await self.ensure_daily_state(underlying=underlying)
    if is_live_execution():
      algo_stats = await fetch_session_trade_stats("live")
      snap = apply_session_stats(snap, algo_stats)
      if live_broker is not None:
        limits, broker_day, _ = await live_broker.fetch_broker_snapshot()
        snap = DailyRiskSnapshot(
          trade_date=snap.trade_date,
          starting_capital=snap.starting_capital,
          available_capital=snap.available_capital,
          deployed_capital=snap.deployed_capital,
          realized_pnl=broker_day.realized_pnl,
          trade_count=snap.trade_count,
          consecutive_losses=snap.consecutive_losses,
          kill_switch=snap.kill_switch,
          entries_blocked=snap.entries_blocked,
          block_reason=snap.block_reason,
          day_pnl_total=broker_day.total_pnl,
        )
        snap = overlay_live_limits(snap, limits)
    return snap

  async def sync_live_account_from_broker(
    self,
    live_broker: Any,
    *,
    broker_snapshot: tuple[Any, Any, list[dict[str, Any]]] | None = None,
  ) -> DailyRiskSnapshot | None:
    """Refresh available/deployed capital from Flattrade while keeping algo counters."""
    if not is_live_execution() or live_broker is None:
      return None
    ul = risk_underlying_key(self._config)
    snap = await self.ensure_daily_state(underlying=ul)
    algo_stats = await fetch_session_trade_stats("live")
    snap = apply_session_stats(snap, algo_stats)
    if broker_snapshot is not None:
      limits, broker_day, _ = broker_snapshot
    else:
      limits, broker_day, _ = await live_broker.fetch_broker_snapshot()
    snap = DailyRiskSnapshot(
      trade_date=snap.trade_date,
      starting_capital=snap.starting_capital,
      available_capital=snap.available_capital,
      deployed_capital=snap.deployed_capital,
      realized_pnl=broker_day.realized_pnl,
      trade_count=snap.trade_count,
      consecutive_losses=snap.consecutive_losses,
      kill_switch=snap.kill_switch,
      entries_blocked=snap.entries_blocked,
      block_reason=snap.block_reason,
      day_pnl_total=broker_day.total_pnl,
    )
    snap = overlay_live_limits(snap, limits)
    pool = get_pool()
    async with pool.acquire() as conn:
      await conn.execute(
        """
        UPDATE daily_risk_state SET
          available_capital = $1,
          deployed_capital = $2,
          realized_pnl = $3,
          updated_at = now()
        WHERE trade_date = $4 AND underlying = $5
        """,
        snap.available_capital,
        snap.deployed_capital,
        snap.realized_pnl,
        snap.trade_date,
        ul,
      )
    return snap

  async def set_auto_trade(self, enabled: bool) -> DailyRiskSnapshot:
    """Enable/disable new entries without kill-switch flatten."""
    today = self._today_ist()
    pool = get_pool()
    await self.ensure_daily_state()
    async with pool.acquire() as conn:
      if uses_pooled_capital(self._config):
        underlyings = [risk_underlying_key(self._config)]
      else:
        underlyings = [
          str(u.get("symbol", "")).upper()
          for u in list_underlyings(self._config)
          if u.get("symbol")
        ] or [self._config.symbols.get("underlying", "NIFTY").upper()]
      for ul in underlyings:
        row = await conn.fetchrow(
          "SELECT kill_switch FROM daily_risk_state WHERE trade_date = $1 AND underlying = $2",
          today,
          ul,
        )
        kill = bool(row["kill_switch"]) if row else False
        if enabled:
          if kill:
            await conn.execute(
              """
              UPDATE daily_risk_state SET
                block_reason = COALESCE(block_reason, 'kill_switch'),
                updated_at = now()
              WHERE trade_date = $1 AND underlying = $2
              """,
              today,
              ul,
            )
          else:
            await conn.execute(
              """
              UPDATE daily_risk_state SET
                entries_blocked = FALSE,
                block_reason = NULL,
                updated_at = now()
              WHERE trade_date = $1 AND underlying = $2
              """,
              today,
              ul,
            )
        else:
          await conn.execute(
            """
            UPDATE daily_risk_state SET
              entries_blocked = TRUE,
              block_reason = 'auto_trade_off',
              updated_at = now()
            WHERE trade_date = $1 AND underlying = $2
            """,
            today,
            ul,
          )
      if enabled:
        await conn.execute(
          """
          INSERT INTO notifications (type, severity, title, message)
          VALUES ('system', 'info', 'Auto trading ON',
                  'Engine may place entries when setups fire')
          """
        )
        await maybe_send_telegram_alert(
          type_="system",
          severity="info",
          title="Auto trading ON",
          message="Engine may place entries when setups fire",
        )
      else:
        await conn.execute(
          """
          INSERT INTO notifications (type, severity, title, message)
          VALUES ('system', 'warning', 'Auto trading OFF',
                  'New entries paused — scans and decision logs continue')
          """
        )
        await maybe_send_telegram_alert(
          type_="system",
          severity="warning",
          title="Auto trading OFF",
          message="New entries paused — scans and decision logs continue",
        )
    return await self.ensure_daily_state()


  async def size_entry(
    self,
    signal: CandidateSignal,
    option: OptionState,
    snapshot: DailyRiskSnapshot,
    *,
    open_position_count: int = 0,
    is_expiry_day: bool = False,
  ) -> EntrySizing:
    raw_lot = signal.scanner_metadata.get("lot_size")
    meta_lot = int(raw_lot) if raw_lot is not None else None
    lot_size = lot_size_for_tsym(signal.tsym, metadata_lot_size=meta_lot)
    confidence = int(
      signal.confidence
      if signal.confidence is not None
      else signal.scanner_metadata.get("confidence", 0)
    )
    entry_ltp = option.ltp or Decimal("0")

    if entry_ltp <= 0:
      return EntrySizing(
        False, 0, lot_size, entry_ltp, Decimal("0"), "invalid_ltp",
        lots=0, confidence=confidence,
      )

    max_daily_loss = self._max_daily_loss_limit(snapshot.starting_capital)
    day_pnl = (
      snapshot.day_pnl_total
      if snapshot.day_pnl_total is not None
      else snapshot.realized_pnl
    )
    if max_daily_loss > 0 and day_pnl <= -max_daily_loss:
      return EntrySizing(
        False, 0, lot_size, entry_ltp, Decimal("0"), "max_daily_loss",
        lots=0, confidence=confidence,
      )

    max_trades = int(self._risk.get("max_trades_per_day", 0))
    if max_trades > 0 and snapshot.trade_count >= max_trades:
      return EntrySizing(
        False, 0, lot_size, entry_ltp, Decimal("0"), "max_trades_per_day",
        lots=0, confidence=confidence,
      )

    max_concurrent = int(self._risk.get("max_concurrent_positions_per_index", 0))
    if max_concurrent > 0 and open_position_count >= max_concurrent:
      return EntrySizing(
        False, 0, lot_size, entry_ltp, Decimal("0"), "max_concurrent_positions",
        lots=0, confidence=confidence,
      )

    max_consec = int(self._risk.get("max_consecutive_losses", 0))
    if max_consec > 0 and snapshot.consecutive_losses >= max_consec:
      return EntrySizing(
        False, 0, lot_size, entry_ltp, Decimal("0"), "max_consecutive_losses",
        lots=0, confidence=confidence,
      )

    if snapshot.kill_switch or snapshot.entries_blocked:
      return EntrySizing(
        False, 0, lot_size, entry_ltp, Decimal("0"), "entries_blocked",
        lots=0, confidence=confidence,
      )

    risk_cfg = risk_cfg_for_sizing(self._risk, is_expiry_day=is_expiry_day)

    max_premium_pct = Decimal(str(risk_cfg.get("max_premium_pct_of_available", 65)))
    max_for_trade = snapshot.available_capital * max_premium_pct / Decimal("100")
    max_deployed_pct = Decimal(str(risk_cfg.get("max_deployed_pct_of_equity", 85)))
    deploy_cap = snapshot.equity * max_deployed_pct / Decimal("100")
    deploy_room = deploy_cap - snapshot.deployed_capital

    greeks_mult = greeks_confirmation_multiplier(signal, option, self._risk)
    position_cap = None
    if signal.setup_type == "option_lift":
      raw_cap = (self._config.strategy.get("option_lift") or {}).get(
        "max_equity_pct_per_position"
      )
      if raw_cap:
        position_cap = Decimal(str(raw_cap))

    lots, premium = fit_lots_to_capital(
      risk_cfg,
      confidence=confidence,
      entry_ltp=entry_ltp,
      lot_size=lot_size,
      available=snapshot.available_capital,
      deployed=snapshot.deployed_capital,
      equity=snapshot.equity,
      greeks_mult=greeks_mult,
      position_equity_pct=position_cap,
    )

    target_lots = lots_for_confidence(
      risk_cfg,
      confidence,
      entry_ltp=entry_ltp,
      lot_size=lot_size,
      available=snapshot.available_capital,
      deployed=snapshot.deployed_capital,
      equity=snapshot.equity,
      position_equity_pct=position_cap,
    )
    if lots < 1:
      quantity = lot_size * max(target_lots, 1)
      premium_req = entry_ltp * quantity
      reason = "insufficient_capital"
      position_room = (
        snapshot.equity * position_cap / Decimal("100")
        if position_cap is not None and position_cap > 0
        else None
      )
      if premium_req > max_for_trade:
        reason = "premium_pct_exceeded"
      elif premium_req > deploy_room:
        reason = "deployed_cap_exceeded"
      elif position_room is not None and premium_req > position_room:
        reason = "position_cap_exceeded"
      return EntrySizing(
        False, quantity, lot_size, entry_ltp, premium_req, reason,
        lots=target_lots, confidence=confidence,
      )

    quantity = lot_size * lots
    logger.info(
      "entry_sized",
      confidence=confidence,
      capital_pct=str(confidence_capital_pct(risk_cfg, confidence)),
      greeks_mult=str(greeks_mult),
      target_lots=target_lots,
      lots=lots,
      quantity=quantity,
      premium=str(premium),
      deploy_room=str(deploy_room),
      setup_type=signal.setup_type,
      available=str(snapshot.available_capital),
    )
    return EntrySizing(
      True, quantity, lot_size, entry_ltp, premium, None,
      lots=lots, confidence=confidence,
    )

  async def reserve_capital(self, premium: Decimal, underlying: str | None = None) -> None:
    if is_live_execution():
      return
    today = self._today_ist()
    ul = risk_underlying_key(self._config, underlying)
    pool = get_pool()
    async with pool.acquire() as conn:
      await conn.execute(
        """
        UPDATE daily_risk_state SET
            deployed_capital = deployed_capital + $3,
            available_capital = available_capital - $3,
            updated_at = now()
        WHERE trade_date = $1 AND underlying = $2
        """,
        today,
        ul,
        premium,
      )

  async def release_capital(self, premium: Decimal, pnl: Decimal, underlying: str | None = None) -> None:
    """Return premium to cash and compound trade P&L into deployable capital."""
    today = self._today_ist()
    ul = risk_underlying_key(self._config, underlying)
    pool = get_pool()
    async with pool.acquire() as conn:
      if is_live_execution():
        await conn.execute(
          """
          UPDATE daily_risk_state SET
              realized_pnl = realized_pnl + $3,
              trade_count = trade_count + 1,
              consecutive_losses = CASE WHEN $3 < 0 THEN consecutive_losses + 1 ELSE 0 END,
              updated_at = now()
          WHERE trade_date = $1 AND underlying = $2
          """,
          today,
          ul,
          pnl,
        )
        row = await conn.fetchrow(
          """
          SELECT starting_capital, realized_pnl, consecutive_losses, entries_blocked
          FROM daily_risk_state WHERE trade_date = $1 AND underlying = $2
          """,
          today,
          ul,
        )
        if row:
          await self._maybe_block_entries(conn, today, ul, row)
        return
      await conn.execute(
        """
        UPDATE daily_risk_state SET
            deployed_capital = GREATEST(deployed_capital - $3, 0),
            available_capital = available_capital + $3 + $4,
            realized_pnl = realized_pnl + $4,
            trade_count = trade_count + 1,
            consecutive_losses = CASE WHEN $4 < 0 THEN consecutive_losses + 1 ELSE 0 END,
            updated_at = now()
        WHERE trade_date = $1 AND underlying = $2
        """,
        today,
        ul,
        premium,
        pnl,
      )
      row = await conn.fetchrow(
        """
        SELECT starting_capital, realized_pnl, consecutive_losses, entries_blocked
        FROM daily_risk_state WHERE trade_date = $1 AND underlying = $2
        """,
        today,
        ul,
      )
      if row:
        await self._maybe_block_entries(conn, today, ul, row)
        logger.info(
          "capital_compounded",
          underlying=ul,
          pnl=str(pnl),
          realized_pnl=str(row["realized_pnl"]),
          consecutive_losses=int(row["consecutive_losses"] or 0),
        )

  async def _maybe_block_entries(self, conn, today: date, ul: str, row) -> None:
    """Persist entries_blocked when risk brakes trip (spec §14)."""
    if bool(row.get("entries_blocked")):
      return
    max_consec = int(self._risk.get("max_consecutive_losses", 0))
    start_cap = Decimal(str(row.get("starting_capital") or 0))
    max_daily = self._max_daily_loss_limit(start_cap)
    block_on_consec = bool(self._risk.get("block_entries_on_consecutive_losses", True))
    # Same config gate for paper and live (paper source of truth; 0 = off).
    block_on_daily = bool(self._risk.get("block_entries_on_daily_loss", True))
    consec = int(row["consecutive_losses"] or 0)
    realized = Decimal(str(row["realized_pnl"] or 0))

    block_reason: str | None = None
    if block_on_consec and max_consec > 0 and consec >= max_consec:
      block_reason = "max_consecutive_losses"
    elif block_on_daily and max_daily > 0 and realized <= -max_daily:
      block_reason = "max_daily_loss"

    if block_reason:
      await conn.execute(
        """
        UPDATE daily_risk_state SET
          entries_blocked = TRUE,
          block_reason = $3,
          updated_at = now()
        WHERE trade_date = $1 AND underlying = $2
        """,
        today,
        ul,
        block_reason,
      )
      logger.warning(
        "entries_blocked_by_risk_brake",
        underlying=ul,
        reason=block_reason,
        consecutive_losses=consec,
        realized_pnl=str(realized),
        starting_capital=str(start_cap),
        max_daily_loss=str(max_daily),
      )

  def is_force_exit_time(self) -> bool:
    force = self._risk.get("force_exit_time", "15:20")
    now_ist = datetime.now(IST).time()
    return now_ist >= time.fromisoformat(force)
