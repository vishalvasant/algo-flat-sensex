"""FUT-first trigger policy — reduce 1m noise on trend setups."""
from __future__ import annotations

from typing import Any


def fut_first_cfg(strategy: dict) -> dict[str, Any]:
  return strategy.get("fut_first") or {}


def fut_first_enabled(strategy: dict) -> bool:
  return bool(fut_first_cfg(strategy).get("enabled", False))


def requires_1m_trigger(strategy: dict, setup_name: str) -> bool:
  """When FUT-first is on, only listed setups require a 1m trigger."""
  if not fut_first_enabled(strategy):
    return True
  required = set(fut_first_cfg(strategy).get("require_1m_trigger_setups") or [])
  return setup_name in required


def trend_uses_5m_momentum(strategy: dict) -> bool:
  cfg = fut_first_cfg(strategy)
  return bool(cfg.get("trend_use_5m_momentum", True)) if fut_first_enabled(strategy) else False
