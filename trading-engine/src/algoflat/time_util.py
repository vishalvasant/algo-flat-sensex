"""IST clock helpers for live trading (NSE session)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    """Current wall-clock time in Asia/Kolkata."""
    return datetime.now(IST)


def ensure_ist(ts: datetime) -> datetime:
    """Normalize any aware datetime to IST (naive values treated as IST)."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=IST)
    return ts.astimezone(IST)


def age_seconds(since: datetime | None, *, now: datetime | None = None) -> float | None:
    """Seconds elapsed since `since` (both normalized to IST)."""
    if since is None:
        return None
    ref = now or now_ist()
    return (ref - ensure_ist(since)).total_seconds()


def from_epoch_ms(ms: int) -> datetime:
    """Upstox feed ltt fields are epoch milliseconds (UTC instant → IST)."""
    return datetime.fromtimestamp(ms / 1000.0, tz=IST)
