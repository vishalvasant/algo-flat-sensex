"""CE/PE chart confirmation — premium 1m/3m/5m structure before entry.

Both CE and PE entries are *long premium*. A strong PE buy needs rising put
premium (above VWAP, green M3/M5), not a falling put chart. Spot direction
chooses the side; the option chart must confirm the chosen premium is alive.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from algoflat.features.indicators import aggregate_from_m1
from algoflat.market_data.vwap import session_vwap
from algoflat.models.events import Candle, CandidateSignal


def closed_bars_before(bars: list[Candle], when: datetime) -> list[Candle]:
  """Return 1m bars fully closed at or before ``when`` (honest replay)."""
  if not bars:
    return []
  out: list[Candle] = []
  for bar in sorted(bars, key=lambda c: c.ts):
    bar_end = bar.ts if bar.ts.tzinfo else bar.ts.replace(tzinfo=when.tzinfo)
    bar_end = bar_end + timedelta(minutes=1)
    if bar_end <= when:
      out.append(bar)
  return out


def _pct_change(old: Decimal, new: Decimal) -> float:
  if old <= 0:
    return 0.0
  return float((new - old) / old * Decimal("100"))


def _setup_cfg(cfg: dict, setup_type: str) -> dict:
  """Merge global option-chart config with per-setup overrides."""
  overrides = (cfg.get("setup_overrides") or {}).get(setup_type) or {}
  merged = dict(cfg)
  merged.update(overrides)
  return merged


def analyze_option_chart(
  *,
  side: str,
  option_m1: list[Candle] | None,
  spot_m1: list[Candle] | None = None,
  when: datetime,
  cfg: dict,
) -> dict[str, Any]:
  """Score premium chart alignment — used for logging and veto reasons."""
  scfg = cfg
  closed = closed_bars_before(option_m1 or [], when)
  spot_closed = closed_bars_before(spot_m1 or [], when)
  out: dict[str, Any] = {
    "bars_closed": len(closed),
    "spot_bars_closed": len(spot_closed),
    "premium_vwap": None,
    "m1_momentum_pct": None,
    "m3_aligned": None,
    "m5_aligned": None,
    "higher_low": None,
    "spot_premium_divergence": None,
    "decay_pct": None,
    "score": 0,
  }
  min_bars = int(scfg.get("min_closed_m1_bars", 8))
  if len(closed) < min_bars:
    out["score"] = 0
    return out

  last = closed[-1]
  premium_vwap = session_vwap(closed)
  out["premium_vwap"] = float(premium_vwap) if premium_vwap is not None else None
  score = 0

  # Long CE and long PE both need premium strength (above VWAP / rising).
  buffer_pct = Decimal(str(scfg.get("vwap_buffer_pct", 0.15)))
  if premium_vwap is not None:
    floor = premium_vwap * (Decimal("1") - buffer_pct / Decimal("100"))
    if last.close >= floor:
      score += 25

  lookback = max(2, int(scfg.get("m1_momentum_lookback", 3)))
  if len(closed) >= lookback:
    move = _pct_change(closed[-lookback].close, last.close)
    out["m1_momentum_pct"] = move
    min_move = float(scfg.get("min_m1_momentum_pct", 0.25))
    if move >= min_move:
      score += 20

  m3 = aggregate_from_m1(closed, 3)
  if len(m3) >= int(scfg.get("min_m3_bars", 3)):
    m3_bar = m3[-1]
    aligned = m3_bar.close > m3_bar.open
    out["m3_aligned"] = aligned
    if aligned:
      score += 15

  m5 = aggregate_from_m1(closed, 5)
  if len(m5) >= int(scfg.get("min_m5_bars", 2)):
    m5_bar = m5[-1]
    aligned = m5_bar.close > m5_bar.open
    out["m5_aligned"] = aligned
    if aligned:
      score += 15
    if len(m5) >= 2 and scfg.get("require_higher_low_structure", True):
      prev, cur = m5[-2], m5[-1]
      hl = cur.low >= prev.low and cur.close >= prev.close
      out["higher_low"] = hl
      if hl:
        score += 10

  decay_lb = max(3, int(scfg.get("decay_lookback_bars", 5)))
  if len(closed) >= decay_lb:
    decay_pct = _pct_change(closed[-decay_lb].close, last.close)
    out["decay_pct"] = decay_pct
    max_decay = float(scfg.get("max_premium_decay_pct", 1.2))
    # Not decaying: premium held or rose (same for long CE/PE).
    if decay_pct >= -max_decay:
      score += 10

  div_lb = max(3, int(scfg.get("divergence_lookback_bars", 5)))
  if (
    scfg.get("block_spot_premium_divergence", True)
    and len(closed) >= div_lb
    and len(spot_closed) >= div_lb
  ):
    opt_chg = _pct_change(closed[-div_lb].close, last.close)
    spot_chg = _pct_change(spot_closed[-div_lb].close, spot_closed[-1].close)
    min_spot = float(scfg.get("min_spot_move_pct_for_divergence", 0.04))
    max_opt = float(scfg.get("max_premium_flat_pct", 0.15))
    # Spot moved our way but premium stayed flat/dead.
    diverged = (
      (side == "CE" and spot_chg >= min_spot and opt_chg <= max_opt)
      or (side == "PE" and spot_chg <= -min_spot and opt_chg <= max_opt)
    )
    out["spot_premium_divergence"] = diverged
    if not diverged:
      score += 15

  out["score"] = min(100, score)
  return out


def option_chart_confirm_reasons(
  signal: CandidateSignal,
  option_m1: list[Candle] | None,
  *,
  cfg: dict,
  now: datetime | None = None,
  spot_m1: list[Candle] | None = None,
) -> list[str]:
  """Veto when CE/PE premium chart disagrees with the spot-led setup."""
  if not cfg.get("enabled", False):
    return []

  exempt = set(cfg.get("exempt_setups") or ["trend_reversal_flip"])
  if signal.setup_type in exempt:
    return []

  apply_setups = set(
    cfg.get("apply_setups")
    or [
      "vwap_trend",
      "vwap_reclaim",
      "vwap_pullback",
      "momentum_continuation",
      "opening_range_breakout",
      "gap_and_go",
      "gap_fade",
      "peak_reversal_fade",
      "level_rejection",
      "pdh_pdl_break",
      "delta_momentum",
      "trend_day",
      "expiry_scalping",
    ]
  )
  if signal.setup_type not in apply_setups:
    return []

  when = now or signal.ts
  scfg = _setup_cfg(cfg, signal.setup_type)

  if not option_m1:
    policy = str(scfg.get("unavailable_policy", "reject_high_risk"))
    high_risk = signal.setup_type in set(
      scfg.get("high_risk_setups")
      or [
        "momentum_continuation",
        "vwap_trend",
        "trend_day",
        "gap_and_go",
        "opening_range_breakout",
        "delta_momentum",
        "expiry_scalping",
      ]
    )
    if policy == "reject_all":
      return ["option_confirmation_unavailable"]
    if policy == "reject_high_risk" and high_risk:
      return ["option_confirmation_unavailable"]
    if scfg.get("allow_if_no_bars", False):
      return []
    return ["option_confirmation_unavailable"]

  closed = closed_bars_before(option_m1, when)
  min_bars = int(scfg.get("min_closed_m1_bars", 8))
  if len(closed) < min_bars:
    policy = str(scfg.get("unavailable_policy", "reject_high_risk"))
    high_risk = signal.setup_type in set(
      scfg.get("high_risk_setups")
      or [
        "momentum_continuation",
        "vwap_trend",
        "trend_day",
        "gap_and_go",
        "opening_range_breakout",
        "delta_momentum",
        "expiry_scalping",
      ]
    )
    if policy == "reject_all":
      return ["option_confirmation_unavailable"]
    if policy == "reject_high_risk" and high_risk:
      return ["option_confirmation_unavailable"]
    if scfg.get("allow_if_no_bars", False):
      return []
    return ["option_chart_insufficient_bars"]

  reasons: list[str] = []
  # One of VWAP / 1m / 3m can miss while the chart is still alive. Two misses block.
  soft: list[str] = []
  side = signal.side
  last = closed[-1]
  premium_vwap = session_vwap(closed)
  buffer_pct = Decimal(str(scfg.get("vwap_buffer_pct", 0.15)))

  # Long premium (CE or PE): reject weak/below-VWAP option charts.
  if scfg.get("require_above_premium_vwap", True) and premium_vwap is not None:
    floor = premium_vwap * (Decimal("1") - buffer_pct / Decimal("100"))
    if last.close < floor:
      soft.append("option_premium_below_vwap")

  lookback = max(2, int(scfg.get("m1_momentum_lookback", 3)))
  if scfg.get("require_m1_momentum", True) and len(closed) >= lookback:
    anchor = closed[-lookback].close
    move = _pct_change(anchor, last.close)
    min_move = float(scfg.get("min_m1_momentum_pct", 0.25))
    if move < min_move:
      soft.append("option_m1_momentum_weak")

  if scfg.get("require_m3_aligned", True):
    m3 = aggregate_from_m1(closed, 3)
    min_m3 = int(scfg.get("min_m3_bars", 3))
    if len(m3) < min_m3:
      soft.append("option_m3_insufficient")
    else:
      m3_bar = m3[-1]
      if m3_bar.close <= m3_bar.open:
        soft.append("option_m3_not_bullish")

  if scfg.get("require_m5_aligned", True):
    m5 = aggregate_from_m1(closed, 5)
    min_m5 = int(scfg.get("min_m5_bars", 2))
    if len(m5) < min_m5:
      reasons.append("option_m5_insufficient")
    else:
      m5_bar = m5[-1]
      if m5_bar.close <= m5_bar.open:
        reasons.append("option_m5_not_bullish")
      if scfg.get("require_higher_low_structure", True) and len(m5) >= 2:
        prev, cur = m5[-2], m5[-1]
        if cur.low < prev.low or cur.close < prev.close:
          reasons.append("option_m5_no_higher_low")

  decay_lb = max(3, int(scfg.get("decay_lookback_bars", 5)))
  if scfg.get("block_premium_decay", True) and len(closed) >= decay_lb:
    old = closed[-decay_lb].close
    decay_pct = _pct_change(old, last.close)
    max_decay = float(scfg.get("max_premium_decay_pct", 1.2))
    if decay_pct < -max_decay:
      reasons.append("option_premium_decaying")

  spot_closed = closed_bars_before(spot_m1 or [], when)
  div_lb = max(3, int(scfg.get("divergence_lookback_bars", 5)))
  if (
    scfg.get("block_spot_premium_divergence", True)
    and len(closed) >= div_lb
    and len(spot_closed) >= div_lb
  ):
    opt_chg = _pct_change(closed[-div_lb].close, last.close)
    spot_chg = _pct_change(spot_closed[-div_lb].close, spot_closed[-1].close)
    min_spot = float(scfg.get("min_spot_move_pct_for_divergence", 0.04))
    max_opt = float(scfg.get("max_premium_flat_pct", 0.15))
    if side == "CE" and spot_chg >= min_spot and opt_chg <= max_opt:
      reasons.append("option_spot_premium_divergence")
    elif side == "PE" and spot_chg <= -min_spot and opt_chg <= max_opt:
      reasons.append("option_spot_premium_divergence")

  min_score = int(scfg.get("min_alignment_score", 0))
  if len(soft) >= 2:
    reasons.extend(soft)
  elif len(soft) == 1 and min_score > 0:
    analysis = analyze_option_chart(
      side=side,
      option_m1=option_m1,
      spot_m1=spot_m1,
      when=when,
      cfg=scfg,
    )
    if int(analysis.get("score", 0)) < min_score:
      reasons.extend(soft)
  elif soft:
    reasons.extend(soft)

  if min_score > 0 and not reasons:
    analysis = analyze_option_chart(
      side=side,
      option_m1=option_m1,
      spot_m1=spot_m1,
      when=when,
      cfg=scfg,
    )
    if int(analysis.get("score", 0)) < min_score:
      reasons.append("option_chart_score_low")

  return reasons
