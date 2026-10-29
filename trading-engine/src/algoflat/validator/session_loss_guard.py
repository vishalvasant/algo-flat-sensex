"""Block aggressive setups after expiry-day losses (ORB / expiry scalping cascade)."""
from __future__ import annotations

from dataclasses import dataclass, field

from algoflat.models.events import CandidateSignal


@dataclass
class SessionLossGuard:
  """Tracks per-session losses for setup families."""

  _loss_counts: dict[str, int] = field(default_factory=dict)
  _expiry_aggressive_blocked: bool = False

  def reset(self) -> None:
    self._loss_counts.clear()
    self._expiry_aggressive_blocked = False

  def record_loss(
    self,
    setup_type: str,
    *,
    is_expiry_day: bool,
    exit_reason: str | None,
    cfg: dict,
  ) -> None:
    guard = cfg.get("session_loss_guard") or {}
    if not guard.get("enabled", False):
      return

    self._loss_counts[setup_type] = self._loss_counts.get(setup_type, 0) + 1

    if not is_expiry_day:
      return

    expiry_cfg = guard.get("expiry_day") or {}
    aggressive = set(
      expiry_cfg.get("aggressive_setups")
      or ["opening_range_breakout", "expiry_scalping"]
    )
    trigger_reasons = set(
      expiry_cfg.get("trigger_exit_reasons") or ["adverse_momentum", "early_invalidation"]
    )
    after_losses = int(expiry_cfg.get("block_after_losses", 1))

    if setup_type not in aggressive:
      return
    if exit_reason and exit_reason not in trigger_reasons:
      return

    total_aggressive_losses = sum(
      self._loss_counts.get(s, 0) for s in aggressive
    )
    if total_aggressive_losses >= after_losses:
      self._expiry_aggressive_blocked = True

  def rejection_reasons(
    self,
    signal: CandidateSignal,
    *,
    is_expiry_day: bool,
    cfg: dict,
  ) -> list[str]:
    guard = cfg.get("session_loss_guard") or {}
    if not guard.get("enabled", False) or not is_expiry_day:
      return []

    expiry_cfg = guard.get("expiry_day") or {}
    blocked = set(
      expiry_cfg.get("blocked_setups")
      or ["opening_range_breakout", "expiry_scalping"]
    )
    if self._expiry_aggressive_blocked and signal.setup_type in blocked:
      return ["expiry_day_aggressive_setup_paused"]
    return []
