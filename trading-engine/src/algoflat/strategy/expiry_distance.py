"""Expiry distance (DTE) playbook — theta decay affects which setups work."""
from __future__ import annotations

from datetime import date
from typing import Any


def days_to_expiry(session_date: date, expiry_date: date) -> int:
  """Calendar days from session date to contract expiry (0 on expiry day)."""
  return max(0, (expiry_date - session_date).days)


def classify_expiry_bucket(
  dte: int,
  *,
  is_expiry_day: bool = False,
  cfg: dict | None = None,
) -> str:
  """
  Classify session by distance to weekly expiry.

  NIFTY weekly (Tuesday):
    Wed after roll  → DTE 6  (fresh_series)
    Thu/Fri         → DTE 4–5 (theta_week — premium decay / chop)
    Mon             → DTE 1  (pre_expiry — trend still works)
    Tue             → DTE 0  (expiry_day)
  """
  matrix = cfg or {}
  if is_expiry_day or dte <= 0:
    return "expiry_day"

  thresholds = matrix.get("thresholds") or {}
  fresh_min = int(thresholds.get("fresh_series_min_dte", 6))
  theta_max = int(thresholds.get("theta_week_max_dte", 5))
  theta_min = int(thresholds.get("theta_week_min_dte", 3))
  theta_late_max = int(thresholds.get("theta_week_late_max_dte", 4))
  pre_max = int(thresholds.get("pre_expiry_max_dte", 2))

  if dte >= fresh_min:
    return "fresh_series"
  if theta_min <= dte <= theta_max:
    if dte <= theta_late_max and matrix.get("buckets", {}).get("theta_week_late"):
      return "theta_week_late"
    return "theta_week"
  if 1 <= dte <= pre_max:
    return "pre_expiry"
  return "theta_week"


def expiry_distance_cfg(strategy: dict) -> dict[str, Any]:
  return strategy.get("expiry_distance") or {}


def allowed_strategies_for_expiry_bucket(
  bucket: str,
  cfg: dict | None = None,
) -> set[str] | None:
  matrix = cfg or {}
  buckets = matrix.get("buckets") or {}
  raw = buckets.get(bucket)
  if not raw:
    return None
  return set(raw)


def resolve_expiry_allowlist(
  *,
  session_date: date,
  expiry_date: date | None,
  is_expiry_day: bool = False,
  strategy: dict,
) -> tuple[set[str] | None, int | None, str, list[str]]:
  """Return (allowlist or None, dte, bucket_name, log lines)."""
  matrix = expiry_distance_cfg(strategy)
  if not matrix.get("enabled", False):
    return None, None, "disabled", ["expiry_distance=disabled"]

  if expiry_date is None:
    return None, None, "unknown", ["expiry_distance=no_expiry_date"]

  dte = days_to_expiry(session_date, expiry_date)
  bucket = classify_expiry_bucket(
    dte,
    is_expiry_day=is_expiry_day,
    cfg=matrix,
  )
  allowed = allowed_strategies_for_expiry_bucket(bucket, matrix)
  logs = [f"expiry_dte={dte}", f"expiry_bucket={bucket}"]
  if allowed is not None:
    logs.append(f"expiry_allowlist={sorted(allowed)}")
  return allowed, dte, bucket, logs
