"""Time-of-day cautious entry gates (open / mid / late session)."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from algoflat.models.events import Candle, CandidateSignal

IST = ZoneInfo("Asia/Kolkata")


def _parse_time(raw: str) -> time:
    return time.fromisoformat(str(raw))


def _parse_windows(raw: list | None) -> list[tuple[time, time]]:
    out: list[tuple[time, time]] = []
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        out.append((_parse_time(row.get("start", "00:00")), _parse_time(row.get("end", "23:59"))))
    return out


def _in_windows(clock: time, windows: list[tuple[time, time]]) -> bool:
    return any(start <= clock < end for start, end in windows)


def nifty_5m_range(spot_m1: list[Candle] | None, *, now: datetime | None = None) -> Decimal | None:
    """High-low span of the last 5 closed 1m Nifty bars."""
    if not spot_m1:
        return None
    when = now or datetime.now(tz=IST)
    closed = [b for b in spot_m1 if b.ts + timedelta(minutes=1) <= when]
    if len(closed) < 5:
        # Fall back to the newest bars we have (live forming series).
        closed = list(spot_m1)
    if len(closed) < 5:
        return None
    window = closed[-5:]
    return max(b.high for b in window) - min(b.low for b in window)


def tod_dead_zone_rejection_reasons(
    cfg: dict,
    *,
    now: datetime | None = None,
    spot_m1: list[Candle] | None = None,
) -> list[str]:
    """Block entries in historically dead IST windows; soft windows need Nifty thrust."""
    if not cfg.get("enabled", False):
        return []
    clock = (now or datetime.now(IST)).astimezone(IST).time()
    hard = _parse_windows(cfg.get("hard_windows"))
    if _in_windows(clock, hard):
        return ["tod_hard_dead_zone"]
    soft = _parse_windows(cfg.get("soft_windows"))
    if _in_windows(clock, soft):
        min_range = Decimal(str(cfg.get("soft_min_nifty_5m_range", 15) or 15))
        rng = nifty_5m_range(spot_m1, now=now)
        if rng is None or rng < min_range:
            return ["tod_soft_no_thrust"]
    return []


def active_phase(cfg: dict, clock: time) -> tuple[str, dict] | tuple[None, None]:
    phases = cfg.get("phases") or {}
    for name, phase in phases.items():
        if not isinstance(phase, dict):
            continue
        start = _parse_time(phase.get("start", "00:00"))
        end = _parse_time(phase.get("end", "23:59"))
        if start <= clock <= end:
            return str(name), phase
    return None, None


def time_of_day_lot_cap(cfg: dict, now: datetime | None = None) -> int | None:
    if not cfg.get("enabled", False):
        return None
    clock = (now or datetime.now(IST)).astimezone(IST).time()
    _, phase = active_phase(cfg, clock)
    if not phase:
        return None
    raw = phase.get("max_lots")
    if raw is None:
        return None
    return max(1, int(raw))


def time_of_day_cautious_rejection_reasons(
    signal: CandidateSignal,
    cautious_cfg: dict,
    *,
    now: datetime | None = None,
) -> list[str]:
    if not cautious_cfg.get("enabled", False):
        return []

    clock = (now or datetime.now(IST)).astimezone(IST).time()
    phase_name, phase = active_phase(cautious_cfg, clock)
    if not phase:
        return []

    reasons: list[str] = []
    prefix = f"tod_{phase_name}"

    min_conf = phase.get("min_confidence")
    if min_conf is not None:
        conf = int(signal.confidence or 0)
        if conf < int(min_conf):
            reasons.append(f"{prefix}_confidence_too_low")

    allowed = phase.get("allowed_setups")
    if isinstance(allowed, list) and len(allowed) > 0:
        if signal.setup_type not in allowed:
            reasons.append(f"{prefix}_setup_not_allowed")

    blocked = phase.get("blocked_setups") or []
    if signal.setup_type in blocked:
        reasons.append(f"{prefix}_setup_blocked")

    min_mtf = phase.get("min_mtf_score")
    if min_mtf is not None:
        extra = signal.feature_snapshot.extra or {}
        mtf_raw = extra.get("mtf_score_ce" if signal.side == "CE" else "mtf_score_pe")
        if mtf_raw is not None and int(mtf_raw) < int(min_mtf):
            reasons.append(f"{prefix}_mtf_too_low")

    return reasons
