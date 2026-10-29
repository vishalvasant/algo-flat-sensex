"""Phases 11–14 — ML trade quality predictor (shadow mode only)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class MLTradeQualityService:
  """Optional P(profitable) layer — never blocks engine when unavailable."""

  def __init__(self, ml_cfg: dict) -> None:
    self._cfg = ml_cfg or {}
    self._enabled = bool(self._cfg.get("enabled", False))
    self._mode = str(self._cfg.get("mode", "shadow"))
    self._threshold = float(self._cfg.get("score_threshold", 0.6))
    self._gate_enabled = bool(self._cfg.get("gate_enabled", False))

  def predict(self, mi_snapshot: dict[str, Any], *, signal_confidence: int = 0) -> dict[str, Any]:
    now = datetime.now(tz=timezone.utc)
    if not self._enabled:
      return {
        "model_available": False,
        "probability_profit": None,
        "expected_return": None,
        "expected_adverse_move": None,
        "model_version": None,
        "prediction_timestamp": now.isoformat(),
        "mode": "disabled",
      }

  # Heuristic shadow scorer until offline model trained (Phase 13).
    ce_pe = mi_snapshot.get("ce_pe") or {}
    premium = mi_snapshot.get("premium") or {}
    dq = mi_snapshot.get("data_quality") or {}
    prob = min(0.92, max(0.25, (signal_confidence / 100.0) * 0.6))
    if ce_pe.get("verdict") == "both_weak":
      prob *= 0.7
    if dq.get("state") == "DATA_QUALITY_DEGRADED":
      prob *= 0.85
    if premium.get("ce_confirm") or premium.get("pe_confirm"):
      prob = min(0.95, prob + 0.05)

    would_trade = prob >= self._threshold
    return {
      "model_available": True,
      "model_version": "shadow_heuristic_v0",
      "probability_profit": round(prob, 4),
      "expected_return": round(prob * 22 - (1 - prob) * 14, 2),
      "expected_adverse_move": round((1 - prob) * 14, 2),
      "prediction_timestamp": now.isoformat(),
      "mode": self._mode,
      "would_trade_ml": would_trade,
      "affects_live_orders": self._gate_enabled and self._mode != "shadow",
      "gate_enabled": self._gate_enabled,
    }

  def shadow_log_fields(self, rule_approved: bool, prediction: dict[str, Any]) -> dict[str, Any]:
    prob = prediction.get("probability_profit")
    ml_trade = prob is not None and prob >= self._threshold
    return {
      "RULE_DECISION": "TRADE" if rule_approved else "NO_TRADE",
      "ML_PREDICTION": prob,
      "HYPOTHETICAL_ML_DECISION": "TRADE" if ml_trade else "NO_TRADE",
    }
