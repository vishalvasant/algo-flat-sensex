"""FUT confirmation — dual-chart engine after spot-mapped levels/strikes.

Spot anchors ITM/ATM/OTM + levels; futures confirm direction via VWAP side,
optional VWAP sustain bars, and (for breakouts) relative volume.
"""
from __future__ import annotations

from algoflat.models.events import CandidateSignal


def fut_momentum_rejection_reasons(
  signal: CandidateSignal,
  *,
  cfg: dict,
) -> list[str]:
  """Block when NIFTY FUT does not confirm the option side (VWAP / volume)."""
  if not cfg.get("enabled", True):
    return []

  exempt = set(
    cfg.get("exempt_setups")
    or [
      "peak_reversal_fade",
      "level_rejection",
      "gap_fade",
      "trend_reversal_flip",
      "liquidity_sweep",
    ]
  )
  if signal.setup_type in exempt:
    return []

  feat = signal.feature_snapshot
  if feat is None:
    return []

  extra = feat.extra or {}
  fut_spot = extra.get("fut_spot")
  if fut_spot is None and feat.nifty_spot is not None:
    fut_spot = float(feat.nifty_spot)
  vwap = feat.session_vwap
  if fut_spot is None or vwap is None:
    if cfg.get("reject_if_unknown", False):
      return ["fut_momentum_unavailable"]
    return []

  fut_f = float(fut_spot)
  vwap_f = float(vwap)
  buffer_pts = float(cfg.get("vwap_buffer_points", 0))
  reasons: list[str] = []

  # Primary: FUT must sit on the correct side of session VWAP.
  if signal.side == "CE" and fut_f + buffer_pts < vwap_f:
    reasons.append("fut_momentum_not_aligned")
  if signal.side == "PE" and fut_f - buffer_pts > vwap_f:
    reasons.append("fut_momentum_not_aligned")
  if reasons:
    return reasons

  setup = signal.setup_type
  sustain_setups = set(
    cfg.get("vwap_sustain_setups")
    or [
      "vwap_trend",
      "vwap_pullback",
      "momentum_continuation",
      "trend_day",
      "opening_range_breakout",
      "gap_and_go",
      "pdh_pdl_break",
      "vwap_reclaim",
    ]
  )
  if cfg.get("require_vwap_sustain", True) and setup in sustain_setups:
    min_bars = int(cfg.get("min_bars_with_vwap", 2))
    bars_with = int(extra.get("bars_with_vwap_3m") or 0)
    if bars_with < min_bars:
      reasons.append("fut_vwap_not_sustained")

  volume_setups = set(
    cfg.get("volume_confirm_setups")
    or [
      "opening_range_breakout",
      "gap_and_go",
      "pdh_pdl_break",
      "vwap_reclaim",
    ]
  )
  if cfg.get("require_volume_strength", True) and setup in volume_setups:
    min_rel = float(cfg.get("min_volume_strength", 1.0))
    rel = extra.get("futures_volume_strength")
    if rel is None:
      if cfg.get("reject_if_volume_unknown", False):
        reasons.append("fut_volume_unavailable")
    elif float(rel) < min_rel:
      reasons.append("fut_volume_weak")

  return reasons
