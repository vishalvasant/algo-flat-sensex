"""Deterministic expected-value filter — decision aid, not a prediction."""
from __future__ import annotations

from algoflat.models.events import CandidateSignal, MarketRegime


def estimate_expected_value(
  signal: CandidateSignal,
  regime: MarketRegime,
  *,
  cfg: dict,
  estimated_cost_inr: float = 2.0,
) -> tuple[float, list[str]]:
  if not cfg.get("enabled", True):
    return 0.0, []

  conf = int(signal.confidence or 0)
  prob = min(0.95, max(0.35, conf / 100.0))

  meta = signal.scanner_metadata or {}
  try:
    ltp = float(meta.get("option_ltp") or 0)
  except (TypeError, ValueError):
    ltp = 0.0
  try:
    lot = int(meta.get("lot_size") or 0)
  except (TypeError, ValueError):
    lot = 0
  try:
    spread_pct = float(meta.get("spread_pct") or 0)
  except (TypeError, ValueError):
    spread_pct = 0.0

  if ltp > 0 and lot > 0:
    stop_pct = float(cfg.get("stop_pct", 0.05))
    target_pct = float(cfg.get("target_pct", 0.04))
    gain = ltp * lot * target_pct
    loss = ltp * lot * stop_pct
    estimated_cost_inr = ltp * lot * max(spread_pct, 0.0) / 100.0 + float(
      cfg.get("round_trip_cost_inr", estimated_cost_inr)
    )
  else:
    gain = float(cfg.get("base_gain_inr", 22))
    loss = float(cfg.get("base_loss_inr", 14))

  if regime.risk_score >= 70:
    loss *= 1.15
  if signal.setup_type in ("peak_reversal_fade", "level_rejection", "gap_fade"):
    gain *= 0.9
    loss *= 0.95

  ev = prob * gain - (1 - prob) * loss - estimated_cost_inr
  logs = [
    f"ev_prob={prob:.2f} gain={gain:.1f} loss={loss:.1f} cost={estimated_cost_inr:.1f} ev={ev:.2f}"
  ]
  return ev, logs


def ev_veto_reasons(
  signal: CandidateSignal,
  regime: MarketRegime,
  *,
  cfg: dict,
) -> list[str]:
  ev, _ = estimate_expected_value(signal, regime, cfg=cfg)
  min_ev = float(cfg.get("min_ev_inr", 0))
  if ev < min_ev:
    return [f"expected_value_below_threshold ev={ev:.2f}"]
  return []
