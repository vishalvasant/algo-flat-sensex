"""Sideways sub-states and regime transition detection."""
from __future__ import annotations

from algoflat.models.events import Bias, FeatureSnapshot, MarketRegime

DEAD_RANGE = "DEAD_RANGE"
VWAP_CHOP = "VWAP_CHOP"
RANGE_REJECTION = "RANGE_REJECTION"
RANGE_BREAKOUT = "RANGE_BREAKOUT"
PREMIUM_EXPANSION = "PREMIUM_EXPANSION"
COMPRESSION = "COMPRESSION"


def classify_sideways_substate(
  features: FeatureSnapshot,
  regime: MarketRegime,
  *,
  cfg: dict,
) -> str | None:
  if regime.primary != "sideways":
    return None

  extra = features.extra or {}
  health = regime.health or {}
  range_pts = float(health.get("range_5m_points") or extra.get("range_5m_points") or 999)
  tight = float(cfg.get("dead_range_max_points", 18))
  vwap_crosses = int(extra.get("vwap_cross_count_session") or 0)
  max_chop_crosses = int(cfg.get("vwap_chop_min_crosses", 4))

  chain = extra.get("chain") or {}
  ce_decay = bool(chain.get("ce_premium_decaying"))
  pe_decay = bool(chain.get("pe_premium_decaying"))
  premium_expansion = bool(chain.get("premium_expansion")) or bool(
    extra.get("premium_expansion")
  )

  if range_pts <= tight and ce_decay and pe_decay:
    return DEAD_RANGE
  if vwap_crosses >= max_chop_crosses:
    return VWAP_CHOP
  if premium_expansion:
    return PREMIUM_EXPANSION
  if extra.get("range_rejection"):
    return RANGE_REJECTION
  if extra.get("range_breakout") or extra.get("fresh_breakout"):
    return RANGE_BREAKOUT
  if health.get("atr_1m") and health.get("avg_atr_1m"):
    atr = float(health["atr_1m"])
    avg = float(health["avg_atr_1m"])
    if avg > 0 and atr <= avg * float(cfg.get("compression_atr_ratio", 0.75)):
      return COMPRESSION
  return VWAP_CHOP


def detect_transition_state(
  features: FeatureSnapshot,
  regime: MarketRegime,
  *,
  cfg: dict,
) -> str | None:
  if not cfg.get("enabled", True):
    return None

  extra = features.extra or {}
  structure = str(extra.get("structure_5m") or regime.health.get("structure_5m") or "")
  sideways_prob = float(regime.probabilities.get("sideways", 0))
  min_sideways = float(cfg.get("min_sideways_probability", 20))

  mixed = structure == "mixed" or sideways_prob >= min_sideways
  if not mixed:
    return None

  primary = regime.primary
  if features.bias_5m == Bias.BULLISH and primary in ("trending_up", "breakout"):
    return "TRANSITION_UP"
  if features.bias_5m == Bias.BEARISH and primary in ("trending_down", "breakout"):
    return "TRANSITION_DOWN"
  return None


def sideways_veto_reasons(
  substate: str | None,
  signal_setup: str,
  *,
  cfg: dict,
) -> list[str]:
  if substate is None or not cfg.get("enabled", True):
    return []

  if substate == DEAD_RANGE:
    allow = set(cfg.get("dead_range_allowed_setups") or ["level_rejection", "liquidity_sweep"])
    if signal_setup not in allow:
      return ["dead_range_no_trade"]

  if substate == VWAP_CHOP:
    block = set(
      cfg.get("vwap_chop_blocked_setups")
      or ["momentum_continuation", "vwap_trend", "delta_momentum", "gap_and_go"]
    )
    if signal_setup in block:
      return ["vwap_chop_blocks_directional"]

  if substate == COMPRESSION:
    allow = set(
      cfg.get("compression_allowed_setups")
      or ["opening_range_breakout", "gap_and_go", "pdh_pdl_break", "oi_breakout"]
    )
    if signal_setup not in allow:
      return ["compression_wait_for_breakout"]

  return []


def transition_veto_reasons(
  transition: str | None,
  signal_setup: str,
  *,
  cfg: dict,
) -> list[str]:
  if transition is None or not cfg.get("enabled", True):
    return []

  block = set(
    cfg.get("block_continuation_setups")
    or [
      "momentum_continuation",
      "vwap_trend",
      "trend_day",
      "gap_and_go",
      "delta_momentum",
    ]
  )
  if cfg.get("block_continuation_in_transition", True) and signal_setup in block:
    return [f"regime_{transition.lower()}_blocks_continuation"]
  return []
