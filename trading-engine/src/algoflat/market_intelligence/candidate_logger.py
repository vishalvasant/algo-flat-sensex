"""Phase 12 — Candidate feature logging for future ML training."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from algoflat.models.events import CandidateSignal, FeatureSnapshot, MarketRegime, SystemEvent

logger = structlog.get_logger(__name__)


def build_candidate_record(
  signal: CandidateSignal | None,
  *,
  features: FeatureSnapshot,
  regime: MarketRegime,
  mi_snapshot: dict[str, Any],
  ml_prediction: dict[str, Any] | None = None,
  approved: bool = False,
  veto_reasons: list[str] | None = None,
) -> dict[str, Any]:
  extra = features.extra or {}
  mi = mi_snapshot or {}
  return {
    "ts": datetime.now(tz=timezone.utc).isoformat(),
    "strategy": signal.setup_type if signal else None,
    "direction": signal.side if signal else None,
    "regime": regime.primary,
    "confidence": signal.confidence if signal else None,
    "approved": approved,
    "veto_reasons": veto_reasons or [],
    "nifty_spot": extra.get("cash_index_spot"),
    "nifty_fut": extra.get("fut_spot"),
    "spot_fut": mi.get("fut") or extra.get("spot_fut"),
    "vix": mi.get("vix"),
    "breadth": mi.get("breadth"),
    "sectors": mi.get("sectors"),
    "global": mi.get("global"),
    "ce_pe": mi.get("ce_pe"),
    "premium": mi.get("premium"),
    "divergence": mi.get("divergence"),
    "ml_prediction": ml_prediction,
    "data_quality": mi.get("data_quality"),
  }


async def log_candidate(journal, record: dict[str, Any], *, enabled: bool = True) -> None:
  if not enabled:
    return
  try:
    await journal.write_system_event(
      SystemEvent(
        event_type="mi_candidate_log",
        ts=datetime.now(tz=timezone.utc),
        severity="debug",
        message=f"MI candidate {record.get('strategy')} {record.get('direction')}",
        metadata=record,
      )
    )
  except Exception:
    logger.exception("mi_candidate_log_failed")
