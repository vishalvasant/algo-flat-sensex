"""Test-only quality entry pipeline — lift → greeks → option chart → Nifty veto."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from algoflat.validator.quality_entry_pipeline import (
  PipelineCandidate,
  SpotBar,
  run_quality_pipeline,
  stage_greeks,
)

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 10, 19)
START = datetime(2026, 10, 19, 10, 0, tzinfo=IST)
CFG = {
  "enabled": True,
  "test_only": True,
  "min_abs_delta": 0.30,
  "max_abs_delta": 0.75,
  "enforce_min_abs_delta": False,  # option_lift as-is
  "max_daily_theta_pct_of_premium": 12.0,
  "min_premium": 5.0,
  "vwap_buffer_pct": 0.50,
  "require_premium_above_vwap": True,
  "level_near_pts": 30.0,
  "swing_lookback_5m": 12,
  "mid_allow_continuation": True,
}
PDH, PDL = 23149.85, 22913.80


def _make_mtf(bars5: list[SpotBar]) -> list[SpotBar]:
  bars15: list[SpotBar] = []
  for i in range(0, len(bars5), 3):
    chunk = bars5[i : i + 3]
    if len(chunk) < 3:
      break
    bars15.append(
      SpotBar(
        chunk[0].ts,
        chunk[0].o,
        max(b.h for b in chunk),
        min(b.l for b in chunk),
        chunk[-1].c,
      )
    )
  return bars15


def _bars5_bull_into_support() -> tuple[list[SpotBar], list[SpotBar]]:
  """Build 5m/15m so regime=bull and price sits on swing support."""
  bars5: list[SpotBar] = []
  px = 23050.0
  ts = START
  # Selloff, then two bullish 15m windows (6 green 5m) so last 15m > prior
  for _ in range(15):
    o, c = px, px - 8
    bars5.append(SpotBar(ts, o, max(o, c) + 2, min(o, c) - 2, c))
    px = c
    ts += timedelta(minutes=5)
  for _ in range(6):
    o, c = px, px + 12
    bars5.append(SpotBar(ts, o, c + 2, o - 1, c))
    px = c
    ts += timedelta(minutes=5)
  return bars5, _make_mtf(bars5)


def _bars5_bear_into_resistance() -> tuple[list[SpotBar], list[SpotBar]]:
  """Rally to a high, then shallow rejection so price stays in resistance zone."""
  bars5: list[SpotBar] = []
  px = 22980.0
  ts = START
  for _ in range(12):
    o, c = px, px + 10
    bars5.append(SpotBar(ts, o, c + 2, o - 1, c))
    px = c
    ts += timedelta(minutes=5)
  # two bearish 15m windows but only -4 pts/bar → still near swing high
  for _ in range(6):
    o, c = px, px - 4
    bars5.append(SpotBar(ts, o, o + 1, c - 1, c))
    px = c
    ts += timedelta(minutes=5)
  return bars5, _make_mtf(bars5)


def test_disabled_by_default():
  cand = PipelineCandidate(
    ts=START + timedelta(hours=2),
    side="CE",
    premium=40.0,
    strike=22950,
    spot=22940,
    expiry=DAY,
    option_vwap=38.0,
    delta=0.45,
    theta=-0.4,
  )
  r = run_quality_pipeline(cand, bars5=[], bars15=[], cfg={"enabled": False}, pdh=PDH, pdl=PDL)
  assert r.accepted is False
  assert "quality_entry_pipeline_disabled" in r.reasons


def test_greeks_low_delta_advisory_when_min_not_enforced():
  cand = PipelineCandidate(
    ts=START,
    side="CE",
    premium=12.0,
    strike=23200,
    spot=22900,
    expiry=DAY,
    delta=0.12,
    theta=-0.3,
  )
  g = stage_greeks(cand, CFG)
  assert g.passed is True
  assert g.detail.get("delta_below_band_advisory") is True


def test_greeks_reject_low_delta_when_enforced():
  cand = PipelineCandidate(
    ts=START,
    side="CE",
    premium=12.0,
    strike=23200,
    spot=22900,
    expiry=DAY,
    delta=0.12,
    theta=-0.3,
  )
  g = stage_greeks(cand, {**CFG, "enforce_min_abs_delta": True})
  assert g.passed is False
  assert "delta_too_low_no_move_potential" in g.reasons


def test_greeks_reject_high_theta_decay():
  cand = PipelineCandidate(
    ts=START,
    side="PE",
    premium=20.0,
    strike=22950,
    spot=22930,
    expiry=DAY,
    delta=0.45,
    theta=-5.0,  # already daily-scale → 25% of premium
  )
  g = stage_greeks(cand, CFG)
  assert g.passed is False
  assert "theta_decay_too_high" in g.reasons


def test_pipeline_accepts_pe_at_resistance_when_bear():
  bars5, bars15 = _bars5_bear_into_resistance()
  t = bars5[-1].ts + timedelta(minutes=5)
  spot = bars5[-1].c
  cand = PipelineCandidate(
    ts=t,
    side="PE",
    premium=55.0,
    strike=round(spot / 50) * 50,
    spot=spot,
    expiry=DAY,
    option_vwap=50.0,
    delta=-0.48,
    theta=-0.40,
  )
  r = run_quality_pipeline(cand, bars5=bars5, bars15=bars15, cfg=CFG, pdh=PDH, pdl=PDL)
  assert r.accepted is True, (r.reject_stage, r.reasons, [s.detail for s in r.stages])
  assert r.entry_ltp == 55.0


def test_pipeline_nifty_vetoes_ce_when_bear_at_resistance():
  bars5, bars15 = _bars5_bear_into_resistance()
  t = bars5[-1].ts + timedelta(minutes=5)
  spot = bars5[-1].c
  cand = PipelineCandidate(
    ts=t,
    side="CE",
    premium=42.0,
    strike=round(spot / 50) * 50,
    spot=spot,
    expiry=DAY,
    option_vwap=40.0,
    delta=0.48,
    theta=-0.35,
  )
  r = run_quality_pipeline(cand, bars5=bars5, bars15=bars15, cfg=CFG, pdh=PDH, pdl=PDL)
  assert r.accepted is False
  assert r.reject_stage == "nifty_chart"


def _bars5_bull_into_resistance() -> tuple[list[SpotBar], list[SpotBar]]:
  """Uptrend pressing swing high — regime=bull, zone=resistance (breakout setup)."""
  bars5: list[SpotBar] = []
  px = 22980.0
  ts = START
  for _ in range(18):
    o, c = px, px + 10
    bars5.append(SpotBar(ts, o, c + 2, o - 1, c))
    px = c
    ts += timedelta(minutes=5)
  return bars5, _make_mtf(bars5)


def test_pipeline_accepts_ce_breakout_at_resistance_when_bull():
  bars5, bars15 = _bars5_bull_into_resistance()
  t = bars5[-1].ts + timedelta(minutes=5)
  spot = bars5[-1].c
  cand = PipelineCandidate(
    ts=t,
    side="CE",
    premium=55.0,
    strike=round(spot / 50) * 50,
    spot=spot,
    expiry=DAY,
    option_vwap=50.0,
    delta=0.48,
    theta=-0.35,
  )
  r = run_quality_pipeline(cand, bars5=bars5, bars15=bars15, cfg=CFG, pdh=PDH, pdl=PDL)
  assert r.accepted is True, (r.reject_stage, r.reasons, [s.detail for s in r.stages])


def test_option_chart_rejects_below_vwap():
  bars5, bars15 = _bars5_bull_into_support()
  t = bars5[-1].ts + timedelta(minutes=5)
  spot = bars5[-1].c
  cand = PipelineCandidate(
    ts=t,
    side="CE",
    premium=30.0,
    strike=round(spot / 50) * 50,
    spot=spot,
    expiry=DAY,
    option_vwap=40.0,  # premium well below VWAP
    delta=0.48,
    theta=-0.35,
  )
  r = run_quality_pipeline(cand, bars5=bars5, bars15=bars15, cfg=CFG, pdh=PDH, pdl=PDL)
  assert r.accepted is False
  assert r.reject_stage == "option_chart"
  assert "option_premium_below_vwap" in r.reasons
