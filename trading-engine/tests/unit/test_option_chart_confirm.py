"""Unit tests for CE/PE premium chart confirmation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from algoflat.models.events import Bias, CandidateSignal, Candle, CandleInterval, FeatureSnapshot
from algoflat.validator.option_chart_confirm import (
  analyze_option_chart,
  option_chart_confirm_reasons,
)

IST = timezone(timedelta(hours=5, minutes=30))


def _bar(ts_min: int, o: float, h: float, l: float, c: float, token: str = "opt") -> Candle:
  base = datetime(2026, 7, 22, 9, 15, tzinfo=IST).astimezone(timezone.utc)
  return Candle(
    instrument_token=token,
    ts=base + timedelta(minutes=ts_min),
    open=Decimal(str(o)),
    high=Decimal(str(h)),
    low=Decimal(str(l)),
    close=Decimal(str(c)),
    volume=1000,
    interval=CandleInterval.M1,
  )


def _signal(side: str = "CE", setup: str = "vwap_reclaim") -> CandidateSignal:
  feat = FeatureSnapshot(
    ts=datetime(2026, 7, 22, 10, 30, tzinfo=IST).astimezone(timezone.utc),
    nifty_spot=Decimal("25000"),
    bias_5m=Bias.BULLISH if side == "CE" else Bias.BEARISH,
  )
  return CandidateSignal(
    ts=feat.ts,
    setup_type=setup,
    side=side,
    instrument_token="opt",
    tsym="NIFTY24JUL25000CE",
    strategy_version="test",
    feature_snapshot=feat,
    confidence=70,
  )


def _cfg() -> dict:
  return {
    "enabled": True,
    "allow_if_no_bars": False,
    "min_closed_m1_bars": 8,
    "require_above_premium_vwap": True,
    "require_m1_momentum": True,
    "require_m3_aligned": True,
    "require_m5_aligned": True,
    "require_higher_low_structure": True,
    "block_premium_decay": True,
    "block_spot_premium_divergence": True,
    "min_alignment_score": 0,
  }


def test_bullish_ce_passes_rising_premium_chart() -> None:
  bars = [_bar(i, 100 + i, 101 + i, 99 + i, 100 + i) for i in range(12)]
  spot = [_bar(i, 25000 + i * 2, 25002 + i * 2, 24998 + i * 2, 25000 + i * 2, "spot") for i in range(12)]
  when = bars[-1].ts + timedelta(minutes=1)
  reasons = option_chart_confirm_reasons(
    _signal("CE"),
    bars,
    cfg=_cfg(),
    now=when,
    spot_m1=spot,
  )
  assert reasons == []


def test_rising_pe_premium_passes_for_long_put() -> None:
  """Live bug: PE above VWAP / rising was rejected as option_premium_above_vwap."""
  bars = [_bar(i, 100 + i, 101 + i, 99 + i, 100 + i) for i in range(12)]
  spot = [_bar(i, 25000 - i * 2, 25002 - i * 2, 24998 - i * 2, 25000 - i * 2, "spot") for i in range(12)]
  when = bars[-1].ts + timedelta(minutes=1)
  reasons = option_chart_confirm_reasons(
    _signal("PE", setup="opening_range_breakout"),
    bars,
    cfg=_cfg(),
    now=when,
    spot_m1=spot,
  )
  assert "option_premium_above_vwap" not in reasons
  assert reasons == []


def test_falling_pe_premium_rejected() -> None:
  bars = [_bar(i, 120 - i, 121 - i, 118 - i, 120 - i) for i in range(12)]
  when = bars[-1].ts + timedelta(minutes=1)
  reasons = option_chart_confirm_reasons(
    _signal("PE", setup="vwap_trend"),
    bars,
    cfg=_cfg(),
    now=when,
  )
  assert "option_premium_decaying" in reasons or "option_m1_momentum_weak" in reasons or "option_premium_below_vwap" in reasons


def test_ce_rejects_decaying_premium() -> None:
  bars = [_bar(i, 120 - i, 121 - i, 118 - i, 120 - i) for i in range(12)]
  when = bars[-1].ts + timedelta(minutes=1)
  reasons = option_chart_confirm_reasons(
    _signal("CE"),
    bars,
    cfg=_cfg(),
    now=when,
  )
  assert "option_premium_decaying" in reasons or "option_m1_momentum_weak" in reasons


def test_trend_day_requires_stricter_momentum() -> None:
  bars = [_bar(i, 100 + i * 0.1, 101 + i * 0.1, 99 + i * 0.1, 100 + i * 0.1) for i in range(12)]
  when = bars[-1].ts + timedelta(minutes=1)
  cfg = _cfg()
  cfg["setup_overrides"] = {"trend_day": {"min_m1_momentum_pct": 0.35}}
  reasons = option_chart_confirm_reasons(
    _signal("CE", setup="trend_day"),
    bars,
    cfg=cfg,
    now=when,
  )
  assert "option_m1_momentum_weak" in reasons


def test_spot_premium_divergence_blocks_ce() -> None:
  opt = [_bar(i, 100, 101, 99, 100) for i in range(12)]
  spot = [_bar(i, 25000 + i * 3, 25003 + i * 3, 24997 + i * 3, 25000 + i * 3, "spot") for i in range(12)]
  when = opt[-1].ts + timedelta(minutes=1)
  reasons = option_chart_confirm_reasons(
    _signal("CE"),
    opt,
    cfg=_cfg(),
    now=when,
    spot_m1=spot,
  )
  assert "option_spot_premium_divergence" in reasons


def test_analyze_option_chart_scores_aligned_premium() -> None:
  bars = [_bar(i, 100 + i, 101 + i, 99 + i, 100 + i) for i in range(12)]
  when = bars[-1].ts + timedelta(minutes=1)
  out = analyze_option_chart(side="CE", option_m1=bars, when=when, cfg=_cfg())
  assert out["score"] >= 55
