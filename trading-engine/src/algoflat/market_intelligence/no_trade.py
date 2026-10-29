"""Phase 34 — Explicit NO_TRADE reason codes for debugging."""
from __future__ import annotations

from typing import Any


NO_TRADE_CODES = (
  "NO_TRADE_LOW_EDGE",
  "NO_TRADE_REGIME",
  "NO_TRADE_EXTENSION",
  "NO_TRADE_OPTION_CONFIRMATION",
  "NO_TRADE_CE_PE_UNCLEAR",
  "NO_TRADE_BREADTH_CONFLICT",
  "NO_TRADE_FUT_DIVERGENCE",
  "NO_TRADE_RISK",
  "NO_TRADE_DATA_QUALITY",
  "NO_TRADE_ML_LOW_PROBABILITY",
  "NO_TRADE_NEGATIVE_EV",
)


def classify_no_trade_code(
  *,
  veto_reasons: list[str] | None = None,
  pipeline_vetoes: list[str] | None = None,
  extra: dict[str, Any] | None = None,
  regime_trade_allowed: bool = True,
  trade_allowed: bool = False,
  ml_prediction: dict[str, Any] | None = None,
  expected_value: float | None = None,
  ev_threshold: float = 0.0,
) -> str | None:
  if trade_allowed:
    return None

  vetoes = list(veto_reasons or []) + list(pipeline_vetoes or [])
  extra = extra or {}
  lowered = [v.lower() for v in vetoes]

  if any("data_quality" in v or "stale" in v for v in lowered):
    return "NO_TRADE_DATA_QUALITY"
  if any("kill_switch" in v or "entries_blocked" in v or "risk" in v or "cooldown" in v for v in lowered):
    return "NO_TRADE_RISK"
  if not regime_trade_allowed or any("regime" in v for v in lowered):
    return "NO_TRADE_REGIME"
  if any("extension" in v or "chase" in v for v in lowered):
    return "NO_TRADE_EXTENSION"
  if any(
    "premium" in v or "option_" in v or "mi_premium" in v or "chart" in v for v in lowered
  ):
    return "NO_TRADE_OPTION_CONFIRMATION"
  if extra.get("mi_no_trade_recommended") or extra.get("ce_pe_verdict") in (
    "both_weak",
    "no_clear_advantage",
  ):
    return "NO_TRADE_CE_PE_UNCLEAR"
  if any("breadth" in v or "divergence" in v for v in lowered):
    return "NO_TRADE_BREADTH_CONFLICT"
  if any("basis" in v or "fut_momentum" in v or "spot_fut" in v for v in lowered):
    return "NO_TRADE_FUT_DIVERGENCE"
  if expected_value is not None and expected_value < ev_threshold:
    return "NO_TRADE_NEGATIVE_EV"
  if any("no_trade" in v or "edge" in v for v in lowered):
    return "NO_TRADE_LOW_EDGE"

  ml = ml_prediction or {}
  if ml.get("would_trade_ml") is False and ml.get("model_available"):
    return "NO_TRADE_ML_LOW_PROBABILITY"

  return "NO_TRADE_LOW_EDGE"
