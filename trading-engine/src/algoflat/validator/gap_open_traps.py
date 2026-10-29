"""Gap-open trap filters — block trend entries against gap direction until OR breaks."""
from __future__ import annotations

from decimal import Decimal

from algoflat.models.events import CandidateSignal


def gap_open_trap_reasons(
    signal: CandidateSignal,
    trap_cfg: dict,
) -> list[str]:
    gap_cfg = trap_cfg.get("gap_open") or {}
    if not gap_cfg.get("enabled", True):
        return []

    feat = signal.feature_snapshot
    extra = feat.extra or {}
    gap = extra.get("gap_points")
    if gap is None:
        return []

    min_gap = float(gap_cfg.get("min_gap_points", 35))
    setup = signal.setup_type
    blocked = set(gap_cfg.get("trend_blocked_setups") or [])
    if setup not in blocked:
        return []

    spot = feat.nifty_spot
    or_h, or_l = extra.get("or_high"), extra.get("or_low")
    reasons: list[str] = []
    # Historical bug (pre-2026-09-11): used the opposite OR bound and inverted
    # comparisons, which blocked CE for nearly the entire gap-down session.
    legacy = bool(gap_cfg.get("legacy_wrong_or_bounds", False))

    if float(gap) <= -min_gap and signal.side == "CE":
        if legacy:
            if or_l is not None and spot is not None and spot > Decimal(str(or_l)):
                reasons.append("gap_down_block_ce_until_or_break")
        else:
            # Gap-down: allow CE only after breaking OR high (recovery confirmation).
            if or_h is not None and spot is not None and spot <= Decimal(str(or_h)):
                reasons.append("gap_down_block_ce_until_or_break")

    if float(gap) >= min_gap and signal.side == "PE":
        if legacy:
            if or_h is not None and spot is not None and spot < Decimal(str(or_h)):
                reasons.append("gap_up_block_pe_until_or_break")
        else:
            # Gap-up: allow PE only after breaking OR low.
            if or_l is not None and spot is not None and spot >= Decimal(str(or_l)):
                reasons.append("gap_up_block_pe_until_or_break")

    return reasons
