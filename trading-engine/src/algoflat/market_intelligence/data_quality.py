"""Phase 33 — Data quality rollup for market intelligence."""
from __future__ import annotations

from typing import Any


def assess_data_quality(snapshot: dict[str, Any]) -> dict[str, Any]:
  critical = ["spot", "fut", "premium"]
  failures: list[str] = []
  for key in critical:
    block = snapshot.get(key) or {}
    if not block.get("available", False):
      failures.append(f"missing_{key}")

  state = "DATA_QUALITY_OK" if not failures else "DATA_QUALITY_DEGRADED"
  if failures and all(not (snapshot.get(k) or {}).get("available") for k in critical):
    state = "DATA_QUALITY_INVALID"

  return {
    "state": state,
    "failures": failures,
    "global_contextual_only": (snapshot.get("global") or {}).get("contextual_only", True),
  }
