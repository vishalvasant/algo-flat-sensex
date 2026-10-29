"""Expiry-day cautious entry gates (replaces hard block-all)."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from algoflat.models.events import CandidateSignal

IST = ZoneInfo("Asia/Kolkata")


def expiry_cautious_rejection_reasons(
    signal: CandidateSignal,
    cautious_cfg: dict,
    *,
    now: datetime | None = None,
) -> list[str]:
    if not cautious_cfg.get("enabled", False):
        return []

    reasons: list[str] = []
    ist_now = (now or datetime.now(IST)).astimezone(IST)
    entry_end = cautious_cfg.get("entry_end_time")
    if entry_end:
        end = time.fromisoformat(str(entry_end))
        if ist_now.time() > end:
            reasons.append("expiry_entry_window_closed")
            return reasons
    if "min_confidence" in cautious_cfg:
        min_conf = int(cautious_cfg["min_confidence"])
        conf = int(signal.confidence or 0)
        if conf < min_conf:
            reasons.append("expiry_confidence_too_low")

    allowed = cautious_cfg.get("allowed_setups")
    if isinstance(allowed, list) and len(allowed) > 0:
        if signal.setup_type not in allowed:
            reasons.append("expiry_setup_not_allowed")

    blocked = cautious_cfg.get("blocked_setups") or []
    if blocked and signal.setup_type in blocked:
        reasons.append("expiry_setup_blocked")

    if "min_mtf_score" in cautious_cfg:
        feat = signal.feature_snapshot
        extra = feat.extra or {}
        min_mtf = int(cautious_cfg["min_mtf_score"])
        if signal.side == "CE":
            mtf_raw = extra.get("mtf_score_ce")
        else:
            mtf_raw = extra.get("mtf_score_pe")
        if mtf_raw is not None and int(mtf_raw) < min_mtf:
            reasons.append("expiry_mtf_too_low")

    return reasons
