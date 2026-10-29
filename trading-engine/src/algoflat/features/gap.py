"""Gap open classification and helpers."""
from __future__ import annotations

from typing import Any


def gap_class(gap_points: float | None, *, min_gap: float) -> str:
  if gap_points is None:
    return "flat"
  if gap_points >= min_gap:
    return "gap_up"
  if gap_points <= -min_gap:
    return "gap_down"
  return "flat"


def gap_mode_cfg(strategy: dict) -> dict[str, Any]:
  return strategy.get("gap_mode") or {}


def active_gap_allowlist(
  gap_points: float | None,
  gap_cfg: dict,
) -> set[str] | None:
  if not gap_cfg.get("enabled", True):
    return None
  min_gap = float(gap_cfg.get("min_gap_points", 35))
  cls = gap_class(gap_points, min_gap=min_gap)
  if cls == "gap_up":
    allowed = gap_cfg.get("gap_up_allowed_strategies")
    return set(allowed) if allowed else None
  if cls == "gap_down":
    allowed = gap_cfg.get("gap_down_allowed_strategies")
    return set(allowed) if allowed else None
  return None
