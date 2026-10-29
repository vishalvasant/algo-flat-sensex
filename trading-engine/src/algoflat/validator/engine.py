from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import structlog

from algoflat.config import AppConfig
from algoflat.models.events import Candle, CandidateSignal, OptionState, ValidationResult
from algoflat.validator.basis_gate import basis_gate_rejection_reasons
from algoflat.validator.expiry_cautious import expiry_cautious_rejection_reasons
from algoflat.validator.fut_momentum import fut_momentum_rejection_reasons
from algoflat.validator.premium_behaviour import premium_responsive_rejection_reasons
from algoflat.validator.time_of_day_cautious import (
  time_of_day_cautious_rejection_reasons,
  tod_dead_zone_rejection_reasons,
)
from algoflat.validator.option_entry import option_entry_veto_reasons
from algoflat.validator.option_chart_confirm import option_chart_confirm_reasons
from algoflat.validator.quality_entry_pipeline import (
  candles_to_spot_bars,
  quality_pipeline_rejection_reasons,
)
from algoflat.validator.session_loss_guard import SessionLossGuard
from algoflat.validator.trap_avoidance import trap_rejection_reasons

if TYPE_CHECKING:
  from algoflat.validator.premium_behaviour import PremiumBehaviourTracker

logger = structlog.get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class RuleValidator:
  def __init__(self, config: AppConfig) -> None:
    self._config = config
    self._validator = config.validator
    self._version = self._validator.get("validator_version", "validator_v1.0.0")
    self._session_loss_guard = SessionLossGuard()

  def reset_session(self) -> None:
    self._session_loss_guard.reset()

  def record_session_loss(
    self,
    setup_type: str,
    *,
    is_expiry_day: bool,
    exit_reason: str | None,
  ) -> None:
    self._session_loss_guard.record_loss(
      setup_type,
      is_expiry_day=is_expiry_day,
      exit_reason=exit_reason,
      cfg=self._validator,
    )

  def validate(
    self,
    signal: CandidateSignal,
    option: OptionState | None,
    *,
    has_open_for_token: bool,
    in_cooldown: bool,
    kill_switch: bool,
    is_expiry_day: bool = False,
    now: datetime | None = None,
    premium_tracker: PremiumBehaviourTracker | None = None,
    regime_primary: str | None = None,
    option_m1: list[Candle] | None = None,
    spot_m1: list[Candle] | None = None,
    spot_m5: list[Candle] | None = None,
    spot_m15: list[Candle] | None = None,
    pdh: float = 0.0,
    pdl: float = 0.0,
    expiry: date | None = None,
  ) -> ValidationResult:
    reasons: list[str] = []
    ist_now = now.astimezone(IST) if now is not None else datetime.now(IST)
    clock = ist_now.time()

    from datetime import time

    start = time.fromisoformat(self._validator["entry_start_time"])
    end = time.fromisoformat(self._validator["entry_end_time"])
    if not (start <= clock <= end):
      reasons.append("outside_entry_window")
    else:
      reasons.extend(
        tod_dead_zone_rejection_reasons(
          self._validator.get("tod_dead_zone") or {},
          now=ist_now,
          spot_m1=spot_m1,
        )
      )
      tod = self._validator.get("time_of_day_cautious") or {}
      if tod.get("enabled", False):
        reasons.extend(
          time_of_day_cautious_rejection_reasons(signal, tod, now=ist_now)
        )

    if kill_switch:
      reasons.append("kill_switch_active")

    if is_expiry_day and bool(self._validator.get("expiry_day_block_entries", False)):
      reasons.append("expiry_day_block")
    if is_expiry_day:
      cautious = self._validator.get("expiry_day_cautious") or {}
      if cautious.get("enabled", False):
        reasons.extend(
          expiry_cautious_rejection_reasons(signal, cautious, now=ist_now)
        )
    reasons.extend(
      trap_rejection_reasons(signal, self._validator.get("trap_avoidance") or {})
    )
    reasons.extend(
      fut_momentum_rejection_reasons(
        signal,
        cfg=self._validator.get("fut_momentum_confirm") or {},
      )
    )
    reasons.extend(
      basis_gate_rejection_reasons(
        signal,
        cfg=self._validator.get("basis_gate") or {},
      )
    )
    reasons.extend(
      premium_responsive_rejection_reasons(
        signal,
        premium_tracker,
        self._validator.get("premium_responsive") or {},
        is_expiry_day=is_expiry_day,
        regime_primary=regime_primary,
      )
    )
    opt_ltp = float(option.ltp) if option is not None and option.ltp is not None else None
    reasons.extend(
      option_chart_confirm_reasons(
        signal,
        option_m1,
        cfg=self._validator.get("option_chart_confirm") or {},
        now=now,
        spot_m1=spot_m1,
      )
    )
    reasons.extend(
      option_entry_veto_reasons(
        signal,
        opt_ltp,
        cfg=self._validator.get("option_entry_veto") or {},
        premium_tracker=premium_tracker,
        now=now,
      )
    )
    # Quality funnel for option_lift: greeks → option VWAP → Nifty hard veto.
    q_cfg = self._validator.get("quality_entry_pipeline") or {}
    if q_cfg.get("enabled", False) and opt_ltp is not None:
      meta = signal.scanner_metadata or {}
      try:
        strike = float(meta.get("strike") or 0)
      except (TypeError, ValueError):
        strike = 0.0
      spot_px = 0.0
      fs = signal.feature_snapshot
      if fs is not None:
        extra = fs.extra or {}
        cash = extra.get("cash_index_spot")
        if cash is not None:
          spot_px = float(cash)
        elif fs.nifty_spot is not None:
          spot_px = float(fs.nifty_spot)
      opt_vwap = None
      if meta.get("option_vwap") is not None:
        try:
          opt_vwap = float(meta["option_vwap"])
        except (TypeError, ValueError):
          opt_vwap = None
      if opt_vwap is None and option_m1:
        from algoflat.market_data.vwap import session_vwap

        closed = [c for c in option_m1 if c.ts + timedelta(minutes=1) <= ist_now]
        vw = session_vwap(closed or option_m1)
        if vw is not None and vw > 0:
          opt_vwap = float(vw)
      bars5 = candles_to_spot_bars(spot_m5)
      bars15 = candles_to_spot_bars(spot_m15)
      if not bars5 and spot_m1:
        # Fallback: build 5m/15m from 1m when higher-TF cache is empty.
        from algoflat.features.indicators import aggregate_from_m1

        bars5 = candles_to_spot_bars(aggregate_from_m1(spot_m1, 5))
        bars15 = candles_to_spot_bars(aggregate_from_m1(spot_m1, 15))
      def _opt_float(key: str) -> float | None:
        raw = meta.get(key)
        if raw is None:
          return None
        try:
          return float(raw)
        except (TypeError, ValueError):
          return None

      reasons.extend(
        quality_pipeline_rejection_reasons(
          setup_type=signal.setup_type,
          side=signal.side,
          premium=float(opt_ltp),
          strike=strike,
          spot=spot_px,
          expiry=expiry,
          option_vwap=opt_vwap,
          delta=_opt_float("delta"),
          theta=_opt_float("theta"),
          tsym=signal.tsym,
          bars5=bars5,
          bars15=bars15,
          cfg=q_cfg,
          pdh=pdh,
          pdl=pdl,
          ts=ist_now,
        )
      )
    reasons.extend(
      self._session_loss_guard.rejection_reasons(
        signal,
        is_expiry_day=is_expiry_day,
        cfg=self._validator,
      )
    )

    if has_open_for_token:
      reasons.append("position_already_open_for_symbol")

    if in_cooldown:
      reasons.append("cooldown_active")

    if option is None:
      reasons.append("option_state_missing")
    else:
      relaxed = self._validator.get("paper_relaxed_liquidity", False)
      # Paper/pre-market often lacks bid/ask depth — LTP is enough for our ATM entries.
      # require_liquidity_present: missing OI or volume is a failed entry, not a skip.
      soft_fields = {"oi", "volume", "bid", "ask"} if relaxed else {"oi", "volume"}
      if not relaxed and self._validator.get("require_liquidity_present", False):
        soft_fields = {"bid", "ask"}
      for field in self._validator.get("required_fields", []):
        val = getattr(option, field, None)
        if val is None:
          if field in soft_fields:
            continue
          reasons.append(f"missing_{field}")

      if option.spread_pct is not None:
        max_spread = Decimal(str(self._validator.get("max_spread_pct", 2.0)))
        if option.spread_pct > max_spread:
          reasons.append("spread_too_wide")

      if not relaxed:
        min_vol = int(self._validator.get("min_option_volume", 0))
        min_oi = int(self._validator.get("min_option_oi", 0))
        if option.volume is not None and option.volume < min_vol:
          reasons.append("low_volume")
        if option.oi is not None and option.oi < min_oi:
          reasons.append("low_oi")

    passed = len(reasons) == 0
    result = ValidationResult(
      candidate_signal_id=signal.id,
      ts=now if now is not None else datetime.now(tz=timezone.utc),
      passed=passed,
      rejection_reasons=reasons,
      validator_version=self._version,
    )
    if not passed:
      logger.info("candidate_rejected", reasons=reasons, tsym=signal.tsym)
    return result
