"""Decision log deduplication and metadata enrichment for the audit feed."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from algoflat.models.events import StrategyDecision


@dataclass
class _DedupeState:
  fingerprint: str | None = None
  last_logged_at: datetime | None = None
  suppressed: int = 0


@dataclass
class DecisionLogPolicy:
  """Suppress identical consecutive NO_TRADE rows; always log trade signals."""

  enabled: bool = True
  repeat_seconds: int = 120
  _state: _DedupeState = field(default_factory=_DedupeState)

  @classmethod
  def from_router_config(cls, router_cfg: dict) -> DecisionLogPolicy:
    return cls(
      enabled=bool(router_cfg.get("log_dedupe_identical_no_trade", True)),
      repeat_seconds=int(router_cfg.get("log_no_trade_repeat_seconds", 120)),
    )

  def reset_session(self) -> None:
    self._state = _DedupeState()

  @staticmethod
  def fingerprint(decision: StrategyDecision) -> str:
    regime = decision.regime.primary if decision.regime else ""
    return "|".join(
      [
        decision.selected_strategy or "",
        decision.selected_reason or "",
        regime,
        "1" if decision.trade_allowed else "0",
      ]
    )

  def prepare_log(
    self,
    decision: StrategyDecision,
    *,
    now: datetime,
  ) -> tuple[bool, dict[str, Any]]:
    """Return whether to persist and extra metadata fields for the journal row."""
    extra: dict[str, Any] = {}
    extra.update(extract_pipeline_scores(decision))

    if decision.trade_allowed:
      extra = self._attach_suppressed(extra)
      self._state = _DedupeState(
        fingerprint=self.fingerprint(decision),
        last_logged_at=now,
      )
      return True, extra

    if not self.enabled:
      self._state = _DedupeState(
        fingerprint=self.fingerprint(decision),
        last_logged_at=now,
      )
      return True, extra

    fp = self.fingerprint(decision)
    if self._state.fingerprint != fp:
      extra = self._attach_suppressed(extra)
      self._state = _DedupeState(fingerprint=fp, last_logged_at=now)
      return True, extra

    self._state.suppressed += 1
    elapsed = 0.0
    if self._state.last_logged_at is not None:
      elapsed = (now - self._state.last_logged_at).total_seconds()
    if elapsed >= self.repeat_seconds:
      extra = self._attach_suppressed(extra)
      self._state.last_logged_at = now
      return True, extra

    return False, extra

  def _attach_suppressed(self, extra: dict[str, Any]) -> dict[str, Any]:
    if self._state.suppressed > 0:
      extra["suppressed_duplicate_scans"] = self._state.suppressed
      self._state.suppressed = 0
    return extra


def extract_pipeline_scores(decision: StrategyDecision) -> dict[str, int]:
  """Pull CE/PE/no-trade scores from router pipeline diagnostics."""
  for warning in decision.warnings:
    if not str(warning).startswith("decision="):
      continue
    try:
      payload = ast.literal_eval(str(warning).split("=", 1)[1])
    except (SyntaxError, ValueError):
      continue
    if not isinstance(payload, dict):
      continue
    out: dict[str, int] = {}
    for key in ("ce_score", "pe_score", "no_trade_score"):
      val = payload.get(key)
      if val is not None:
        out[key] = int(val)
    if out:
      return out
  return {}
