"""Normalized market tick metadata — Phase 1 (New-Engine spec §4)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class DataValidity(str, Enum):
  OK = "ok"
  STALE = "stale"
  MISSING = "missing"
  INVALID = "invalid"


@dataclass(frozen=True)
class MarketTickMeta:
  """Single observable with provenance and freshness."""

  source: str
  instrument: str
  timestamp: datetime
  value: float | None
  freshness_sec: float | None = None
  validity: DataValidity = DataValidity.OK
  extra: dict[str, Any] = field(default_factory=dict)

  def to_dict(self) -> dict[str, Any]:
    return {
      "source": self.source,
      "instrument": self.instrument,
      "timestamp": self.timestamp.isoformat(),
      "value": self.value,
      "freshness_sec": self.freshness_sec,
      "validity": self.validity.value,
      **self.extra,
    }


def tick_meta(
  *,
  source: str,
  instrument: str,
  value: float | None,
  ts: datetime | None = None,
  freshness_sec: float | None = None,
  validity: DataValidity = DataValidity.OK,
  **extra: Any,
) -> MarketTickMeta:
  now = datetime.now(tz=timezone.utc)
  stamp = ts or now
  if freshness_sec is None and ts is not None:
    freshness_sec = max(0.0, (now - ts).total_seconds())
  return MarketTickMeta(
    source=source,
    instrument=instrument,
    timestamp=stamp,
    value=value,
    freshness_sec=freshness_sec,
    validity=validity,
    extra=extra,
  )
