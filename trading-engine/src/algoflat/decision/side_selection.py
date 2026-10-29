"""CE vs PE vs NO_TRADE opportunity comparison."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from algoflat.models.events import CandidateSignal, FeatureSnapshot, MarketRegime


@dataclass
class ScoredCandidate:
  strategy: str
  signal: CandidateSignal
  confidence: int
  context: dict[str, Any]


@dataclass
class SideSelectionResult:
  selected: ScoredCandidate | None
  ce_score: int
  pe_score: int
  no_trade_score: int
  logs: list[str]


def _no_trade_score(
  features: FeatureSnapshot,
  regime: MarketRegime,
  *,
  cfg: dict,
) -> tuple[int, list[str]]:
  base = int(cfg.get("base_score", 55))
  logs: list[str] = [f"no_trade_base={base}"]
  extra = features.extra or {}

  if not regime.trade_allowed:
    return 100, logs + ["regime_blocks_trade"]

  if extra.get("dead_range") or extra.get("sideways_substate") == "DEAD_RANGE":
    return min(100, base + 35), logs + ["dead_range_boost"]

  sideways_prob = float(regime.probabilities.get("sideways", 0))
  if sideways_prob >= 35:
    base += 12
    logs.append("sideways_prob_boost=+12")

  if extra.get("weak_option_response"):
    base += 15
    logs.append("weak_option_response_boost=+15")

  risk = int(regime.risk_score or 0)
  if risk >= 75:
    base += min(20, (risk - 70))
    logs.append(f"high_risk_boost=+{min(20, risk - 70)}")

  return min(100, base), logs


def select_best_side(
  candidates: list[ScoredCandidate],
  features: FeatureSnapshot,
  regime: MarketRegime,
  *,
  cfg: dict,
) -> SideSelectionResult:
  logs: list[str] = []
  if not cfg.get("enabled", True):
    if not candidates:
      return SideSelectionResult(None, 0, 0, 0, logs)
    best = max(candidates, key=lambda c: c.confidence)
    ce = max((c.confidence for c in candidates if c.signal.side == "CE"), default=0)
    pe = max((c.confidence for c in candidates if c.signal.side == "PE"), default=0)
    return SideSelectionResult(best, ce, pe, 0, logs)

  ce_best = max(
    (c for c in candidates if c.signal.side == "CE"),
    key=lambda c: c.confidence,
    default=None,
  )
  pe_best = max(
    (c for c in candidates if c.signal.side == "PE"),
    key=lambda c: c.confidence,
    default=None,
  )
  ce_score = ce_best.confidence if ce_best else 0
  pe_score = pe_best.confidence if pe_best else 0
  no_trade_score, nt_logs = _no_trade_score(features, regime, cfg=cfg.get("no_trade") or {})
  logs.extend(nt_logs)

  extra = features.extra or {}
  # Only blend when a real side candidate exists — otherwise MI strengths invent
  # phantom CE/PE scores (~25/26) and can "win" vs NO_TRADE with best_side=None.
  mi_ce = extra.get("ce_strength")
  mi_pe = extra.get("pe_strength")
  mi_blend = float((cfg.get("mi_blend") or {}).get("weight", 0.35))
  if mi_ce is not None and mi_pe is not None and mi_blend > 0 and (ce_best or pe_best):
    if ce_best is not None:
      ce_score = int(round(ce_score * (1 - mi_blend) + float(mi_ce) * mi_blend))
    if pe_best is not None:
      pe_score = int(round(pe_score * (1 - mi_blend) + float(mi_pe) * mi_blend))
    logs.append(f"mi_blend ce={ce_score} pe={pe_score}")

  if extra.get("mi_no_trade_recommended"):
    no_trade_score = min(100, no_trade_score + int(cfg.get("mi_no_trade_boost", 12)))
    logs.append("mi_no_trade_recommended_boost")

  ce_pe_verdict = extra.get("ce_pe_verdict")
  if ce_pe_verdict == "both_weak":
    no_trade_score = min(100, no_trade_score + 18)
    logs.append("mi_both_weak_boost")

  min_edge = int(cfg.get("min_edge_over_no_trade", 5))
  best_side: ScoredCandidate | None = None

  if not candidates:
    logs.append("no_gate_passing_candidates")
    return SideSelectionResult(None, ce_score, pe_score, no_trade_score, logs)

  if ce_best is not None and ce_score >= pe_score and ce_score > 0:
    if ce_score >= no_trade_score + min_edge:
      best_side = ce_best
      logs.append(f"selected_ce score={ce_score} vs no_trade={no_trade_score}")
    else:
      logs.append(f"ce_loses_to_no_trade ce={ce_score} no_trade={no_trade_score}")
  elif pe_best is not None and pe_score > 0:
    if pe_score >= no_trade_score + min_edge:
      best_side = pe_best
      logs.append(f"selected_pe score={pe_score} vs no_trade={no_trade_score}")
    else:
      logs.append(f"pe_loses_to_no_trade pe={pe_score} no_trade={no_trade_score}")
  else:
    logs.append("no_ce_pe_candidates")

  if best_side is None and candidates:
    overall = max(candidates, key=lambda c: c.confidence)
    if overall.confidence >= no_trade_score + min_edge:
      best_side = overall
      logs.append(f"fallback_best_strategy={overall.strategy}")

  if best_side is not None and cfg.get("veto_against_ce_pe_verdict", True):
    counter = {"peak_reversal_fade", "level_rejection", "gap_fade"}
    verdict = extra.get("ce_pe_verdict")
    side = best_side.signal.side
    setup = best_side.signal.setup_type
    if setup not in counter:
      # A premium verdict only cancels a side when the other side has a real candidate.
      if verdict == "ce_better" and side == "PE" and ce_best is not None:
        logs.append(f"veto_pe_while_ce_stronger ce={ce_score} pe={pe_score}")
        best_side = None
      elif verdict == "pe_better" and side == "CE" and pe_best is not None:
        logs.append(f"veto_ce_while_pe_stronger ce={ce_score} pe={pe_score}")
        best_side = None

  return SideSelectionResult(best_side, ce_score, pe_score, no_trade_score, logs)
