"""Late-entry / chase filter — reject moves already extended without fresh confirmation."""
from __future__ import annotations

from algoflat.decision.extension import CONTINUATION_SETUPS
from algoflat.models.events import CandidateSignal, FeatureSnapshot


def chase_veto_reasons(
  signal: CandidateSignal,
  features: FeatureSnapshot,
  *,
  cfg: dict,
) -> list[str]:
  if not cfg.get("enabled", True):
    return []

  if signal.setup_type not in CONTINUATION_SETUPS:
    return []

  extra = features.extra or {}
  dist = abs(float(extra.get("distance_to_vwap_points") or 0))
  max_ext = float(cfg.get("max_extension_without_retest", 30))
  recent_move = abs(float(extra.get("recent_move_points") or 0))
  max_recent = float(cfg.get("max_recent_move_points", 45))

  has_retest = bool(
    extra.get("fresh_breakout")
    or extra.get("trigger_1m")
    or extra.get("trigger_vwap_pullback")
    or features.trigger_1m
  )
  premium_already_expanded = bool(extra.get("premium_already_expanded"))

  if dist >= max_ext and not has_retest:
    return ["late_entry_extended_from_vwap"]

  if recent_move >= max_recent and not has_retest:
    return ["late_entry_after_large_move"]

  if premium_already_expanded and not has_retest:
    return ["late_entry_premium_already_expanded"]

  return []
