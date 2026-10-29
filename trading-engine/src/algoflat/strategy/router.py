from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import structlog

from algoflat.config import AppConfig
from algoflat.contract_selector.selector import ContractUniverse
from algoflat.models.events import (
  Bias,
  CandidateSignal,
  FeatureSnapshot,
  MarketRegime,
  OptionState,
  StrategyDecision,
)
from algoflat.features.gap import active_gap_allowlist, gap_mode_cfg
from algoflat.scanner.base import StrategyScanner
from algoflat.strategy.opportunity_score import OpportunityScorer
from algoflat.strategy.expiry_distance import resolve_expiry_allowlist
from algoflat.strategy.session_matrix import resolve_session_allowlist
from algoflat.decision.pipeline import DecisionPipeline
from algoflat.decision.side_selection import ScoredCandidate

if False:  # TYPE_CHECKING
  from algoflat.journal.analytics import StrategyLearner

logger = structlog.get_logger(__name__)


def _score_context_from_option(
  signal: CandidateSignal,
  option: OptionState | dict[str, OptionState | None] | None,
) -> dict:
  """Build quality-gate context from option state / strike_pick metadata."""
  ctx: dict = {}
  pick = (signal.scanner_metadata or {}).get("strike_pick") or {}
  for k in ("delta", "gamma", "iv", "theta", "vega"):
    if pick.get(k) is not None:
      ctx[k] = pick[k]
  state: OptionState | None = None
  if isinstance(option, dict):
    state = option.get(signal.instrument_token)
  elif isinstance(option, OptionState):
    state = option
  if state is not None:
    if state.ltp is not None:
      ctx["ltp"] = float(state.ltp)
    if state.spread_pct is not None:
      ctx["spread_pct"] = float(state.spread_pct)
    if state.volume is not None:
      ctx["volume"] = state.volume
    if state.oi is not None:
      ctx["oi"] = state.oi
  meta = signal.scanner_metadata or {}
  if meta.get("option_ltp") is not None and "ltp" not in ctx:
    ctx["ltp"] = float(meta["option_ltp"])
  return ctx


def _diagnose_no_signal(
  name: str,
  features: FeatureSnapshot,
  option: OptionState | dict[str, OptionState | None] | None,
) -> str:
  """Explain why a scanner produced nothing — for Decision Logs."""
  if features.nifty_spot is None or features.session_vwap is None:
    return "missing_spot_or_vwap"
  if features.bias_5m == Bias.NEUTRAL:
    return "neutral_bias"
  has_ltp = False
  if isinstance(option, dict):
    has_ltp = any(s is not None and s.ltp is not None for s in option.values())
  elif option is not None:
    has_ltp = option.ltp is not None
  if not has_ltp:
    if option is None or (isinstance(option, dict) and not option):
      return "option_state_missing"
    return "option_ltp_missing"

  extra = features.extra or {}
  if name == "vwap_reclaim":
    if not features.setup_3m:
      return "no_reclaim_setup"
    if not features.trigger_1m:
      return "no_reclaim_trigger"
    if features.bias_5m == Bias.BULLISH and (
      features.setup_3m != "vwap_reclaim_bull"
      or features.trigger_1m != "vwap_reclaim_cross_up"
    ):
      return "reclaim_side_mismatch"
    if features.bias_5m == Bias.BEARISH and (
      features.setup_3m != "vwap_reclaim_bear"
      or features.trigger_1m != "vwap_reclaim_cross_down"
    ):
      return "reclaim_side_mismatch"
    return "reclaim_gate_failed"

  if name == "vwap_pullback":
    if not extra.get("setup_vwap_pullback"):
      return "no_pullback_setup"
    if not extra.get("trigger_vwap_pullback"):
      return "no_pullback_trigger"
    return "pullback_gate_failed"

  if name == "vwap_trend":
    if not extra.get("setup_vwap_trend"):
      return "no_trend_setup"
    return "trend_gate_failed"

  return "no_setup_or_trigger"


class StrategyRouter:
  """Pick exactly one coded strategy or NO_TRADE. Deterministic."""

  def __init__(
    self,
    config: AppConfig,
    scanners: list[StrategyScanner],
    quality: QualityGate,
  ) -> None:
    self._config = config
    router_cfg = config.strategy.get("router", {})
    enabled = set(router_cfg.get("enabled_strategies") or [s.name for s in scanners])
    self._scanners = [s for s in scanners if s.name in enabled]
    self._quality = quality
    self._opportunity = OpportunityScorer(config)
    self._decision = DecisionPipeline(config)
    self._min_confidence = quality.min_confidence
    self.last_decision: StrategyDecision | None = None

  def _score_context(
    self,
    signal: CandidateSignal,
    option: OptionState | dict[str, OptionState | None] | None,
  ) -> dict:
    return _score_context_from_option(signal, option)

  def _active_scanners(
    self,
    regime: MarketRegime,
    features: FeatureSnapshot,
    *,
    is_expiry_day: bool = False,
    learner: StrategyLearner | None = None,
    session_date: date | None = None,
    expiry_date: date | None = None,
  ) -> tuple[list[StrategyScanner], list[str]]:
    """Filter scanners by regime policy and session learner pause."""
    regime_cfg = self._config.strategy.get("regime", {})
    filter_mode = str(regime_cfg.get("filter_mode", "soft")).lower()
    expiry_mode = regime_cfg.get("expiry_mode") or {}
    gap_cfg = gap_mode_cfg(self._config.strategy)
    allowed: set[str] | None = None
    filter_logs: list[str] = []

    session_allowed, session_bucket, session_logs = resolve_session_allowlist(
      features,
      regime,
      is_expiry_day=is_expiry_day,
      strategy=self._config.strategy,
    )
    filter_logs.extend(session_logs)

    extra = features.extra or {}
    if session_date is None and extra.get("session_date"):
      try:
        session_date = date.fromisoformat(str(extra["session_date"]))
      except ValueError:
        session_date = None
    if (
      expiry_date is None
      and session_date is not None
      and extra.get("days_to_expiry") is not None
    ):
      from datetime import timedelta

      expiry_date = session_date + timedelta(days=int(extra["days_to_expiry"]))

    expiry_allowed, _dte, expiry_bucket, expiry_logs = resolve_expiry_allowlist(
      session_date=session_date or datetime.now(tz=timezone.utc).date(),
      expiry_date=expiry_date,
      is_expiry_day=is_expiry_day,
      strategy=self._config.strategy,
    )
    filter_logs.extend(expiry_logs)

    if filter_mode == "soft":
      filter_logs.append("regime_filter_mode=soft")
      if session_allowed is not None:
        allowed = session_allowed
    else:
      extra = features.extra or {}
      gap_pts = extra.get("gap_points")
      gap_allow = active_gap_allowlist(
        float(gap_pts) if gap_pts is not None else None, gap_cfg
      )
      in_open = regime.primary == "opening_range" or (
        regime.health or {}
      ).get("session_phase") == "open"
      if gap_allow is not None and in_open:
        allowed = gap_allow
        filter_logs.append(
          f"gap_mode class={extra.get('gap_class')} allowlist={sorted(allowed)}"
        )
      elif is_expiry_day and bool(expiry_mode.get("enabled", False)):
        allowed = set(expiry_mode.get("allowed_strategies") or [])
        filter_logs.append(f"expiry_mode allowlist={sorted(allowed)}")
      elif regime.primary == "sideways":
        sideways = regime_cfg.get("sideways_allowed_strategies")
        if sideways is not None:
          allowed = set(sideways)
          filter_logs.append(f"sideways allowlist={sorted(allowed)}")

    # Session matrix + expiry distance allowlists intersect.
    if session_allowed is not None:
      allowed = session_allowed if allowed is None else (allowed & session_allowed)
      filter_logs.append(f"session_bucket={session_bucket}")
    if expiry_allowed is not None:
      allowed = expiry_allowed if allowed is None else (allowed & expiry_allowed)
      filter_logs.append(f"expiry_bucket={expiry_bucket}")

    scanners = self._scanners
    if allowed is not None:
      scanners = [s for s in scanners if s.name in allowed]
      if not scanners:
        filter_logs.append("no_scanner_in_regime_allowlist")

    if learner is not None:
      paused = [s.name for s in scanners if learner.is_paused(s.name)]
      if paused:
        filter_logs.append(f"session_paused={paused}")
      scanners = [s for s in scanners if not learner.is_paused(s.name)]

    return scanners, filter_logs

  def route(
    self,
    features: FeatureSnapshot,
    regime: MarketRegime,
    universe: ContractUniverse,
    options_by_strategy: dict[str, OptionState | dict[str, OptionState | None] | None],
    *,
    is_expiry_day: bool = False,
    learner: StrategyLearner | None = None,
    session_date: date | None = None,
    expiry_date: date | None = None,
  ) -> tuple[StrategyDecision, CandidateSignal | None]:
    logs: list[str] = []
    warnings: list[str] = []
    scores: list[dict[str, Any]] = []
    now = datetime.now(tz=timezone.utc)
    feature_debug = {
      "bias": features.bias_5m.value,
      "spot": float(features.nifty_spot) if features.nifty_spot is not None else None,
      "vwap": float(features.session_vwap) if features.session_vwap is not None else None,
      "distance": (features.extra or {}).get("distance_to_vwap_points"),
      "setup_3m": features.setup_3m,
      "trigger_1m": features.trigger_1m,
      "setup_pullback": (features.extra or {}).get("setup_vwap_pullback"),
      "trigger_pullback": (features.extra or {}).get("trigger_vwap_pullback"),
      "setup_trend": (features.extra or {}).get("setup_vwap_trend"),
      "skip_reasons": (features.extra or {}).get("skip_reasons"),
      "candle_counts": (features.extra or {}).get("candle_counts"),
    }

    if not regime.trade_allowed:
      decision = StrategyDecision(
        ts=now,
        selected_strategy="NO_TRADE",
        confidence=0,
        trade_allowed=False,
        position_side="NONE",
        selected_reason="regime_blocks_trade: " + ", ".join(regime.reasons[:4]),
        regime=regime,
        strategy_scores=[],
        warnings=warnings,
        logs=["regime.trade_allowed=false", str(feature_debug)],
      )
      # Attach feature debug via warnings bucket for journal metadata consumers
      decision.warnings.append(f"features={feature_debug}")
      self.last_decision = decision
      return decision, None

    scanners, filter_logs = self._active_scanners(
      regime,
      features,
      is_expiry_day=is_expiry_day,
      learner=learner,
      session_date=session_date,
      expiry_date=expiry_date,
    )
    logs.extend(filter_logs)

    if not scanners:
      decision = StrategyDecision(
        ts=now,
        selected_strategy="NO_TRADE",
        confidence=0,
        trade_allowed=False,
        position_side="NONE",
        selected_reason="no_eligible_strategy_for_regime",
        regime=regime,
        strategy_scores=[],
        warnings=[f"features={feature_debug}"],
        logs=logs,
      )
      self.last_decision = decision
      return decision, None

    best_signal: CandidateSignal | None = None
    best_conf = -1
    best_name = "NO_TRADE"
    best_logs: list[str] = []
    scored_candidates: list[ScoredCandidate] = []

    for scanner in scanners:
      option = options_by_strategy.get(scanner.name)
      signal = scanner.scan(features, universe, option)
      if signal is None:
        reason = _diagnose_no_signal(scanner.name, features, option)
        scores.append(
          {
            "strategy": scanner.name,
            "compatible": False,
            "confidence": 0,
            "reason": reason,
          }
        )
        logs.append(f"{scanner.name}: {reason}")
        continue

      ctx = self._score_context(signal, option)
      conf, conf_logs = self._quality.score(
        signal,
        features,
        regime,
        context=ctx,
      )
      opp_bonus, opp_logs = self._opportunity.score(
        signal, features, regime, context=ctx
      )
      regime_penalty, pen_logs = self._opportunity.regime_penalty(
        scanner.name, regime, features, is_expiry_day=is_expiry_day
      )
      final_conf = max(0, min(100, conf + opp_bonus - regime_penalty))

      # Counter-side priority when MTF mismatch favours fade over trend.
      extra = features.extra or {}
      if bool(extra.get("bias_confidence_mismatch")) and scanner.name in (
        "peak_reversal_fade",
        "level_rejection",
        "gap_fade",
      ):
        gap = int(extra.get("mtf_confidence_gap") or 0)
        boost = min(12, max(0, gap // 2))
        if boost > 0:
          final_conf = min(100, final_conf + boost)
          conf_logs.append(f"counter_mismatch_boost=+{boost}")

      signal.confidence = final_conf
      signal.scanner_metadata = {
        **signal.scanner_metadata,
        "confidence": final_conf,
        "base_confidence": conf,
        "opportunity_bonus": opp_bonus,
        "regime_penalty": regime_penalty,
        "confidence_logs": conf_logs + opp_logs + pen_logs,
        "regime_primary": regime.primary,
      }
      entry = {
        "strategy": scanner.name,
        "compatible": True,
        "confidence": final_conf,
        "base_confidence": conf,
        "opportunity_bonus": opp_bonus,
        "regime_penalty": regime_penalty,
        "side": signal.side,
        "tsym": signal.tsym,
        "passes_gate": final_conf >= self._min_confidence,
        "logs": conf_logs + opp_logs + pen_logs,
      }
      scores.append(entry)
      logs.append(
        f"{scanner.name}: confidence={final_conf} "
        f"(base={conf} opp=+{opp_bonus} regime=-{regime_penalty})"
      )
      if final_conf >= self._min_confidence:
        scored_candidates.append(
          ScoredCandidate(
            strategy=scanner.name,
            signal=signal,
            confidence=final_conf,
            context=ctx,
          )
        )

      if final_conf > best_conf:
        best_conf = final_conf
        best_signal = signal
        best_name = scanner.name
        best_logs = conf_logs + opp_logs + pen_logs

    pipeline = self._decision.evaluate(
      scored_candidates,
      features,
      regime,
      options_by_strategy=options_by_strategy,
      candles_stale=bool((features.extra or {}).get("candles_stale")),
      last_refresh_ok=bool((features.extra or {}).get("last_refresh_ok", True)),
    )
    logs.extend(pipeline.logs)
    diag = self._decision.diagnostics_dict(pipeline)

    if pipeline.signal is not None:
      best_signal = pipeline.signal.signal
      best_conf = pipeline.signal.confidence
      best_name = pipeline.signal.strategy
    elif scored_candidates and pipeline.approved:
      pass  # keep legacy best_* from loop
    elif not pipeline.approved or pipeline.veto_reasons:
      reason = (
        "decision_pipeline_veto: " + ", ".join(pipeline.veto_reasons[:4])
        if pipeline.veto_reasons
        else f"no_trade_score={pipeline.no_trade_score} beats candidates"
      )
      decision = StrategyDecision(
        ts=now,
        selected_strategy="NO_TRADE",
        confidence=max(0, best_conf),
        trade_allowed=False,
        position_side="NONE",
        selected_reason=reason,
        regime=regime,
        strategy_scores=scores,
        warnings=[f"features={feature_debug}", f"decision={diag}"],
        logs=logs + best_logs,
      )
      self.last_decision = decision
      logger.info("strategy_routed_no_trade", reason=reason, decision=diag)
      return decision, None

    if best_signal is None or best_conf < self._min_confidence:
      if best_signal is not None:
        reason = f"best_confidence={best_conf} < min={self._min_confidence}"
      else:
        reasons = [f"{s['strategy']}={s.get('reason')}" for s in scores]
        reason = "no_strategy_produced_signal: " + "; ".join(reasons)
      decision = StrategyDecision(
        ts=now,
        selected_strategy="NO_TRADE",
        confidence=max(0, best_conf),
        trade_allowed=False,
        position_side="NONE",
        selected_reason=reason,
        regime=regime,
        strategy_scores=scores,
        warnings=[f"features={feature_debug}"],
        logs=logs + best_logs,
      )
      self.last_decision = decision
      logger.info(
        "strategy_routed_no_trade",
        reason=reason,
        features=feature_debug,
        regime=regime.primary,
      )
      return decision, None

    rivals = [s for s in scores if s.get("compatible") and s["strategy"] != best_name]
    if rivals:
      second = max((s.get("confidence") or 0) for s in rivals)
      if second == best_conf:
        warnings.append("confidence_tie_prefer_first_registered")

    decision = StrategyDecision(
      ts=now,
      selected_strategy=best_name,
      confidence=best_conf,
      trade_allowed=True,
      position_side=best_signal.side,
      selected_reason=(
        f"selected {best_name} confidence={best_conf} "
        f"regime={regime.primary} risk={regime.risk_score}"
      ),
      regime=regime,
      strategy_scores=scores,
      warnings=warnings + [f"decision={diag}"],
      logs=logs + best_logs,
    )
    self.last_decision = decision
    logger.info(
      "strategy_routed",
      strategy=best_name,
      confidence=best_conf,
      side=best_signal.side,
      tsym=best_signal.tsym,
      regime=regime.primary,
    )
    return decision, best_signal
