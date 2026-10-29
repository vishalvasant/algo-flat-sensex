"""FUT–cash basis helpers — dynamic threshold for monthly vs near expiry."""
from __future__ import annotations


def expected_max_basis_points(
  spot: float | None,
  *,
  dte_futures: int | None = None,
  cfg: dict | None = None,
) -> float:
  """Allow wider basis when the traded future is not the immediate near month."""
  cfg = cfg or {}
  floor = float(cfg.get("max_abs_basis_points", 30))
  if spot is None or spot <= 0:
    return floor

  pct = float(cfg.get("max_basis_pct_of_spot", 0.008))
  pct_limit = spot * pct

  if dte_futures is not None and dte_futures > 5:
    dte_mult = float(cfg.get("basis_dte_multiplier", 2.5))
    floor = max(floor, dte_futures * dte_mult)

  return max(floor, pct_limit)
