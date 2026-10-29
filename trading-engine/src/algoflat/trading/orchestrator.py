from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from pathlib import Path

from algoflat.config import AppConfig
from algoflat.contract_selector.expiry import parse_expiry_tag
from algoflat.strategy.expiry_distance import days_to_expiry
from algoflat.contract_selector.resolve import resolve_side_contract
from algoflat.contract_selector.selector import ContractSelector, ContractUniverse
from algoflat.contract_selector.strike_picker import (
  atm_band_instruments,
  candidate_instruments,
  effective_quote_band_steps,
)
from algoflat.execution.engine import ExecutionEngine
from algoflat.features.chain_intel import build_chain_snapshot
from algoflat.features.engine import FeatureEngine
from algoflat.journal.analytics import StrategyLearner
from algoflat.journal.writer import JournalWriter
from algoflat.market_data.engine import MarketDataEngine
from algoflat.market_intelligence.candidate_logger import build_candidate_record, log_candidate
from algoflat.market_intelligence.context import MarketIntelligenceContext
from algoflat.market_intelligence.engine import MarketIntelligenceEngine
from algoflat.market_intelligence.ml_service import MLTradeQualityService
from algoflat.models.events import (
  Bias,
  CandidateSignal,
  CandleInterval,
  OptionState,
  QuoteUpdate,
  StrategyDecision,
  SystemEvent,
)
from algoflat.trading.option_lift import SETUP_TYPE as OPTION_LIFT
from algoflat.trading.option_lift import (
  apply_opposite_trade_today,
  find_option_lifts,
  split_confirmed_lifts,
)
from algoflat.option_data.greeks import compute_greeks
from algoflat.option_data.layer import OptionDataLayer
from algoflat.position.manager import PositionManager
from algoflat.quality.gate import QualityGate
from algoflat.regime.classifier import RegimeClassifier
from algoflat.risk.engine import RiskEngine
from algoflat.runtime.trading_mode import get_execution_mode
from algoflat.broker.routed import RoutedBrokerAdapter
from algoflat.scanner.library import build_strategy_scanners
from algoflat.strategy.router import StrategyRouter
from algoflat.trading.decision_log_policy import DecisionLogPolicy
from algoflat.trading.entry_confirmation import (
  EntryConfirmationTracker,
  in_open_gap_entry_window,
)
from algoflat.validator.engine import RuleValidator
from algoflat.validator.time_of_day_cautious import (
  time_of_day_cautious_rejection_reasons,
  tod_dead_zone_rejection_reasons,
)
from algoflat.validator.premium_behaviour import PremiumBehaviourTracker
from algoflat.symbols_util import index_spot_context, list_underlyings, price_context, uses_dual_index_spot

logger = structlog.get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")

_LIFT_BATCH_STOP = {
  "deployed_cap_exceeded",
  "max_daily_loss",
  "max_consecutive_losses",
  "max_trades_per_day",
  "kill_switch",
  "entries_blocked",
  "cooldown_active",
}
# One strike can be too big for its own sleeve. The next strike may still fit.
_LIFT_SIZE_SKIP = {
  "insufficient_capital",
  "premium_pct_exceeded",
  "position_cap_exceeded",
}


def _lift_block_head(reason: str | None) -> str:
  if not reason:
    return ""
  return reason.split(",")[0].strip()


def _lift_batch_should_stop(reason: str | None) -> bool:
  """Stop later lifts when a book-level risk rule already rejected one."""
  head = _lift_block_head(reason)
  return head in _LIFT_BATCH_STOP or head.startswith("cooldown")


def _lift_size_skip(reason: str | None) -> bool:
  """This contract does not fit. A cheaper strike in the same batch still can."""
  return _lift_block_head(reason) in _LIFT_SIZE_SKIP


class TradingOrchestrator:
  def __init__(
    self,
    config: AppConfig,
    broker,
    journal: JournalWriter,
    market_data: MarketDataEngine,
    option_data: OptionDataLayer,
  ) -> None:
    self._config = config
    self._broker = broker
    self._journal = journal
    self._market_data = market_data
    self._option_data = option_data
    self._universe: ContractUniverse | None = None
    self._prior_oi: dict[str, int] = {}

    self.features = FeatureEngine(config, market_data)
    self.regime = RegimeClassifier(config)
    self.quality = QualityGate(config)
    root = Path(__file__).resolve().parents[4]
    self.learner = StrategyLearner.from_config(
      root / "reports" / "strategy_learner.json", config.strategy
    )
    self.quality.set_learner(self.learner)
    scanners = build_strategy_scanners(config)
    # Keep attribute for debug endpoints that still expect .scanner
    self.scanner = next((s for s in scanners if s.name == "vwap_reclaim"), scanners[0])
    self.router = StrategyRouter(config, scanners, self.quality)
    self.validator = RuleValidator(config)
    self.risk = RiskEngine(config)
    self.execution = ExecutionEngine(config, broker, journal, self.risk)
    self.positions = PositionManager(
      config, broker, journal, self.risk, market_data
    )
    self.positions.set_trade_close_hook(self._on_trade_closed)
    self._contract_selector = ContractSelector(config, broker)

    self._scan_lock = asyncio.Lock()
    self._entering_tokens: set[str] = set()
    self._last_scan_at: datetime | None = None
    self._last_scan_completed_at: datetime | None = None
    self._scan_interval_sec = int(config.runtime.get("scan_interval_seconds", 10))
    self._entry_scan_on_quote = bool(config.runtime.get("entry_scan_on_quote", False))
    self._log_every = bool(config.strategy.get("router", {}).get("log_every_decision", True))
    self._decision_log_policy = DecisionLogPolicy.from_router_config(
      config.strategy.get("router") or {}
    )
    self._entry_confirm = EntryConfirmationTracker.from_config(
      config.strategy.get("entry_confirmation") or {}
    )
    self._option_lift_sent: set[str] = set()
    self._option_lift_streaks: dict[str, int] = {}
    pb_cfg = config.validator.get("premium_responsive") or {}
    self._premium_tracker = PremiumBehaviourTracker(
      max_samples=int(pb_cfg.get("max_samples", 12))
    )
    self._session_date: date | None = None
    self._running = False
    self._underlyings = [
      str(u.get("symbol", "")).upper()
      for u in list_underlyings(config)
      if u.get("symbol")
    ] or [str(config.symbols.get("underlying", "NIFTY")).upper()]
    self._scan_underlying_idx = 0
    self._switch_underlying: Any | None = None
    self._sector_refresh_cb: Any | None = None
    self._scan_count = 0
    self._index_ltps: dict[str, Decimal] = {}
    self._index_opens: dict[str, Decimal] = {}
    self._mi_engine = MarketIntelligenceEngine(config)
    self._ml_service = MLTradeQualityService(config.ml)
    self._last_mi_snapshot: dict[str, Any] = {}
    self._last_ml_prediction: dict[str, Any] = {}
    self._last_entry_block: str | None = None
    self._last_entry_confirm: str | None = None
    self._mi_cfg = config.market_intelligence if hasattr(config, "market_intelligence") else {}

  def update_index_quote(
    self,
    symbol: str,
    ltp: Decimal,
    *,
    session_open: Decimal | None = None,
  ) -> None:
    ul = symbol.upper()
    self._index_ltps[ul] = ltp
    if session_open is not None:
      self._index_opens[ul] = session_open

  def set_index_market_snapshot(
    self,
    ltps: dict[str, Decimal | float],
    opens: dict[str, Decimal | float] | None = None,
  ) -> None:
    for sym, px in ltps.items():
      self._index_ltps[str(sym).upper()] = Decimal(str(px))
    if opens:
      for sym, px in opens.items():
        self._index_opens[str(sym).upper()] = Decimal(str(px))

  @property
  def last_mi_snapshot(self) -> dict[str, Any]:
    return self._last_mi_snapshot

  @property
  def last_ml_prediction(self) -> dict[str, Any]:
    return self._last_ml_prediction

  def set_underlying_switcher(self, callback) -> None:
    self._switch_underlying = callback

  @property
  def last_entry_block(self) -> str | None:
    return self._last_entry_block

  @property
  def last_entry_confirm(self) -> str | None:
    return self._last_entry_confirm

  def set_sector_refresher(self, callback) -> None:
    self._sector_refresh_cb = callback

  def _active_underlying(self) -> str:
    return str(self._config.symbols.get("underlying", "NIFTY")).upper()

  def _open_count_for(self, underlying: str) -> int:
    ul = underlying.upper()
    return sum(1 for p in self.positions.open_positions if p.tsym.upper().startswith(ul))

  def _open_lifts_are_green(self, open_now: list) -> bool:
    """True when every open position clears the configured profit gate on LTP."""
    lift_cfg = self._config.strategy.get("option_lift") or {}
    min_pct = Decimal(str(lift_cfg.get("second_position_min_profit_pct", 0) or 0))
    need = Decimal("1") + (min_pct / Decimal("100"))
    for pos in open_now:
      state = self._option_data.get(pos.instrument_token)
      mark = None
      if state is not None and state.ltp is not None:
        mark = state.ltp
      elif pos.last_good_ltp is not None:
        mark = pos.last_good_ltp
      if mark is None or pos.entry_price <= 0:
        return False
      if min_pct > 0:
        if mark < pos.entry_price * need:
          return False
      elif mark <= pos.entry_price:
        return False
    return True

  def _open_book_entry_block(self, signal_side: str) -> str | None:
    """Block new entries that fight or stack on an already-open book."""
    lift_cfg = self._config.strategy.get("option_lift") or {}
    open_now = self.positions.open_positions
    if not open_now:
      return None
    # Hard rule: only one open contract — no pyramid and no opposite hedge.
    if bool(lift_cfg.get("one_position_at_a_time", False)):
      return "position_already_open"
    open_sides = {p.option_side for p in open_now}
    side = (signal_side or "").upper()
    if bool(lift_cfg.get("one_side_at_a_time", False)) and open_sides and side not in open_sides:
      return "opposite_side_open"
    # Even if same-side pyramid is allowed, never open the other side while green.
    if (
      bool(lift_cfg.get("block_opposite_when_green", True))
      and open_sides
      and side not in open_sides
      and self._open_lifts_are_green(open_now)
    ):
      return "opposite_while_green"
    if bool(lift_cfg.get("second_position_only_if_green", False)) and not self._open_lifts_are_green(
      open_now
    ):
      return "wait_for_open_trade_green"
    return None

  async def _fresh_option_ltp_state(
    self, token: str, *, exchange: str, tsym: str
  ) -> OptionState | None:
    """Latest raw LTP from the WebSocket option cache — never REST / mid / EMA."""
    del exchange  # entry path is WS-only; exchange kept for call-site compatibility
    state = self._option_data.get(token)
    if state is None or state.ltp is None or state.ltp <= 0:
      logger.info("entry_ws_ltp_missing", token=token, tsym=tsym)
      return None
    source = (state.quote_source or "").lower()
    if source and "websocket" not in source and source != "ws":
      logger.info(
        "entry_ws_ltp_not_websocket",
        token=token,
        tsym=tsym,
        quote_source=state.quote_source,
        ltp=str(state.ltp),
      )
      return None
    return state

  def _on_trade_closed(
    self, setup_type: str, pnl: Decimal, exit_reason: str, *, is_expiry_day: bool = False
  ) -> None:
    self.learner.record_trade(setup_type, pnl, exit_reason=exit_reason)
    if pnl < 0:
      self.validator.record_session_loss(
        setup_type,
        is_expiry_day=is_expiry_day,
        exit_reason=exit_reason,
      )

  def set_universe(self, universe: ContractUniverse) -> None:
    self._universe = universe

  async def initialize(self) -> None:
    await self.risk.ensure_daily_state()
    snap = await self.risk.ensure_daily_state()
    await self._load_prior_day_levels()
    logger.info(
      "paper_account_initialized",
      capital=str(snap.available_capital),
      starting=str(snap.starting_capital),
      strategies=len(self.router._scanners),
    )

  async def _load_prior_day_levels(self) -> None:
    """PDH/PDL/CPR inputs from previous session."""
    try:
      from datetime import date, time, timedelta

      today = datetime.now(IST).date()
      # Walk back up to 5 calendar days for prior session
      for back in range(1, 6):
        d = today - timedelta(days=back)
        if d.weekday() >= 5:
          continue
        start = datetime.combine(d, time(9, 15), tzinfo=IST).astimezone(timezone.utc)
        end = datetime.combine(d, time(15, 30), tzinfo=IST).astimezone(timezone.utc)
        exchange, token = price_context(self._config.symbols)
        if uses_dual_index_spot(self._config.symbols):
          exchange, token = index_spot_context(self._config.symbols)
        rows = await self._broker.get_candles(
          exchange,
          token,
          CandleInterval.M5,
          start,
          end,
        )
        if rows:
          hi = max(c.high for c in rows)
          lo = min(c.low for c in rows)
          cl = rows[-1].close
          self.features.set_prior_day(hi, lo, cl)
          logger.info("prior_day_levels", date=str(d), pdh=str(hi), pdl=str(lo), close=str(cl))
          return
    except Exception:
      logger.exception("prior_day_levels_failed")

  async def run_periodic_scan(self) -> None:
    self._running = True
    while self._running:
      try:
        if self._market_open():
          # Must share _scan_lock with on_quote — otherwise two concurrent
          # scans can both pass has_open_for_token and double-enter the same
          # contract (seen live: identical entry+exit timestamps).
          async with self._scan_lock:
            await self._process_pending_flips()
            self._scan_count += 1
            if self._sector_refresh_cb is not None and self._scan_count % 6 == 0:
              try:
                await self._sector_refresh_cb()
              except Exception:
                logger.exception("sector_refresh_failed")
            self._last_scan_at = datetime.now(tz=timezone.utc)
            await self._scan_for_entry()
      except asyncio.CancelledError:
        raise
      except Exception:
        logger.exception("periodic_scan_failed")
      await asyncio.sleep(self._scan_interval_sec)

  async def _process_pending_flips(self) -> None:
    if not self.positions._pending_flips:
      return
    flips = self.positions.pop_pending_flips()
    for flip in flips:
      try:
        await self._try_reversal_flip(flip)
      except Exception:
        logger.exception("reversal_flip_failed", flip=flip)

  def stop(self) -> None:
    self._running = False

  async def on_quote(self, quote: QuoteUpdate) -> None:
    await self.positions.on_quote(quote)

    if self._universe is None or not self._market_open():
      return

    if not self._entry_scan_on_quote:
      if self.positions._pending_flips:
        async with self._scan_lock:
          await self._process_pending_flips()
      # §26A: fast premium confirmation on quote ticks — not a full strategy scan.
      entry_confirm = getattr(self, "_entry_confirm", None)
      if (
        entry_confirm is not None
        and entry_confirm.has_pending()
        and quote.instrument_token
      ):
        fast_sig, fast_reason = entry_confirm.try_fast_confirm(
          quote.instrument_token,
          premium_tracker=self._premium_tracker,
        )
        if fast_sig is not None:
          async with self._scan_lock:
            await self._journal.write_system_event(
              SystemEvent(
                event_type="entry_confirm_fast",
                ts=datetime.now(tz=timezone.utc),
                severity="info",
                message=fast_reason or "fast_confirm",
                metadata={
                  "setup": fast_sig.setup_type,
                  "tsym": fast_sig.tsym,
                  "timing": (fast_sig.scanner_metadata or {}).get("confirmation_timing"),
                },
              )
            )
            await self._execute_signal(fast_sig, is_expiry=self._is_expiry_day())
      return

    # Flips + entry scans share one lock so they cannot race a periodic scan
    # into a duplicate same-token fill.
    async with self._scan_lock:
      await self._process_pending_flips()

      now = datetime.now(tz=timezone.utc)
      if (
        self._last_scan_at
        and (now - self._last_scan_at).total_seconds() < self._scan_interval_sec
      ):
        return
      self._last_scan_at = now
      await self._scan_for_entry()

  def _mark_option_lift_sent(self, signal: CandidateSignal) -> None:
    if signal.setup_type != OPTION_LIFT:
      return
    bar_ts = (signal.scanner_metadata or {}).get("bar_ts")
    self._option_lift_sent.add(f"{signal.instrument_token}:{bar_ts}")

  def _maybe_reset_session(self) -> None:
    today = datetime.now(IST).date()
    if self._session_date != today:
      self._session_date = today
      self.learner.begin_session(today)
      self.validator.reset_session()
      self._entry_confirm.clear()
      self._option_lift_sent.clear()
      self._option_lift_streaks.clear()
      self._premium_tracker = PremiumBehaviourTracker(
        max_samples=int(
          (self._config.validator.get("premium_responsive") or {}).get("max_samples", 12)
        )
      )
      self._decision_log_policy.reset_session()
      self._last_entry_block = None
      self._last_entry_confirm = None

  def _record_premium_samples(
    self,
    band_states: dict[str, OptionState | None],
    spot: Decimal | None,
  ) -> None:
    if spot is None:
      return
    now = datetime.now(tz=timezone.utc)
    spot_f = float(spot)
    for st in band_states.values():
      if st is not None and st.ltp is not None:
        self._premium_tracker.record(st.instrument_token, spot_f, float(st.ltp), now)

  def _band_option_states(self, universe: ContractUniverse) -> dict[str, OptionState | None]:
    """LTP map for ATM±N and OTM CE/PE — used by Greek strike picker."""
    step = Decimal(str(self._config.symbols.get("strike_step", 50)))
    strike_cfg = self._config.strategy.get("strike_selection") or {}
    band = effective_quote_band_steps(strike_cfg)
    modes = list(strike_cfg.get("modes") or ["atm"])
    otm_steps = [int(s) for s in (strike_cfg.get("otm_steps") or [])]
    atm_band = int(strike_cfg.get("atm_band_steps", 1))
    states: dict[str, OptionState | None] = {}
    seen: set[str] = set()
    for side in ("CE", "PE"):
      for inst in candidate_instruments(
        universe,
        side,
        band_steps=atm_band,
        otm_steps=otm_steps,
        modes=modes,
        step=step,
      ):
        if inst.token in seen:
          continue
        seen.add(inst.token)
        states[inst.token] = self._option_data.state_with_mark(inst.token)
      if len(states) < 2:
        for inst in atm_band_instruments(universe, side, band_steps=band, step=step):
          if inst.token in seen:
            continue
          seen.add(inst.token)
          states[inst.token] = self._option_data.state_with_mark(inst.token)
    if universe.atm_ce:
      states.setdefault(
        universe.atm_ce.token, self._option_data.state_with_mark(universe.atm_ce.token)
      )
    if universe.atm_pe:
      states.setdefault(
        universe.atm_pe.token, self._option_data.state_with_mark(universe.atm_pe.token)
      )
    return states

  def _refresh_chain_and_option_context(
    self,
    universe: ContractUniverse,
    band_states: dict[str, OptionState | None],
    spot: Decimal | None,
  ) -> None:
    """OI/PCR/MaxPain + ATM option VWAP/Greeks into FeatureEngine."""
    oi_states = dict(band_states)
    for inst in universe.instruments:
        if oi_states.get(inst.token) is not None:
            continue
        st = self._option_data.state_with_mark(inst.token)
        if st is not None and st.oi:
            oi_states[inst.token] = st
    chain = build_chain_snapshot(universe, oi_states, prior_oi=self._prior_oi)
    chain = self._enrich_chain_atm(chain, universe, band_states, spot)
    self.features.set_chain_snapshot(chain)
    # Refresh prior OI baseline for next delta
    for tok, st in oi_states.items():
      if st is not None and st.oi is not None:
        self._prior_oi[tok] = int(st.oi)

    # Prefer ATM side matching live bias for option VWAP / greeks context
    atm = universe.atm_ce or universe.atm_pe
    bias_spot = spot
    vwap = self._market_data.session_vwap_value
    prefer_pe = (
      bias_spot is not None and vwap is not None and bias_spot < vwap
    )
    inst = universe.atm_pe if prefer_pe and universe.atm_pe else universe.atm_ce
    if inst is None:
      inst = atm
    ctx: dict = {}
    if inst is not None:
      st = band_states.get(inst.token) or self._option_data.get(inst.token)
      if st is not None:
        ctx["ltp"] = float(st.ltp) if st.ltp is not None else None
        ctx["spread_pct"] = float(st.spread_pct) if st.spread_pct is not None else None
        ctx["volume"] = st.volume
        ctx["oi"] = st.oi
        ctx["option_vwap"] = self._option_data.option_vwap(inst.token)
        if st.broker_delta is not None:
          ctx["delta"] = st.broker_delta
          ctx["gamma"] = st.broker_gamma
          ctx["iv"] = st.broker_iv
          ctx["theta"] = st.broker_theta
          ctx["greeks_source"] = "broker"
        elif spot is not None and st.ltp is not None and inst.expiry_date is not None:
          g = compute_greeks(
            spot=float(spot),
            strike=float(inst.strike),
            premium=float(st.ltp),
            option_type=inst.option_type,
            expiry=inst.expiry_date.date(),
          )
          ctx["delta"] = g.delta
          ctx["gamma"] = g.gamma
          ctx["iv"] = g.iv
          ctx["theta"] = g.theta
          ctx["vega"] = g.vega
          ctx["greeks_source"] = "model"
    self.features.set_option_context(ctx)

  def _enrich_chain_atm(
    self,
    chain: dict[str, Any],
    universe: ContractUniverse,
    band_states: dict[str, OptionState | None],
    spot: Decimal | None,
  ) -> dict[str, Any]:
    out = dict(chain)
    for key, inst in (("atm_ce", universe.atm_ce), ("atm_pe", universe.atm_pe)):
      if inst is None:
        continue
      st = band_states.get(inst.token) or self._option_data.get(inst.token)
      prev_oi = self._prior_oi.get(inst.token)
      row: dict[str, Any] = {"token": inst.token, "strike": float(inst.strike)}
      if st is not None:
        row.update(
          {
            "ltp": float(st.ltp) if st.ltp is not None else None,
            "volume": st.volume,
            "oi": st.oi,
            "oi_change": (
              int(st.oi or 0) - int(prev_oi)
              if st.oi is not None and prev_oi is not None
              else None
            ),
            "spread_pct": float(st.spread_pct) if st.spread_pct is not None else None,
            "option_vwap": self._option_data.option_vwap(inst.token),
          }
        )
        if st.broker_delta is not None:
          row["delta"] = st.broker_delta
          row["gamma"] = st.broker_gamma
          row["iv"] = st.broker_iv
          row["theta"] = st.broker_theta
          row["greeks_source"] = "broker"
        elif spot is not None and st.ltp is not None and inst.expiry_date is not None:
          g = compute_greeks(
            spot=float(spot),
            strike=float(inst.strike),
            premium=float(st.ltp),
            option_type=inst.option_type,
            expiry=inst.expiry_date.date(),
          )
          row["delta"] = g.delta
          row["gamma"] = g.gamma
          row["iv"] = g.iv
          row["theta"] = g.theta
          row["vega"] = g.vega
          row["greeks_source"] = "model"
      out[key] = row
    return out

  def refresh_mi_snapshot(self) -> dict[str, Any]:
    """Build MI on demand for API/cockpit when periodic scan has not run yet."""
    if self._last_mi_snapshot:
      return self._last_mi_snapshot
    if self._universe is None:
      return {"enabled": True, "pending": True, "version": self._mi_cfg.get("version", "mi_v1.0.0")}
    try:
      features = self.features.compute_cached(max_age_sec=5.0)
      m1 = self._market_data.candles(CandleInterval.M1)
      m5 = self._market_data.candles(CandleInterval.M5)
      session_date = datetime.now(IST).date()
      expiry_date = self._contract_expiry_date()
      dte = (
        days_to_expiry(session_date, expiry_date)
        if expiry_date is not None
        else None
      )
      spot = features.nifty_spot or self._market_data.spot_ltp
      universe = self._universe
      if spot is not None:
        universe = self._contract_selector.retarget_atm(self._universe, spot)
      band_states = self._band_option_states(universe)
      _, mi = self._build_market_intelligence(
        features,
        m1,
        m5,
        universe,
        band_states,
        is_expiry=self._is_expiry_day(),
        dte=dte,
      )
      return mi
    except Exception:
      logger.exception("mi_snapshot_refresh_failed")
      return {"enabled": True, "pending": True, "error": "build_failed"}

  def _build_market_intelligence(
    self,
    features,
    m1,
    m5,
    universe: ContractUniverse,
    band_states: dict[str, OptionState | None],
    *,
    is_expiry: bool,
    dte: int | None,
  ) -> tuple[Any, dict[str, Any]]:
    extra = dict(features.extra or {})
    index_ltps = dict(self._index_ltps)
    if extra.get("cash_index_spot") is not None:
      index_ltps["NIFTY"] = Decimal(str(extra["cash_index_spot"]))
    if extra.get("fut_spot") is not None:
      index_ltps.setdefault("NIFTY_FUT", Decimal(str(extra["fut_spot"])))

    ctx = MarketIntelligenceContext(
      features=features,
      m1=m1,
      m5=m5,
      index_ltps=index_ltps,
      index_opens=dict(self._index_opens),
      chain_snapshot=self.features.chain_snapshot or {},
      option_states=band_states,
      now=datetime.now(tz=timezone.utc),
      is_expiry_day=is_expiry,
      days_to_expiry=dte,
    )
    mi = self._mi_engine.build(ctx)
    self._last_mi_snapshot = mi
    if self._mi_cfg.get("phases", {}).get("ml_shadow", True):
      self._last_ml_prediction = self._ml_service.predict(mi, signal_confidence=0)
    extra = self._mi_engine.apply_to_features_extra(extra, mi)
    return features.model_copy(update={"extra": extra}), mi

  async def _log_mi_candidate(
    self,
    signal: CandidateSignal | None,
    features,
    regime,
    mi: dict[str, Any],
    *,
    approved: bool = False,
    veto_reasons: list[str] | None = None,
  ) -> None:
    log_cfg = self._mi_cfg.get("candidate_log") or {}
    if not log_cfg.get("enabled", True):
      return
    if signal is None and not log_cfg.get("log_all_candidates", False):
      return
    ml_pred = None
    if self._mi_cfg.get("phases", {}).get("ml_shadow", True):
      ml_pred = self._ml_service.predict(
        mi,
        signal_confidence=signal.confidence if signal else 0,
      )
      self._last_ml_prediction = ml_pred
      if signal is not None:
        ml_pred = {
          **ml_pred,
          **self._ml_service.shadow_log_fields(approved, ml_pred),
        }
    record = build_candidate_record(
      signal,
      features=features,
      regime=regime,
      mi_snapshot=mi,
      ml_prediction=ml_pred,
      approved=approved,
      veto_reasons=veto_reasons,
    )
    await log_candidate(self._journal, record, enabled=True)

  @property
  def last_scan_completed_at(self) -> datetime | None:
    return self._last_scan_completed_at

  def _time_of_day_blocks(self, signal: CandidateSignal) -> list[str]:
    now = datetime.now(tz=timezone.utc)
    spot_m1 = self._market_data.candles(CandleInterval.M1)
    reasons = tod_dead_zone_rejection_reasons(
      self._config.validator.get("tod_dead_zone") or {},
      now=now,
      spot_m1=spot_m1,
    )
    tod = self._config.validator.get("time_of_day_cautious") or {}
    reasons.extend(time_of_day_cautious_rejection_reasons(signal, tod, now=now))
    return reasons

  async def _log_entry_skipped_signal(
    self,
    signal: CandidateSignal,
    reasons: list[str],
    *,
    severity: str = "warning",
  ) -> None:
    self._last_entry_block = ", ".join(reasons)[:240]
    self._last_entry_confirm = None
    await self._journal.write_system_event(
      SystemEvent(
        event_type="entry_skipped",
        ts=datetime.now(tz=timezone.utc),
        severity=severity,
        message=f"Validation failed: {', '.join(reasons[:4])}",
        metadata={
          "setup": signal.setup_type,
          "side": signal.side,
          "tsym": signal.tsym,
          "confidence": signal.confidence,
          "underlying": self._active_underlying(),
          "block_reason": reasons[0] if reasons else "validation_failed",
          "rejection_reasons": reasons,
          "precheck": True,
        },
      )
    )

  async def _scan_for_entry(self) -> None:
    try:
      await self._scan_for_entry_inner()
    finally:
      self._last_scan_completed_at = datetime.now(tz=timezone.utc)
      decision = self.router.last_decision
      if decision is not None:
        logger.info(
          "scan_completed",
          underlying=self._active_underlying(),
          strategy=decision.selected_strategy,
          reason=(decision.selected_reason or "")[:160],
          trade_allowed=decision.trade_allowed,
          confidence=decision.confidence,
          regime=decision.regime.primary if decision.regime else None,
          m1_bars=len(self._market_data.candles(CandleInterval.M1)),
          candles_stale=self._market_data.candles_stale(),
        )

  async def _scan_for_entry_inner(self) -> None:
    if self._universe is None:
      return

    self._maybe_reset_session()

    if self._underlyings and self._switch_underlying is not None:
      symbol = self._underlyings[self._scan_underlying_idx % len(self._underlyings)]
      self._scan_underlying_idx += 1
      # Only switch/backfill when rotating to a different underlying — a full
      # backfill on every 10s scan was blocking the scan loop for minutes.
      if symbol != self._active_underlying():
        try:
          await self._switch_underlying(symbol)
          await self._market_data.backfill_today()
          await self._load_prior_day_levels()
        except Exception:
          logger.exception("underlying_switch_failed", symbol=symbol)

    refreshed = await self._market_data.refresh_session_candles()
    if refreshed:
      for interval_candles in self._market_data._candles.values():
        await self._journal.write_candles(interval_candles)

    # Stale candle feed → do not trade on outdated VWAP/setups.
    # A failed refresh alone is OK if we still have a fresh last bar.
    if self._market_data.candles_stale():
      if self._log_every:
        latest = self._market_data.latest_candle_ts()
        await self._journal.write_system_event(
          SystemEvent(
            event_type="strategy_decision",
            ts=datetime.now(tz=timezone.utc),
            severity="warning",
            message="NO_TRADE",
            metadata={
              "selected_strategy": "NO_TRADE",
              "selected_reason": "stale_candle_feed",
              "trade_allowed": False,
              "confidence": 0,
              "last_m1_ts": latest.isoformat() if latest else None,
              "last_refresh_ok": self._market_data.last_refresh_ok,
            },
          )
        )
      return

    m1 = self._market_data.candles(CandleInterval.M1)
    m3 = self._market_data.candles(CandleInterval.M3)
    m5 = self._market_data.candles(CandleInterval.M5)
    # Match day_backtest warmup so live does not scan on thin early-session bars.
    min_m1 = int(self._config.runtime.get("min_m1_bars_for_entry", 20))
    min_m3 = int(self._config.runtime.get("min_m3_bars_for_entry", 5))
    if len(m1) < min_m1 or len(m3) < min_m3:
      if self._log_every:
        await self._journal.write_system_event(
          SystemEvent(
            event_type="strategy_decision",
            ts=datetime.now(tz=timezone.utc),
            severity="debug",
            message="NO_TRADE",
            metadata={
              "selected_strategy": "NO_TRADE",
              "selected_reason": "warmup_insufficient_bars",
              "trade_allowed": False,
              "confidence": 0,
              "m1_bars": len(m1),
              "m3_bars": len(m3),
              "min_m1_bars_for_entry": min_m1,
              "min_m3_bars_for_entry": min_m3,
            },
          )
        )
      return

    features = self.features.compute()
    is_expiry = self._is_expiry_day()
    session_date = datetime.now(IST).date()
    expiry_date = self._contract_expiry_date()
    self.features.is_expiry_day = is_expiry
    self.features.expiry_date = expiry_date
    self.features.session_date = session_date
    dte = (
      days_to_expiry(session_date, expiry_date)
      if expiry_date is not None
      else None
    )

    # Retarget ATM from cash index when dual-chart mode is on (ITM/ATM/OTM math),
    # else fall back to FUT LTP used for momentum/VWAP.
    spot = features.nifty_spot or self._market_data.spot_ltp
    universe = self._universe
    atm_px = spot
    extra0 = features.extra or {}
    prefer_cash_atm = bool(self._config.symbols.get("atm_from_cash_index", True))
    cash_atm = extra0.get("cash_index_spot")
    if prefer_cash_atm and cash_atm is not None:
      atm_px = Decimal(str(cash_atm))
    if atm_px is not None and self._universe is not None:
      universe = self._contract_selector.retarget_atm(self._universe, atm_px)
      self._universe = universe

    try:
      await self._market_data.refresh_option_band_candles(universe.instruments)
    except Exception:
      logger.exception("option_band_candle_refresh_failed")

    # ATM±1 (and ATM) LTP map for Greek strike selection on both sides.
    band_states = self._band_option_states(universe)
    self._record_premium_samples(band_states, spot)
    self._refresh_chain_and_option_context(universe, band_states, spot)
    # Recompute features with chain + option Greeks context.
    features = self.features.compute()
    features, mi = self._build_market_intelligence(
      features,
      m1,
      m5,
      universe,
      band_states,
      is_expiry=is_expiry,
      dte=dte,
    )
    regime = self.regime.classify(
      features, m1, m5, is_expiry_day=is_expiry, days_to_expiry=dte
    )
    self.positions.set_regime(regime.primary)
    extra = dict(features.extra or {})
    extra["candles_stale"] = self._market_data.candles_stale()
    extra["last_refresh_ok"] = self._market_data.last_refresh_ok
    if regime.health.get("transition_state"):
      extra["transition_state"] = regime.health["transition_state"]
    features = features.model_copy(update={"extra": extra})
    ce_state = (
      band_states.get(universe.atm_ce.token) if universe.atm_ce else None
    )
    pe_state = (
      band_states.get(universe.atm_pe.token) if universe.atm_pe else None
    )

    options_by_strategy = {s.name: band_states for s in self.router._scanners}

    lift_cfg = dict(self._config.strategy.get("option_lift") or {})
    use_lift = bool(lift_cfg.get("enabled", False)) and bool(
      lift_cfg.get("replace_setup_entries", True)
    )
    lift_batch: list[CandidateSignal] = []
    if use_lift:
      lift_cfg.setdefault("strike_step", self._config.symbols.get("strike_step", 50))
      option_m1 = {
        inst.token: self._market_data.candles_for_token(inst.token, CandleInterval.M1)
        for inst in (universe.instruments or [])
        if getattr(inst, "option_type", None) in ("CE", "PE")
      }
      found, lift_note = find_option_lifts(
        features=features,
        universe=universe,
        band_states=band_states,
        option_m1_by_token=option_m1,
        spot_m1=m1,
        cfg=lift_cfg,
        strategy_version=str(
          self._config.strategy.get("strategy_version", "strategy_router_v1.1.0")
        ),
      )
      eligible: list[CandidateSignal] = []
      for sig in found:
        if self.positions.has_open_for_token(sig.instrument_token):
          continue
        bar_key = f"{sig.instrument_token}:{sig.scanner_metadata.get('bar_ts')}"
        if bar_key in self._option_lift_sent:
          continue
        eligible.append(sig)
      confirm_scans = max(1, int(lift_cfg.get("confirm_scans", 2)))
      ready, waiting, self._option_lift_streaks = split_confirmed_lifts(
        eligible,
        self._option_lift_streaks,
        required=confirm_scans,
      )
      # Today-only: CE ATM+ confirm → buy PE ATM-2 (mirror PE→CE). Confirm stays on source.
      remapped = apply_opposite_trade_today(
        ready,
        universe=universe,
        band_states=band_states,
        cfg=lift_cfg,
      )
      lift_batch = [
        sig
        for sig in remapped
        if not self.positions.has_open_for_token(sig.instrument_token)
      ]
      signal = lift_batch[0] if lift_batch else None
      if found and not eligible:
        lift_note["reason"] = "qualifying lifts already open or offered this bar"
      elif waiting and not ready:
        bits = ", ".join(f"{sig.tsym} {streak}/{confirm_scans}" for sig, streak in waiting[:6])
        lift_note["reason"] = f"confirm {bits}"
      elif len(remapped) > 1:
        names = ", ".join(
          (
            f"{sig.tsym}←{((sig.scanner_metadata or {}).get('opposite_of_tsym') or '?')}"
            if (sig.scanner_metadata or {}).get("opposite_trade")
            else sig.tsym
          )
          for sig in remapped[:6]
        )
        lift_note["reason"] = f"{len(remapped)} lifts: {names}"
        if waiting:
          lift_note["reason"] += f" · {len(waiting)} still confirming"
      elif signal is not None and (signal.scanner_metadata or {}).get("opposite_trade"):
        src = (signal.scanner_metadata or {}).get("opposite_of_tsym") or "?"
        lift_note["reason"] = f"opposite today: {src} → {signal.tsym}"
      shown = lift_batch or ready or [sig for sig, _streak in waiting]
      sides = {sig.side for sig in shown}
      if not shown:
        position_side = "NONE"
      elif len(sides) == 1:
        position_side = shown[0].side
      else:
        position_side = "CE+PE"
      decision = StrategyDecision(
        ts=datetime.now(tz=timezone.utc),
        selected_strategy=OPTION_LIFT if signal is not None else "NO_TRADE",
        confidence=int(signal.confidence or 0) if signal is not None else 0,
        trade_allowed=signal is not None,
        position_side=position_side,
        selected_reason=str(lift_note.get("reason") or "")[:180],
        regime=regime,
      )
      self.router.last_decision = decision
    else:
      decision, signal = self.router.route(
        features,
        regime,
        universe,
        options_by_strategy,
        is_expiry_day=is_expiry,
        learner=self.learner,
        session_date=session_date,
        expiry_date=expiry_date,
      )

    execution_meta: dict[str, Any] = {}
    executable: CandidateSignal | None = None
    self._last_entry_confirm = None

    if signal is not None:
      tod_reasons = self._time_of_day_blocks(signal)
      if tod_reasons:
        await self._log_entry_skipped_signal(signal, tod_reasons)
        execution_meta = {
          "execution_status": "blocked",
          "block_reasons": tod_reasons,
        }
      else:
        confirmed, wait_reason = self._entry_confirm.consider(
          signal,
          in_open_gap_window=in_open_gap_entry_window(
            datetime.now(tz=timezone.utc),
            entry_start=str(self._config.validator.get("entry_start_time", "09:30")),
            open_window_minutes=int(
              (self._config.strategy.get("entry_confirmation") or {}).get(
                "open_window_minutes", 15
              )
            ),
          ),
        )
        if confirmed is None:
          if wait_reason:
            self._last_entry_confirm = wait_reason
            execution_meta = {
              "execution_status": "confirm_wait",
              "entry_confirm": wait_reason,
            }
            await self._journal.write_system_event(
              SystemEvent(
                event_type="entry_confirm_wait",
                ts=datetime.now(tz=timezone.utc),
                severity="info",
                message=wait_reason,
                metadata={
                  "setup": signal.setup_type,
                  "side": signal.side,
                  "tsym": signal.tsym,
                  "confidence": signal.confidence,
                  "confirmation_start": datetime.now(tz=timezone.utc).isoformat(),
                },
              )
            )
        else:
          self._last_entry_block = None
          executable = confirmed
          execution_meta = {"execution_status": "ready"}
    else:
      self._entry_confirm.consider(None)

    # Persist scan decisions (deduped identical NO_TRADE streaks for the audit feed).
    if self._log_every:
      should_log, score_meta = self._decision_log_policy.prepare_log(
        decision,
        now=decision.ts,
      )
      if should_log:
        meta = decision.model_dump(mode="json")
        meta.update(score_meta)
        meta.update(execution_meta)
        meta["scan_underlying"] = self._active_underlying()
        meta["scan_interval_seconds"] = self._scan_interval_sec
        meta["entry_scan_on_quote"] = self._entry_scan_on_quote
        meta["exit_eval_interval_seconds"] = self.positions.exit_eval_interval_seconds()
        meta["candles_stale"] = self._market_data.candles_stale()
        meta["last_refresh_ok"] = self._market_data.last_refresh_ok
        meta["m1_bars"] = len(m1)
        meta["m3_bars"] = len(m3)
        meta["m5_bars"] = len(m5)
        meta["forming_m1_from_quotes"] = bool(
          self._config.runtime.get("forming_m1_from_quotes", True)
        )
        meta["gap_points"] = (features.extra or {}).get("gap_points")
        meta["prior_close"] = (features.extra or {}).get("prior_close")
        meta["ce_ltp"] = float(ce_state.ltp) if ce_state and ce_state.ltp is not None else None
        meta["pe_ltp"] = float(pe_state.ltp) if pe_state and pe_state.ltp is not None else None
        meta["market_intelligence"] = mi
        meta["option_band_m1_tokens"] = sum(
          1
          for inst in (universe.instruments or [])
          if getattr(inst, "option_type", None) in ("CE", "PE")
          and len(
            self._market_data.candles_for_token(
              str(getattr(inst, "token", "") or ""), CandleInterval.M1
            )
          )
          >= 1
        )
        log_message = decision.selected_reason or decision.selected_strategy
        if execution_meta.get("execution_status") == "confirm_wait":
          log_message = f"candidate_pending: {execution_meta.get('entry_confirm')}"
        elif execution_meta.get("execution_status") == "blocked":
          log_message = f"entry_blocked: {', '.join(execution_meta.get('block_reasons') or [])[:120]}"
        await self._journal.write_system_event(
          SystemEvent(
            event_type="strategy_decision",
            ts=decision.ts,
            severity=(
              "warning"
              if execution_meta.get("execution_status") in ("blocked", "confirm_wait")
              else ("info" if decision.trade_allowed else "debug")
            ),
            message=log_message,
            metadata=meta,
          )
        )

    if executable is None:
      await self._log_mi_candidate(signal, features, regime, mi, approved=False)
      if signal is None:
        logger.info(
          "scan_no_trade",
          strategy=decision.selected_strategy,
          reason=decision.selected_reason,
          bias=features.bias_5m.value,
          regime=regime.primary,
          setup=features.setup_3m,
          trigger=features.trigger_1m,
          pullback=(features.extra or {}).get("setup_vwap_pullback"),
          pe_ltp=str(pe_state.ltp) if pe_state and pe_state.ltp is not None else None,
          ce_ltp=str(ce_state.ltp) if ce_state and ce_state.ltp is not None else None,
        )
      return

    signal = executable
    await self._log_mi_candidate(signal, features, regime, mi, approved=decision.trade_allowed)

    confirm_meta = dict(signal.scanner_metadata or {})
    timing = dict(confirm_meta.get("confirmation_timing") or {})
    timing.setdefault("confirmation_completed", datetime.now(tz=timezone.utc).isoformat())
    timing.setdefault("confirm_path", "scan_interval")
    confirm_meta["confirmation_timing"] = timing
    signal = signal.model_copy(update={"scanner_metadata": confirm_meta})

    from algoflat.market_session import is_entry_window_open

    if not is_entry_window_open(self._config.validator):
      await self._journal.write_system_event(
        SystemEvent(
          event_type="entry_skipped",
          ts=datetime.now(tz=timezone.utc),
          severity="debug",
          message=f"Signal {signal.setup_type} skipped — outside entry window",
          metadata={
            "setup": signal.setup_type,
            "side": signal.side,
            "reason": "outside_entry_window",
            "entry_start": self._config.validator.get("entry_start_time"),
            "entry_end": self._config.validator.get("entry_end_time"),
          },
        )
      )
      return

    bought = False
    one_fill_per_scan = bool(lift_cfg.get("one_fill_per_scan", False))
    for sig in lift_batch:
      meta = sig.scanner_metadata or {}
      # Deeper in-the-money adds only follow a fill of the strike we chose.
      if meta.get("itm_companion") and not bought:
        continue
      if meta.get("capital_fallback") and bought:
        break
      if bought and (one_fill_per_scan or _lift_batch_should_stop(self._last_entry_block)):
        break
      if not bought and _lift_batch_should_stop(self._last_entry_block):
        break
      book_block = self._open_book_entry_block(sig.side)
      if book_block:
        self._last_entry_block = book_block
        await self._log_entry_skipped_signal(sig, [book_block])
        break
      self._mark_option_lift_sent(sig)
      filled = await self._execute_signal(sig, is_expiry=is_expiry)
      if filled:
        bought = True
        if one_fill_per_scan:
          break
        continue
      if _lift_size_skip(self._last_entry_block):
        continue
      if _lift_batch_should_stop(self._last_entry_block):
        break

  async def _try_reversal_flip(self, flip: dict) -> None:
    """After trend_reversal exit, buy the opposite side (CE↔PE) if risk allows."""
    if self._universe is None:
      return
    side = flip.get("side")
    if side not in ("CE", "PE"):
      return

    features = self.features.compute()
    spot = features.nifty_spot or self._market_data.spot_ltp
    if spot is None:
      return
    universe = self._contract_selector.retarget_atm(self._universe, spot)
    self._universe = universe
    band_states = self._band_option_states(universe)
    resolved = resolve_side_contract(
      config=self._config,
      universe=universe,
      side=side,
      spot=spot,
      option_states=band_states,
    )
    if resolved is None:
      logger.info("reversal_flip_no_contract", side=side)
      return
    inst, opt_state, pick = resolved

    # Align feature bias with flip direction for quality scoring.
    bias = Bias.BULLISH if side == "CE" else Bias.BEARISH
    feat = features.model_copy(update={"bias_5m": bias})
    signal = CandidateSignal(
      ts=datetime.now(tz=timezone.utc),
      setup_type="trend_reversal_flip",
      side=side,
      instrument_token=inst.token,
      tsym=inst.tsym,
      strategy_version=self._config.strategy.get(
        "strategy_version", "strategy_router_v1.1.0"
      ),
      feature_snapshot=feat,
      confidence=80,
      scanner_metadata={
        "atm_strike": str(universe.atm_strike),
        "option_ltp": str(opt_state.ltp),
        "exchange": inst.exchange,
        "lot_size": inst.lot_size,
        "flip_from": flip.get("from_side"),
        "strike_pick": pick,
      },
    )
    logger.info(
      "reversal_flip_entry_attempt",
      from_side=flip.get("from_side"),
      to_side=side,
      tsym=inst.tsym,
      strike=str(inst.strike),
      delta=pick.get("delta"),
    )
    await self._execute_signal(signal, is_expiry=self._is_expiry_day())

  async def _execute_signal(self, signal: CandidateSignal, *, is_expiry: bool) -> bool:
    token = signal.instrument_token
    if self.positions.entry_blocked_for_token(token) or token in self._entering_tokens:
      logger.info(
        "entry_skipped_duplicate_guard",
        tsym=signal.tsym,
        token=token,
        blocked=self.positions.entry_blocked_for_token(token),
        entering=token in self._entering_tokens,
      )
      return False

    self._entering_tokens.add(token)
    try:
      return await self._execute_signal_locked(signal, is_expiry=is_expiry)
    finally:
      self._entering_tokens.discard(token)

  async def _execute_signal_locked(
    self, signal: CandidateSignal, *, is_expiry: bool
  ) -> bool:
    token = signal.instrument_token
    # Re-check after any await gap from the caller path.
    if self.positions.has_open_for_token(token):
      return False

    book_block = self._open_book_entry_block(signal.side)
    if book_block:
      self._last_entry_block = book_block
      await self._log_entry_skipped_signal(signal, [book_block])
      return False

    risk_snap = await self.risk.snapshot_for_execution(
      live_broker=self._broker.live_broker
      if isinstance(self._broker, RoutedBrokerAdapter)
      else None,
    )
    if risk_snap.kill_switch or risk_snap.entries_blocked:
      await self._journal.write_system_event(
        SystemEvent(
          event_type="entry_skipped",
          ts=signal.ts,
          severity="warning",
          message=f"Signal {signal.setup_type} {signal.side} skipped — entries blocked",
          metadata={
            "setup": signal.setup_type,
            "side": signal.side,
            "tsym": signal.tsym,
            "confidence": signal.confidence,
            "kill_switch": risk_snap.kill_switch,
            "entries_blocked": risk_snap.entries_blocked,
            "block_reason": risk_snap.block_reason,
          },
        )
      )
      return False

    await self._journal.write_candidate_signal(signal)

    order_ts = datetime.now(tz=timezone.utc)
    timing = dict((signal.scanner_metadata or {}).get("confirmation_timing") or {})
    timing["order_attempt"] = order_ts.isoformat()
    confirmed_ltp = (signal.scanner_metadata or {}).get("option_ltp")

    option_exch = str(
      (signal.scanner_metadata or {}).get("exchange")
      or self._config.symbols.get("exchange_options", "BFO")
    )
    # Buy at current WebSocket LTP only — never confirm-time / REST / mid / EMA.
    opt_for_signal = await self._fresh_option_ltp_state(
      token, exchange=option_exch, tsym=signal.tsym
    )
    if opt_for_signal is None or opt_for_signal.ltp is None or opt_for_signal.ltp <= 0:
      self._last_entry_block = "stale_or_missing_ltp"
      await self._log_entry_skipped_signal(signal, ["stale_or_missing_ltp"])
      return False
    max_age = float(self._config.runtime.get("entry_max_quote_age_seconds", 2) or 0)
    if max_age > 0 and opt_for_signal.last_update_ts is not None:
      age = (order_ts - opt_for_signal.last_update_ts).total_seconds()
      if age > max_age:
        self._last_entry_block = "stale_or_missing_ltp"
        await self._log_entry_skipped_signal(
          signal, [f"stale_or_missing_ltp age={age:.1f}s"]
        )
        return False
    meta = dict(signal.scanner_metadata or {})
    meta["option_ltp"] = str(opt_for_signal.ltp)
    meta["option_mark"] = "ltp"
    meta["confirmed_option_ltp"] = str(confirmed_ltp) if confirmed_ltp is not None else None
    meta["confirmation_timing"] = timing
    if opt_for_signal.volume is not None:
      meta["volume"] = opt_for_signal.volume
    if opt_for_signal.oi is not None:
      meta["oi"] = opt_for_signal.oi
    signal = signal.model_copy(update={"scanner_metadata": meta, "ts": order_ts})
    logger.info(
      "entry_ltp_refreshed",
      tsym=signal.tsym,
      confirmed_ltp=str(confirmed_ltp) if confirmed_ltp is not None else None,
      order_ltp=str(opt_for_signal.ltp),
      ltp_source=opt_for_signal.quote_source or "websocket",
    )

    if self.positions.has_open_for_token(token):
      return False

    min_opt_m1 = int(self._config.runtime.get("min_option_m1_bars_for_chart", 5))
    if bool(self._config.runtime.get("force_fetch_candidate_option_m1", True)):
      try:
        await self._market_data.ensure_option_token_m1(
          token,
          exchange=option_exch,
          min_bars=min_opt_m1,
        )
      except Exception:
        logger.exception("option_candidate_m1_ensure_failed", token=token)

    option_m1 = self._market_data.candles_for_token(token, CandleInterval.M1)
    spot_m1 = self._market_data.candles(CandleInterval.M1)
    spot_m5 = self._market_data.candles(CandleInterval.M5)
    spot_m15 = self._market_data.candles(CandleInterval.M15)
    pdh = float(self.features.prior_high) if self.features.prior_high is not None else 0.0
    pdl = float(self.features.prior_low) if self.features.prior_low is not None else 0.0

    validation = self.validator.validate(
      signal,
      opt_for_signal,
      has_open_for_token=self.positions.has_open_for_token(token),
      # Opposite-side flip must not be blocked by the exit we just did.
      in_cooldown=(
        False
        if signal.setup_type == "trend_reversal_flip"
        else self.positions.in_cooldown(token)
      ),
      kill_switch=risk_snap.kill_switch,
      is_expiry_day=is_expiry,
      premium_tracker=self._premium_tracker,
      regime_primary=self.router.last_decision.regime.primary
      if self.router.last_decision and self.router.last_decision.regime
      else None,
      option_m1=option_m1,
      spot_m1=spot_m1,
      spot_m5=spot_m5,
      spot_m15=spot_m15,
      pdh=pdh,
      pdl=pdl,
      expiry=self._contract_expiry_date(),
    )
    await self._journal.write_validation(validation)
    if not validation.passed:
      reasons = list(validation.rejection_reasons)
      self._last_entry_block = ", ".join(reasons)[:240]
      self._last_entry_confirm = None
      await self._journal.write_system_event(
        SystemEvent(
          event_type="entry_skipped",
          ts=signal.ts,
          severity="warning",
          message=f"Validation failed: {', '.join(validation.rejection_reasons[:4])}",
          metadata={
            "setup": signal.setup_type,
            "side": signal.side,
            "tsym": signal.tsym,
            "confidence": signal.confidence,
            "underlying": self._active_underlying(),
            "block_reason": validation.rejection_reasons[0] if validation.rejection_reasons else "validation_failed",
            "rejection_reasons": validation.rejection_reasons,
            "option_m1_bars": len(option_m1 or []),
            "spot_m1_bars": len(spot_m1 or []),
            "option_m1_source": "cache_or_force_fetch",
          },
        )
      )
      return False

    # Re-read WebSocket LTP after validation/m1 so sizing uses the live print.
    fresh = await self._fresh_option_ltp_state(
      token, exchange=option_exch, tsym=signal.tsym
    )
    if fresh is None or fresh.ltp is None or fresh.ltp <= 0:
      self._last_entry_block = "stale_or_missing_ltp"
      await self._log_entry_skipped_signal(signal, ["stale_or_missing_ltp"])
      return False
    opt_for_signal = fresh
    meta = dict(signal.scanner_metadata or {})
    meta["option_ltp"] = str(opt_for_signal.ltp)
    meta["option_mark"] = "ltp"
    signal = signal.model_copy(update={"scanner_metadata": meta})

    sizing = await self.risk.size_entry(
      signal,
      opt_for_signal,
      risk_snap,
      open_position_count=self._open_count_for(self._active_underlying()),
      is_expiry_day=is_expiry,
    )
    if not sizing.approved:
      self._last_entry_block = sizing.rejection_reason or "risk_rejected"
      await self._journal.write_notification(
        "trade",
        "warning",
        "Entry blocked by risk",
        sizing.rejection_reason or "risk_rejected",
      )
      return False

    # Final guard immediately before capital reserve + fill.
    if self.positions.has_open_for_token(token):
      logger.info(
        "entry_aborted_token_opened_during_sizing",
        tsym=signal.tsym,
        token=token,
      )
      return False

    await self.risk.reserve_capital(sizing.premium_required, self._active_underlying())
    try:
      position_id, order_id, update = await self.execution.enter(signal, sizing)
      timing["order_placed"] = datetime.now(tz=timezone.utc).isoformat()
      signal = signal.model_copy(
        update={
          "scanner_metadata": {
            **(signal.scanner_metadata or {}),
            "confirmation_timing": timing,
          }
        }
      )
      await self.positions.register_open_from_order(
        position_id,
        order_id,
        signal.id,
        signal,
        sizing,
        update,
        is_expiry_day=is_expiry,
        mode=get_execution_mode(),
      )
      self._last_entry_block = None
      return True
    except Exception:
      await self.risk.release_capital(
        sizing.premium_required, Decimal("0"), self._active_underlying()
      )
      logger.exception("paper_entry_failed", tsym=signal.tsym)
      raise

  def _contract_expiry_date(self) -> date | None:
    if self._universe is None:
      return None
    if self._universe.atm_ce and self._universe.atm_ce.expiry_date:
      return self._universe.atm_ce.expiry_date.date()
    if self._universe.expiry_symbol:
      return parse_expiry_tag(self._universe.expiry_symbol)
    return None

  def _is_expiry_day(self) -> bool:
    if self._universe is None or not self._universe.expiry_symbol:
      return False
    exp = parse_expiry_tag(self._universe.expiry_symbol)
    if exp is None:
      return False
    return exp == datetime.now(IST).date()

  def _market_open(self) -> bool:
    from algoflat.market_session import is_market_open

    return is_market_open(self._config.market_session)
