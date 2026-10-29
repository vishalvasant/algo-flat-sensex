from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal

from algoflat.market_data.engine import MarketDataEngine
from algoflat.market_data.vwap import session_vwap
from algoflat.models.events import Candle
from algoflat.validator.option_chart_confirm import closed_bars_before


@dataclass
class ExitDecision:
  should_exit: bool
  reason: str | None = None


# Hard stops bypass min_hold — adverse SL must fire immediately on valid quotes.
HARD_EXIT_REASONS = frozenset({"force_exit", "adverse_momentum", "max_loss_inr"})


def _adverse_stop_pct(
  exit_cfg: dict,
  *,
  confidence: int | None,
  high_vol: bool,
  weak_entry: bool = False,
) -> Decimal:
  """Max loss % from entry — 4–5% default; tighter when entry quality was weak."""
  if weak_entry:
    return Decimal(str(exit_cfg.get("weak_entry_adverse_pct", 2.5))) / Decimal("100")

  key = "high_vol_adverse_move_pct_from_entry" if high_vol else "adverse_move_pct_from_entry"
  max_pct = Decimal(str(exit_cfg.get(key, exit_cfg.get("adverse_move_pct_from_entry", 5)))) / Decimal(
    "100"
  )
  if not exit_cfg.get("confidence_adverse_scaling", False) or confidence is None:
    return max_pct

  min_pct = Decimal(str(exit_cfg.get("adverse_move_min_pct", 4))) / Decimal("100")
  base_conf = int(exit_cfg.get("adverse_base_confidence", 76))
  top_conf = int(exit_cfg.get("adverse_max_confidence", 100))
  conf = max(base_conf, min(int(confidence), top_conf))
  if top_conf <= base_conf:
    return max_pct
  ratio = Decimal(conf - base_conf) / Decimal(top_conf - base_conf)
  return min_pct + (max_pct - min_pct) * ratio


def _entry_quality_weak(entry_meta: dict | None, exit_cfg: dict) -> bool:
  """True when greeks/volume/OI at entry were below supportive thresholds."""
  if not entry_meta:
    return False
  threshold = Decimal(str(exit_cfg.get("weak_entry_greeks_mult_threshold", 0.80)))
  gm = entry_meta.get("greeks_mult")
  if gm is not None and Decimal(str(gm)) < threshold:
    return True
  min_vol = int(exit_cfg.get("weak_entry_min_volume", 0) or 0)
  min_oi = int(exit_cfg.get("weak_entry_min_oi", 0) or 0)
  vol = entry_meta.get("volume")
  oi = entry_meta.get("oi")
  if min_vol > 0 and vol is not None and int(vol) < min_vol:
    return True
  if min_oi > 0 and oi is not None and int(oi) < min_oi:
    return True
  return False


def _momentum_trail_floor(
  entry_price: Decimal,
  mfe_points: Decimal,
  exit_cfg: dict,
) -> Decimal | None:
  """
  Profit trail floor — no profit target / no upside cap.

  When ``profit_trail_lock_pct`` is set (e.g. 0.55): on a pullback from peak,
  lock that fraction of peak open profit (floor = entry + MFE×lock). The trade
  may keep running indefinitely until this floor is hit or session flatten.

  Optional ``profit_trail_arm_mfe_pct``: do not arm the trail until MFE reaches
  that % of entry (avoids scratching a runner on the first tick of green).

  Else legacy mode: breakeven once green; above step-arm MFE lock (peak% - buffer%).
  Floor is snapped to 0.05 so LTP touch/below compares cleanly on the tick grid.
  """
  if mfe_points <= 0 or entry_price <= 0:
    return None

  tick = Decimal(str(exit_cfg.get("price_tick", "0.05")))
  lock_raw = exit_cfg.get("profit_trail_lock_pct")
  if lock_raw is not None:
    lock = Decimal(str(lock_raw))
    if lock <= 0:
      return None
    arm_pct = Decimal(str(exit_cfg.get("profit_trail_arm_mfe_pct", 0) or 0)) / Decimal("100")
    if arm_pct > 0 and mfe_points < entry_price * arm_pct:
      return None
    floor = entry_price + mfe_points * lock
    if floor < entry_price:
      floor = entry_price
    ticks = int((floor / tick).to_integral_value(rounding=ROUND_FLOOR))
    return (tick * ticks).quantize(tick)

  mfe_pct = mfe_points / entry_price
  breakeven_arm = Decimal(str(exit_cfg.get("trail_breakeven_arm_mfe_pct", 2))) / Decimal("100")
  if mfe_pct < breakeven_arm:
    return None

  step_arm = Decimal(str(exit_cfg.get("trail_step_arm_mfe_pct", 4))) / Decimal("100")
  buffer_pct = Decimal(str(exit_cfg.get("trail_step_buffer_pct", 2))) / Decimal("100")

  if mfe_pct <= step_arm:
    floor = entry_price
  else:
    locked_pct = mfe_pct - buffer_pct
    if locked_pct <= 0:
      floor = entry_price
    else:
      floor = entry_price * (Decimal("1") + locked_pct)

  # Snap down to tick — exit when LTP <= floor (touch or below), never require exact float match.
  ticks = int((floor / tick).to_integral_value(rounding=ROUND_FLOOR))
  return (tick * ticks).quantize(tick)


def _thesis_invalidated(
  *,
  option_side: str,
  market_data: MarketDataEngine,
  exit_cfg: dict,
  setup_type: str | None,
) -> bool:
  """True when underlying structure no longer supports the held option side."""
  if not exit_cfg.get("thesis_invalidation_enabled", True):
    return True
  exempt = set(exit_cfg.get("thesis_invalidation_exempt_setups") or [])
  if setup_type in exempt:
    return True
  return vwap_flipped_against(
    option_side=option_side,
    market_data=market_data,
    exit_cfg=exit_cfg,
  )


def vwap_flipped_against(
  *,
  option_side: str,
  market_data: MarketDataEngine,
  exit_cfg: dict,
) -> bool:
  """True when spot has crossed session VWAP against the held option side."""
  if not exit_cfg.get("bias_flip_exit", True):
    return False
  spot = market_data.spot_ltp
  vwap = market_data.session_vwap_value
  if spot is None or vwap is None:
    return False
  buffer = Decimal(str(exit_cfg.get("bias_flip_buffer_points", 0)))
  if option_side == "CE" and spot < (vwap - buffer):
    return True
  if option_side == "PE" and spot > (vwap + buffer):
    return True
  return False


def should_queue_reversal_flip(
  *,
  exit_reason: str,
  option_side: str,
  market_data: MarketDataEngine,
  exit_cfg: dict,
) -> bool:
  """Queue opposite-side entry after stop-out when VWAP confirms reversal."""
  if not exit_cfg.get("flip_on_trend_reversal", True):
    return False
  flip_reasons = set(
    exit_cfg.get("flip_exit_reasons")
    or ["trend_reversal", "adverse_momentum", "early_invalidation"]
  )
  if exit_reason not in flip_reasons:
    return False
  if exit_reason == "trend_reversal":
    return True
  return vwap_flipped_against(
    option_side=option_side,
    market_data=market_data,
    exit_cfg=exit_cfg,
  )


def _trend_reversal_should_exit(
  *,
  option_side: str,
  entry_price: Decimal,
  current_ltp: Decimal,
  mfe_points: Decimal,
  held: float,
  market_data: MarketDataEngine,
  exit_cfg: dict,
) -> bool:
  """VWAP bias flip — only after min hold and when not a small winning scalp."""
  if not exit_cfg.get("bias_flip_exit", True):
    return False

  reversal_min_hold = int(exit_cfg.get("trend_reversal_min_hold_seconds", 180))
  if held < reversal_min_hold:
    return False

  in_profit = current_ltp > entry_price
  if bool(exit_cfg.get("trend_reversal_skip_when_profitable", True)) and in_profit:
    return False

  min_mfe_pct = Decimal(str(exit_cfg.get("trend_reversal_min_mfe_pct", 3))) / Decimal("100")
  if bool(exit_cfg.get("trend_reversal_defer_if_had_mfe", True)):
    if mfe_points >= entry_price * min_mfe_pct:
      return False

  only_underwater = bool(exit_cfg.get("trend_reversal_only_if_underwater", True))
  if only_underwater and in_profit:
    return False

  spot = market_data.spot_ltp
  vwap = market_data.session_vwap_value
  if spot is None or vwap is None:
    return False

  buffer = Decimal(str(exit_cfg.get("bias_flip_buffer_points", 0)))
  if option_side == "CE" and spot < (vwap - buffer):
    return True
  if option_side == "PE" and spot > (vwap + buffer):
    return True
  return False


def premium_structure_broken(
  option_m1: list[Candle] | None,
  *,
  now: datetime,
  exit_cfg: dict,
  held_seconds: float,
  mfe_points: Decimal,
  entry_price: Decimal,
) -> bool:
  """True when the held premium has lost its own VWAP or printed a lower high.

  Winners already past the trail arm are left to the percent trail.
  """
  if not exit_cfg.get("premium_structure_exit_enabled", False):
    return False
  min_hold = int(exit_cfg.get("premium_structure_min_hold_seconds", 45))
  if held_seconds < min_hold or entry_price <= 0:
    return False
  cap = Decimal(str(exit_cfg.get("premium_structure_max_mfe_pct", 2.5))) / Decimal("100")
  if cap > 0 and mfe_points >= entry_price * cap:
    return False
  closed = closed_bars_before(option_m1 or [], now)
  min_bars = int(exit_cfg.get("premium_structure_min_bars", 5))
  if len(closed) < min_bars:
    return False
  last = closed[-1]
  premium_vwap = session_vwap(closed)
  buffer = Decimal(str(exit_cfg.get("premium_structure_vwap_buffer_pct", 0.15)))
  if premium_vwap is not None and premium_vwap > 0:
    floor = premium_vwap * (Decimal("1") - buffer / Decimal("100"))
    if last.close < floor:
      return True
  if len(closed) >= 2:
    prev, cur = closed[-2], closed[-1]
    if cur.high < prev.high and cur.close < prev.close:
      return True
  return False


def evaluate_momentum_exit(
  *,
  option_side: str,
  entry_price: Decimal,
  entry_ts: datetime,
  current_ltp: Decimal,
  mfe_points: Decimal,
  market_data: MarketDataEngine,
  cfg: dict,
  force_exit: bool,
  regime_primary: str | None = None,
  quantity: int = 1,
  is_expiry_day: bool = False,
  confidence: int | None = None,
  setup_type: str | None = None,
  entry_meta: dict | None = None,
  now: datetime | None = None,
  option_m1: list[Candle] | None = None,
) -> ExitDecision:
  if force_exit:
    return ExitDecision(True, "force_exit")

  now = now or datetime.now(tz=timezone.utc)
  if entry_ts.tzinfo is None and now.tzinfo is not None:
    entry_ts = entry_ts.replace(tzinfo=timezone.utc)
  held = (now - entry_ts).total_seconds()

  exit_cfg = dict(cfg)
  if is_expiry_day:
    expiry_overrides = cfg.get("expiry_day_exit") or {}
    exit_cfg.update({k: v for k, v in expiry_overrides.items() if v is not None})

  if entry_price <= 0 or current_ltp <= 0:
    return ExitDecision(False)

  in_profit = current_ltp > entry_price
  high_vol = regime_primary == "high_volatility"
  weak_entry = _entry_quality_weak(entry_meta, exit_cfg)
  adverse_pct = _adverse_stop_pct(
    exit_cfg,
    confidence=confidence,
    high_vol=high_vol,
    weak_entry=weak_entry,
  )

  qty = max(quantity, 1)
  max_loss_inr = Decimal(str(exit_cfg.get("max_loss_inr_per_trade", 0)))
  if max_loss_inr > 0:
    loss_inr = (entry_price - current_ltp) * qty
    if loss_inr >= max_loss_inr:
      return ExitDecision(True, "max_loss_inr")

  # Flat-trade scratch before the full adverse stop: never meaningfully green
  # and already ~0.8% under entry → cut. Does not wait for min_hold.
  if exit_cfg.get("never_green_scratch_enabled", False):
    scratch_hold = int(exit_cfg.get("never_green_scratch_min_hold_seconds", 5))
    scratch_mfe = Decimal(str(exit_cfg.get("never_green_scratch_max_mfe_pct", 0.3))) / Decimal("100")
    scratch_loss = Decimal(str(exit_cfg.get("never_green_scratch_loss_pct", 0.8))) / Decimal("100")
    if held >= scratch_hold:
      never_green = mfe_points < entry_price * scratch_mfe
      underwater = current_ltp <= entry_price * (Decimal("1") - scratch_loss)
      if never_green and underwater:
        return ExitDecision(True, "never_green_scratch")

  if current_ltp <= entry_price * (Decimal("1") - adverse_pct):
    return ExitDecision(True, "adverse_momentum")

  min_hold = int(exit_cfg.get("min_hold_seconds", 20))
  if setup_type == "broker_adopted":
    min_hold = max(
      min_hold,
      int(exit_cfg.get("broker_adopted_min_hold_seconds", 60)),
    )
  if held < min_hold:
    return ExitDecision(False)

  max_hold = int(exit_cfg.get("max_hold_minutes", 0))
  skip_time_in_profit = bool(exit_cfg.get("skip_time_stop_when_profitable", True))
  if max_hold > 0 and held > max_hold * 60:
    stall_cap = exit_cfg.get("time_stop_green_max_mfe_pct")
    stalled_green = False
    if in_profit and stall_cap is not None:
      cap = Decimal(str(stall_cap)) / Decimal("100")
      stalled_green = cap > 0 and mfe_points < entry_price * cap
    if not (skip_time_in_profit and in_profit) or stalled_green:
      return ExitDecision(True, "time_stop")

  structure_broken = premium_structure_broken(
    option_m1,
    now=now,
    exit_cfg=exit_cfg,
    held_seconds=held,
    mfe_points=mfe_points,
    entry_price=entry_price,
  )
  if structure_broken:
    return ExitDecision(True, "premium_structure")

  # Early invalidation: never went meaningfully green → cut at tight SL.
  exempt = set(exit_cfg.get("early_invalidation_exempt_setups") or [])
  if exit_cfg.get("early_invalidation_enabled", True) and setup_type not in exempt:
    early_min_hold = int(exit_cfg.get("early_invalidation_min_hold_seconds", 45))
    mfe_arm_pct = Decimal(str(exit_cfg.get("early_invalidation_max_mfe_pct", 2))) / Decimal("100")
    early_loss_pct = Decimal(str(exit_cfg.get("early_invalidation_loss_pct", 4))) / Decimal("100")
    if held >= early_min_hold:
      never_armed = mfe_points < entry_price * mfe_arm_pct
      underwater = current_ltp <= entry_price * (Decimal("1") - early_loss_pct)
      if never_armed and underwater:
        thesis_ok = _thesis_invalidated(
          option_side=option_side,
          market_data=market_data,
          exit_cfg=exit_cfg,
          setup_type=setup_type,
        )
        if exit_cfg.get("require_thesis_invalidation_for_early_cut", False):
          if not thesis_ok and not structure_broken:
            return ExitDecision(False)
        return ExitDecision(True, "early_invalidation")

  trail_floor = _momentum_trail_floor(entry_price, mfe_points, exit_cfg)
  # Touch or below trail SL → exit (not exact-price match only).
  if trail_floor is not None and current_ltp <= trail_floor:
    reason = (
      "profit_trail"
      if exit_cfg.get("profit_trail_lock_pct") is not None
      else "momentum_trail"
    )
    return ExitDecision(True, reason)

  # VWAP flip — last resort after hard stops; avoid 30s noise exits.
  if _trend_reversal_should_exit(
    option_side=option_side,
    entry_price=entry_price,
    current_ltp=current_ltp,
    mfe_points=mfe_points,
    held=held,
    market_data=market_data,
    exit_cfg=exit_cfg,
  ):
    return ExitDecision(True, "trend_reversal")

  return ExitDecision(False)
