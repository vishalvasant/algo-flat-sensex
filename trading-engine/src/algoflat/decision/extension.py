"""VWAP extension state — strategy-dependent entry constraints."""
from __future__ import annotations

from algoflat.models.events import CandidateSignal, FeatureSnapshot, MarketRegime

CONTINUATION_SETUPS = frozenset(
  {
    "vwap_trend",
    "momentum_continuation",
    "trend_day",
    "gap_and_go",
    "opening_range_breakout",
    "delta_momentum",
    "pdh_pdl_break",
    "oi_breakout",
    "gap_fade",
  }
)
REVERSAL_SETUPS = frozenset(
  {
    "peak_reversal_fade",
    "level_rejection",
    "vwap_rejection",
    "gap_fade",
    "liquidity_sweep",
  }
)
PULLBACK_SETUPS = frozenset({"vwap_pullback", "vwap_reclaim", "ema_pullback"})


def classify_extension_state(
  features: FeatureSnapshot,
  regime: MarketRegime,
  *,
  cfg: dict,
) -> str:
  extra = features.extra or {}
  dist = abs(float(extra.get("distance_to_vwap_points") or 0))
  moderate = float(cfg.get("moderate_points", 15))
  high = float(cfg.get("high_points", 25))
  extreme = float(cfg.get("extreme_points", 35))

  if dist < moderate:
    return "NORMAL"
  if dist < high:
    return "MODERATE"
  if dist < extreme:
    return "HIGH"

  reversal_signals = int(extra.get("reversal_signal_count") or 0)
  min_rev = int(cfg.get("exhaustion_min_reversal_signals", 2))
  structure = str(extra.get("structure_5m") or regime.health.get("structure_5m") or "")
  fresh_breakout = bool(extra.get("fresh_breakout")) or structure in ("hhhl", "lllh")

  if reversal_signals >= min_rev and not fresh_breakout:
    return "EXTREME_EXHAUSTION"
  if fresh_breakout:
    return "EXTREME_CONTINUATION_CONFIRMATION"
  return "EXTREME"


def extension_veto_reasons(
  signal: CandidateSignal,
  extension_state: str,
  features: FeatureSnapshot,
  *,
  cfg: dict,
) -> list[str]:
  if not cfg.get("enabled", True):
    return []

  setup = signal.setup_type
  block_states = set(cfg.get("continuation_block_states") or ["HIGH", "EXTREME"])
  extra = features.extra or {}

  if setup in CONTINUATION_SETUPS:
    if extension_state in block_states:
      if not extra.get("fresh_breakout") and not extra.get("trigger_1m"):
        return ["extension_blocks_late_continuation"]
    if extension_state == "EXTREME_EXHAUSTION":
      return ["extension_exhaustion_blocks_continuation"]

  if setup in PULLBACK_SETUPS:
    if extension_state in ("EXTREME", "EXTREME_EXHAUSTION"):
      if not extra.get("trigger_vwap_pullback") and not features.trigger_1m:
        return ["pullback_not_complete_at_extension"]

  if setup in REVERSAL_SETUPS:
    if extension_state in ("NORMAL", "MODERATE"):
      if setup not in ("gap_fade", "liquidity_sweep"):
        return ["reversal_needs_extension"]

  return []
