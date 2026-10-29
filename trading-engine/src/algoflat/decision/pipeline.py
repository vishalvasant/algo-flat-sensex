"""Decision pipeline — integrates hard gates, market state, side selection, EV."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from algoflat.decision.chase_filter import chase_veto_reasons
from algoflat.decision.data_quality import assess_data_quality
from algoflat.decision.expected_value import ev_veto_reasons, estimate_expected_value
from algoflat.decision.extension import classify_extension_state, extension_veto_reasons
from algoflat.decision.market_state import (
  classify_sideways_substate,
  detect_transition_state,
  sideways_veto_reasons,
  transition_veto_reasons,
)
from algoflat.decision.side_selection import ScoredCandidate, select_best_side
from algoflat.models.events import CandidateSignal, FeatureSnapshot, MarketRegime, OptionState


@dataclass
class PipelineResult:
  approved: bool
  signal: ScoredCandidate | None
  hard_gate_failures: list[str] = field(default_factory=list)
  veto_reasons: list[str] = field(default_factory=list)
  extension_state: str = "NORMAL"
  sideways_substate: str | None = None
  transition_state: str | None = None
  data_quality: str = "DATA_QUALITY_OK"
  ce_score: int = 0
  pe_score: int = 0
  no_trade_score: int = 0
  expected_value: float = 0.0
  opportunity_score: int = 0
  logs: list[str] = field(default_factory=list)
  shadow_only: list[str] = field(default_factory=list)


class DecisionPipeline:
  """Post-scanner decision layer per Fix Specification v1.0."""

  def __init__(self, config) -> None:
    self._cfg = config.strategy.get("decision_pipeline") or {}
    self._enabled = bool(self._cfg.get("enabled", True))
    self._enforce = bool(self._cfg.get("enforce", True))
    self._shadow = bool(self._cfg.get("shadow_mode", False))

  def evaluate(
    self,
    candidates: list[ScoredCandidate],
    features: FeatureSnapshot,
    regime: MarketRegime,
    *,
    options_by_strategy: dict[str, OptionState | dict | None] | None = None,
    candles_stale: bool = False,
    last_refresh_ok: bool = True,
  ) -> PipelineResult:
    logs: list[str] = []
    hard_failures: list[str] = []
    vetoes: list[str] = []
    shadow: list[str] = []

    ext_cfg = self._cfg.get("extension") or {}
    ms_cfg = self._cfg.get("market_state") or {}
    dq_cfg = self._cfg.get("data_quality") or {}

    extension_state = classify_extension_state(features, regime, cfg=ext_cfg)
    sideways_sub = classify_sideways_substate(features, regime, cfg=ms_cfg.get("sideways") or {})
    transition = detect_transition_state(features, regime, cfg=ms_cfg.get("transition") or {})
    logs.append(f"extension={extension_state} sideways={sideways_sub} transition={transition}")

    extra = features.extra or {}
    if sideways_sub:
      extra = {**extra, "sideways_substate": sideways_sub}
    features = features.model_copy(update={"extra": extra})

    side_cfg = self._cfg.get("side_selection") or {}
    selection = select_best_side(candidates, features, regime, cfg=side_cfg)
    logs.extend(selection.logs)

    if selection.selected is None:
      veto = "no_trade_preferred"
      for line in reversed(selection.logs):
        if line.startswith("veto_"):
          veto = line
          break
      return PipelineResult(
        approved=False,
        signal=None,
        hard_gate_failures=hard_failures,
        veto_reasons=[veto],
        extension_state=extension_state,
        sideways_substate=sideways_sub,
        transition_state=transition,
        ce_score=selection.ce_score,
        pe_score=selection.pe_score,
        no_trade_score=selection.no_trade_score,
        logs=logs,
      )

    picked = selection.selected
    signal = picked.signal
    option = _resolve_option(signal, options_by_strategy)

    dq_state, dq_failures = assess_data_quality(
      features=features,
      option=option,
      candles_stale=candles_stale,
      last_refresh_ok=last_refresh_ok,
      cfg=dq_cfg,
    )
    if dq_state == "DATA_QUALITY_INVALID":
      hard_failures.extend(dq_failures)

    hard_failures.extend(extension_veto_reasons(signal, extension_state, features, cfg=ext_cfg))
    hard_failures.extend(sideways_veto_reasons(sideways_sub, signal.setup_type, cfg=ms_cfg.get("sideways") or {}))
    hard_failures.extend(transition_veto_reasons(transition, signal.setup_type, cfg=ms_cfg.get("transition") or {}))
    hard_failures.extend(chase_veto_reasons(signal, features, cfg=self._cfg.get("chase_filter") or {}))
    hard_failures.extend(ev_veto_reasons(signal, regime, cfg=self._cfg.get("expected_value") or {}))

    ev, ev_logs = estimate_expected_value(signal, regime, cfg=self._cfg.get("expected_value") or {})
    logs.extend(ev_logs)

    opp = int((signal.scanner_metadata or {}).get("opportunity_bonus") or 0)

    if hard_failures:
      vetoes.extend(hard_failures)
      if self._shadow and not self._enforce:
        shadow.extend(hard_failures)
        logs.append(f"shadow_would_block={hard_failures}")

    approved = not vetoes if (self._enforce and not self._shadow) else True
    if not self._enabled:
      approved = True
      vetoes = []
    elif vetoes and self._enforce:
      approved = False

    return PipelineResult(
      approved=approved,
      signal=picked if approved else None,
      hard_gate_failures=hard_failures,
      veto_reasons=vetoes,
      extension_state=extension_state,
      sideways_substate=sideways_sub,
      transition_state=transition,
      data_quality=dq_state,
      ce_score=selection.ce_score,
      pe_score=selection.pe_score,
      no_trade_score=selection.no_trade_score,
      expected_value=ev,
      opportunity_score=opp,
      logs=logs,
      shadow_only=shadow,
    )

  def diagnostics_dict(self, result: PipelineResult) -> dict[str, Any]:
    return {
      "extension_state": result.extension_state,
      "sideways_substate": result.sideways_substate,
      "transition_state": result.transition_state,
      "data_quality": result.data_quality,
      "ce_score": result.ce_score,
      "pe_score": result.pe_score,
      "no_trade_score": result.no_trade_score,
      "opportunity_score": result.opportunity_score,
      "expected_value": round(result.expected_value, 2),
      "hard_gate_results": result.hard_gate_failures,
      "veto_reasons": result.veto_reasons,
      "shadow_only": result.shadow_only,
    }


def _resolve_option(
  signal: CandidateSignal,
  options_by_strategy: dict[str, OptionState | dict | None] | None,
) -> OptionState | None:
  if not options_by_strategy:
    return None
  for opt in options_by_strategy.values():
    if isinstance(opt, OptionState):
      if opt.instrument_token == signal.instrument_token:
        return opt
    elif isinstance(opt, dict):
      state = opt.get(signal.instrument_token)
      if isinstance(state, OptionState):
        return state
  return None
