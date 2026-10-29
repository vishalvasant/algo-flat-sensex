"""FUT–cash basis gate — block entries when index and futures diverge too far."""
from __future__ import annotations

from algoflat.models.events import CandidateSignal
from algoflat.validator.basis_util import expected_max_basis_points


def basis_gate_rejection_reasons(
  signal: CandidateSignal,
  *,
  cfg: dict,
) -> list[str]:
  if not cfg.get("enabled", True):
    return []

  exempt = set(
    cfg.get("exempt_setups")
    or [
      "peak_reversal_fade",
      "level_rejection",
      "gap_fade",
      "trend_reversal_flip",
      "liquidity_sweep",
    ]
  )
  if signal.setup_type in exempt:
    return []

  feat = signal.feature_snapshot
  if feat is None:
    return []

  basis = (feat.extra or {}).get("basis_points")
  if basis is None:
    if cfg.get("reject_if_unknown", False):
      return ["basis_unavailable"]
    return []

  extra = feat.extra or {}
  spot = extra.get("fut_spot") or extra.get("cash_index_spot")
  dte_fut = extra.get("futures_dte")
  max_pts = expected_max_basis_points(
    float(spot) if spot is not None else None,
    dte_futures=int(dte_fut) if dte_fut is not None else None,
    cfg=cfg,
  )
  if abs(float(basis)) > max_pts:
    return ["basis_spread_too_wide"]
  return []
