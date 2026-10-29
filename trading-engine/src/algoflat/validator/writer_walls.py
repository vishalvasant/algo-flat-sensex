"""Block entries that run into call-writer resistance or put-writer support."""
from __future__ import annotations

from decimal import Decimal
from typing import Any


def writer_wall_rejection_reasons(
    *,
    side: str,
    spot: Decimal | None,
    chain: dict[str, Any] | None,
    setup_type: str = "",
    cfg: dict[str, Any] | None = None,
) -> list[str]:
    """Heavy call OI above spot is resistance. Heavy put OI below spot is support.

    A call is blocked while spot is still under a call-writer strike. A put is
    blocked while spot is still above a put-writer strike. Clearing the strike
    by ``break_buffer_points`` lifts the block.
    """
    cfg = cfg or {}
    if not cfg.get("enabled", False):
        return []
    if setup_type in set(cfg.get("exempt_setups") or []):
        return []
    if spot is None or not chain:
        return []

    rows = [row for row in (chain.get("strikes") or []) if isinstance(row, dict)]
    if not rows:
        return []

    proximity = float(cfg.get("proximity_points", 25))
    break_buffer = float(cfg.get("break_buffer_points", 8))
    min_ratio = float(cfg.get("min_oi_ratio", 0.55))
    min_oi = int(cfg.get("min_oi", 500_000))
    spot_f = float(spot)

    if side == "CE":
        return _call_writer_block(
            rows,
            spot_f,
            proximity=proximity,
            break_buffer=break_buffer,
            min_ratio=min_ratio,
            min_oi=min_oi,
        )
    if side == "PE":
        return _put_writer_block(
            rows,
            spot_f,
            proximity=proximity,
            break_buffer=break_buffer,
            min_ratio=min_ratio,
            min_oi=min_oi,
        )
    return []


def _call_writer_block(
    rows: list[dict[str, Any]],
    spot_f: float,
    *,
    proximity: float,
    break_buffer: float,
    min_ratio: float,
    min_oi: int,
) -> list[str]:
    max_oi = max(int(row.get("ce_oi") or 0) for row in rows)
    if max_oi < min_oi:
        return []
    threshold = max(min_oi, int(max_oi * min_ratio))
    overhead = []
    for row in rows:
        oi = int(row.get("ce_oi") or 0)
        if oi < threshold:
            continue
        strike = float(row["strike"])
        if strike + break_buffer <= spot_f:
            continue
        dist = strike - spot_f
        if dist > proximity:
            continue
        overhead.append((dist, strike))
    if not overhead:
        return []
    overhead.sort()
    return [f"ce_into_call_writer@{int(overhead[0][1])}"]


def _put_writer_block(
    rows: list[dict[str, Any]],
    spot_f: float,
    *,
    proximity: float,
    break_buffer: float,
    min_ratio: float,
    min_oi: int,
) -> list[str]:
    max_oi = max(int(row.get("pe_oi") or 0) for row in rows)
    if max_oi < min_oi:
        return []
    threshold = max(min_oi, int(max_oi * min_ratio))
    underneath = []
    for row in rows:
        oi = int(row.get("pe_oi") or 0)
        if oi < threshold:
            continue
        strike = float(row["strike"])
        if spot_f + break_buffer <= strike:
            continue
        dist = spot_f - strike
        if dist > proximity:
            continue
        underneath.append((dist, strike))
    if not underneath:
        return []
    underneath.sort()
    return [f"pe_into_put_writer@{int(underneath[0][1])}"]
