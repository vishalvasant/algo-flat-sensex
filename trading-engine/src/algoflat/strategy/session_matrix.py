"""Session playbook — map open/gap/momentum regime to eligible strategies."""
from __future__ import annotations

from typing import Any

from algoflat.features.gap import gap_class, gap_mode_cfg
from algoflat.models.events import Bias, FeatureSnapshot, MarketRegime

_BUCKET_ORDER = (
  "expiry_day",
  "gap_up_strong_open",
  "gap_up_fade",
  "gap_down_strong_open",
  "gap_down_fade",
  "sideways_range",
  "flat_trend",
)


def session_matrix_cfg(strategy: dict) -> dict[str, Any]:
  return strategy.get("session_matrix") or {}


def classify_session_bucket(
  features: FeatureSnapshot,
  regime: MarketRegime,
  *,
  is_expiry_day: bool = False,
  cfg: dict | None = None,
) -> str:
  """Classify the active session bucket for strategy routing."""
  matrix = cfg or {}
  if not matrix.get("enabled", False):
    return "flat_trend"

  extra = features.extra or {}
  gap_cfg = gap_mode_cfg({"gap_mode": matrix.get("gap_mode") or {}})
  min_gap = float(matrix.get("min_gap_points", gap_cfg.get("min_gap_points", 35)))
  gap_pts = extra.get("gap_points")
  gcls = gap_class(float(gap_pts) if gap_pts is not None else None, min_gap=min_gap)

  strong_ext = float(matrix.get("strong_open_extension_points", 15))
  fade_ext = float(matrix.get("fade_extension_points", 20))
  dist = abs(float(extra.get("distance_to_vwap_points") or 0))
  health = regime.health or {}
  session_phase = str(health.get("session_phase") or "")
  in_open = session_phase == "open" or regime.primary == "opening_range"
  counter_sigs = len(extra.get("counter_bias_signals") or [])
  mismatch = bool(extra.get("bias_confidence_mismatch"))

  if is_expiry_day and matrix.get("buckets", {}).get("expiry_day"):
    return "expiry_day"

  # Gap playbooks apply only during the open window. Keeping gap_down_fade /
  # gap_up_fade all day was locking out trend setups after the open recovered
  # (live Aug 12: trending_up late session still stuck on gap_down_fade).
  gap_only_in_open = bool(matrix.get("gap_buckets_open_only", True))
  apply_gap = (not gap_only_in_open) or in_open

  if apply_gap and gcls == "gap_up":
    if in_open and dist >= strong_ext and features.bias_5m == Bias.BULLISH:
      return "gap_up_strong_open"
    if dist >= fade_ext or counter_sigs >= 2 or mismatch:
      return "gap_up_fade"
    return "gap_up_strong_open"

  if apply_gap and gcls == "gap_down":
    if in_open and dist >= strong_ext and features.bias_5m == Bias.BEARISH:
      return "gap_down_strong_open"
    if dist >= fade_ext or counter_sigs >= 2 or mismatch:
      return "gap_down_fade"
    return "gap_down_strong_open"

  if regime.primary == "sideways" or (
    str(extra.get("structure_5m") or "") == "mixed" and dist < fade_ext
  ):
    return "sideways_range"

  if regime.primary in ("trending_up", "trending_down", "breakout", "low_volatility"):
    return "flat_trend"

  return str(matrix.get("default_bucket") or "flat_trend")


def allowed_strategies_for_bucket(
  bucket: str,
  cfg: dict | None = None,
) -> set[str] | None:
  matrix = cfg or {}
  buckets = matrix.get("buckets") or {}
  raw = buckets.get(bucket)
  if not raw:
    return None
  return set(raw)


def resolve_session_allowlist(
  features: FeatureSnapshot,
  regime: MarketRegime,
  *,
  is_expiry_day: bool = False,
  strategy: dict,
) -> tuple[set[str] | None, str, list[str]]:
  """Return (allowlist or None, bucket_name, log lines)."""
  matrix = session_matrix_cfg(strategy)
  if not matrix.get("enabled", False):
    return None, "disabled", ["session_matrix=disabled"]

  bucket = classify_session_bucket(
    features, regime, is_expiry_day=is_expiry_day, cfg=matrix
  )
  allowed = allowed_strategies_for_bucket(bucket, matrix)
  logs = [f"session_bucket={bucket}"]
  if allowed is not None:
    logs.append(f"session_allowlist={sorted(allowed)}")
  return allowed, bucket, logs
