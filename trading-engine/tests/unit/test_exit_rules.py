"""Unit tests for breakeven + stepped momentum trail exits."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from algoflat.position.exit_rules import (
  _adverse_stop_pct,
  _momentum_trail_floor,
  evaluate_momentum_exit,
)


def _cfg(**overrides):
  cfg = {
    "min_hold_seconds": 20,
    "max_hold_minutes": 25,
    "skip_time_stop_when_profitable": True,
    "bias_flip_exit": True,
    "bias_flip_buffer_points": 8,
    "adverse_move_pct_from_entry": 4,
    "adverse_move_min_pct": 3,
    "confidence_adverse_scaling": True,
    "adverse_base_confidence": 76,
    "adverse_max_confidence": 100,
    "trail_step_arm_mfe_pct": 4,
    "trail_step_buffer_pct": 2,
    "early_invalidation_enabled": True,
    "early_invalidation_min_hold_seconds": 45,
    "early_invalidation_max_mfe_pct": 2,
    "early_invalidation_loss_pct": 4,
  }
  cfg.update(overrides)
  return cfg


def _md_bull():
  md = MagicMock()
  md.spot_ltp = Decimal("24200")
  md.session_vwap_value = Decimal("24150")
  return md


def test_adverse_stop_scales_with_confidence():
  cfg = _cfg(confidence_adverse_scaling=True)
  tight = _adverse_stop_pct(cfg, confidence=76, high_vol=False)
  wide = _adverse_stop_pct(cfg, confidence=100, high_vol=False, weak_entry=False)
  assert tight == Decimal("0.03")
  assert wide == Decimal("0.04")


def test_adverse_stop_weak_entry_tighter():
  cfg = _cfg()
  weak = _adverse_stop_pct(cfg, confidence=100, high_vol=False, weak_entry=True)
  assert weak == Decimal("0.025")


def test_trail_floor_none_until_breakeven_arm():
  assert _momentum_trail_floor(Decimal("100"), Decimal("1"), _cfg()) is None


def test_trail_floor_breakeven_when_green_under_4pct():
  floor = _momentum_trail_floor(Decimal("100"), Decimal("3"), _cfg())
  assert floor == Decimal("100")


def test_trail_floor_steps_after_4pct_mfe():
  # Peak +6% → lock +4% → floor 104
  floor = _momentum_trail_floor(Decimal("100"), Decimal("6"), _cfg())
  assert floor == Decimal("104")


def test_trail_floor_at_10pct_mfe():
  floor = _momentum_trail_floor(Decimal("100"), Decimal("10"), _cfg())
  assert floor == Decimal("108")


def test_profit_trail_lock_55pct():
  # Peak +10 pts → lock 55% → floor 105.50
  floor = _momentum_trail_floor(
    Decimal("100"), Decimal("10"), _cfg(profit_trail_lock_pct=0.55)
  )
  assert floor == Decimal("105.50")


def test_profit_trail_waits_for_arm_mfe():
  # +1% MFE with 1.5% arm → trail not armed yet (let runner develop).
  assert (
    _momentum_trail_floor(
      Decimal("100"),
      Decimal("1"),
      _cfg(profit_trail_lock_pct=0.55, profit_trail_arm_mfe_pct=1.5),
    )
    is None
  )
  floor = _momentum_trail_floor(
    Decimal("100"),
    Decimal("3"),
    _cfg(profit_trail_lock_pct=0.55, profit_trail_arm_mfe_pct=1.5),
  )
  assert floor == Decimal("101.65")


def test_profit_trail_exit_reason():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("105"),
    mfe_points=Decimal("10"),
    market_data=_md_bull(),
    cfg=_cfg(
      profit_trail_lock_pct=0.55,
      profit_trail_arm_mfe_pct=1.5,
      adverse_move_pct_from_entry=5,
      confidence_adverse_scaling=False,
      early_invalidation_enabled=False,
      bias_flip_exit=False,
      max_hold_minutes=0,
    ),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "profit_trail"


def test_green_runner_not_time_stopped():
  """In profit before session end — no forced exit; only trail/SL/15:15."""
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(hours=3)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("112"),
    mfe_points=Decimal("15"),
    market_data=_md_bull(),
    cfg=_cfg(
      profit_trail_lock_pct=0.55,
      profit_trail_arm_mfe_pct=1.5,
      adverse_move_pct_from_entry=5,
      confidence_adverse_scaling=False,
      early_invalidation_enabled=False,
      bias_flip_exit=False,
      max_hold_minutes=0,
      never_green_scratch_enabled=False,
    ),
    force_exit=False,
  )
  # Still above trail floor (100 + 15*0.55 = 108.25) → keep riding.
  assert not decision.should_exit


def test_early_invalidation_cuts_never_green_loser():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("96"),
    mfe_points=Decimal("1"),
    market_data=_md_bull(),
    cfg=_cfg(
      adverse_move_pct_from_entry=5,
      confidence_adverse_scaling=False,
      early_invalidation_loss_pct=4,
    ),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "early_invalidation"


def test_early_invalidation_skipped_for_exempt_orb():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("95.5"),
    mfe_points=Decimal("1"),
    market_data=_md_bull(),
    cfg=_cfg(early_invalidation_exempt_setups=["opening_range_breakout"]),
    force_exit=False,
    setup_type="opening_range_breakout",
  )
  assert decision.should_exit
  assert decision.reason == "adverse_momentum"


def test_adverse_stops_at_5pct_default():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("94.5"),
    mfe_points=Decimal("3"),
    market_data=_md_bull(),
    cfg=_cfg(confidence_adverse_scaling=False, adverse_move_pct_from_entry=5),
    force_exit=False,
    confidence=95,
    entry_meta={"greeks_mult": 1.0},
  )
  assert decision.should_exit
  assert decision.reason == "adverse_momentum"


def test_adverse_stop_bypasses_min_hold():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=5)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("94"),
    mfe_points=Decimal("0"),
    market_data=_md_bull(),
    cfg=_cfg(min_hold_seconds=20, confidence_adverse_scaling=False, adverse_move_pct_from_entry=5),
    force_exit=False,
    confidence=95,
    entry_meta={"greeks_mult": 1.0},
  )
  assert decision.should_exit
  assert decision.reason == "adverse_momentum"


def test_broker_adopted_blocks_soft_exit_during_grace():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=30)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("99.5"),
    mfe_points=Decimal("2"),
    market_data=_md_bull(),
    cfg=_cfg(min_hold_seconds=15, broker_adopted_min_hold_seconds=60),
    force_exit=False,
    setup_type="broker_adopted",
  )
  assert not decision.should_exit


def test_breakeven_trail_exits_after_pullback_from_small_win():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("99.8"),
    mfe_points=Decimal("2"),
    market_data=_md_bull(),
    cfg=_cfg(),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "momentum_trail"


def test_no_trail_exit_while_still_above_breakeven():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("100.5"),
    mfe_points=Decimal("2"),
    market_data=_md_bull(),
    cfg=_cfg(),
    force_exit=False,
  )
  assert not decision.should_exit


def test_trail_exits_after_6pct_mfe_pullback_to_104():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("103.9"),
    mfe_points=Decimal("6"),
    market_data=_md_bull(),
    cfg=_cfg(),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "momentum_trail"


def test_trail_holds_runner_above_step_floor():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("106.5"),
    mfe_points=Decimal("8"),
    market_data=_md_bull(),
    cfg=_cfg(),
    force_exit=False,
  )
  assert not decision.should_exit


def test_trend_reversal_blocked_early_noise():
  md = MagicMock()
  md.spot_ltp = Decimal("24100")
  md.session_vwap_value = Decimal("24150")
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=40)

  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("99"),
    mfe_points=Decimal("0"),
    market_data=md,
    cfg=_cfg(trend_reversal_min_hold_seconds=180),
    force_exit=False,
  )
  assert not decision.should_exit


def test_trend_reversal_exits_underwater_after_min_hold():
  md = MagicMock()
  md.spot_ltp = Decimal("24100")
  md.session_vwap_value = Decimal("24150")
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=200)

  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("99"),
    mfe_points=Decimal("0"),
    market_data=md,
    cfg=_cfg(trend_reversal_min_hold_seconds=180),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "trend_reversal"


def test_trend_reversal_skipped_when_profitable():
  md = MagicMock()
  md.spot_ltp = Decimal("24100")
  md.session_vwap_value = Decimal("24150")
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=200)

  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("102"),
    mfe_points=Decimal("3"),
    market_data=md,
    cfg=_cfg(trend_reversal_min_hold_seconds=180),
    force_exit=False,
  )
  assert decision.reason != "trend_reversal"


def test_partial_runner_uses_the_same_trail_as_the_screen():
  """A booked partial must not drop the stop below the trail shown in the UI."""
  entry = Decimal("126.65")
  mfe = Decimal("16.75")
  cfg = _cfg(
    trail_breakeven_arm_mfe_pct=1.5,
    trail_step_arm_mfe_pct=2.5,
    trail_step_buffer_pct=1.5,
    trail_step_buffer_pct_after_partial=3,
    min_hold_seconds=15,
  )
  floor = _momentum_trail_floor(entry, mfe, cfg)
  assert floor == Decimal("141.50")
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=120)
  decision = evaluate_momentum_exit(
    option_side="PE",
    entry_price=entry,
    entry_ts=entry_ts,
    current_ltp=Decimal("141.45"),
    mfe_points=mfe,
    market_data=_md_bull(),
    cfg=cfg,
    force_exit=False,
    entry_meta={"partial_booked": True},
  )
  assert decision.should_exit
  assert decision.reason == "momentum_trail"


def test_trail_exits_on_exact_floor_touch():
  """LTP == trail floor must exit (touch), not only strict below."""
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("104"),  # exact step floor after +6% MFE
    mfe_points=Decimal("6"),
    market_data=_md_bull(),
    cfg=_cfg(),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "momentum_trail"


def test_trail_exits_when_ltp_below_floor():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
  decision = evaluate_momentum_exit(
    option_side="PE",
    entry_price=Decimal("64.50"),
    entry_ts=entry_ts,
    current_ltp=Decimal("65.50"),
    mfe_points=Decimal("2.56"),  # ~4% MFE → stepped floor ~66.05 snapped
    market_data=_md_bull(),
    cfg=_cfg(
      trail_breakeven_arm_mfe_pct=1.5,
      trail_step_arm_mfe_pct=2.5,
      trail_step_buffer_pct=1.5,
    ),
    force_exit=False,
  )
  floor = _momentum_trail_floor(
    Decimal("64.50"),
    Decimal("2.56"),
    _cfg(
      trail_breakeven_arm_mfe_pct=1.5,
      trail_step_arm_mfe_pct=2.5,
      trail_step_buffer_pct=1.5,
    ),
  )
  assert floor is not None
  assert Decimal("65.50") <= floor
  assert decision.should_exit
  assert decision.reason == "momentum_trail"


def test_premium_structure_exits_when_chart_breaks():
  from algoflat.models.events import Candle, CandleInterval

  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=90)
  now = datetime.now(tz=timezone.utc)
  bars = []
  px = Decimal("100")
  for i in range(6):
    ts = now - timedelta(minutes=6 - i)
    close = px - Decimal(i)
    bars.append(
      Candle(
        ts=ts,
        instrument_token="opt",
        interval=CandleInterval.M1,
        open=close + Decimal("1"),
        high=close + Decimal("1"),
        low=close - Decimal("1"),
        close=close,
        volume=1,
      )
    )
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("98"),
    mfe_points=Decimal("0.5"),
    market_data=_md_bull(),
    cfg=_cfg(premium_structure_exit_enabled=True, min_hold_seconds=15),
    force_exit=False,
    now=now,
    option_m1=bars,
  )
  assert decision.should_exit
  assert decision.reason == "premium_structure"


def test_no_time_stop_when_in_profit():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(minutes=40)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("103"),
    mfe_points=Decimal("3"),
    market_data=_md_bull(),
    cfg=_cfg(max_hold_minutes=25),
    force_exit=False,
  )
  assert not decision.should_exit


def test_time_stop_when_underwater():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(minutes=40)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("99"),
    mfe_points=Decimal("0.5"),
    market_data=_md_bull(),
    cfg=_cfg(max_hold_minutes=25),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "time_stop"
