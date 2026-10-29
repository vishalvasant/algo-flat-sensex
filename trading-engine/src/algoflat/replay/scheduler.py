"""Ordered scan timestamps from live journal."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from algoflat.replay.loader import DayJournal, ScanEvent

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class ReplaySchedule:
    day: date
    source: str
    events: list[ScanEvent]

    def timestamps(self) -> list[datetime]:
        return [e.ts for e in self.events]


def build_schedule(
    journal: DayJournal,
    *,
    scan_interval_sec: int = 10,
) -> ReplaySchedule:
    """Use journal strategy_decision times, or synthetic session grid if empty."""
    if journal.scans:
        return ReplaySchedule(day=journal.day, source="journal", events=journal.scans)

    start = datetime.combine(journal.day, time(9, 15), tzinfo=IST)
    end = datetime.combine(journal.day, time(15, 29), tzinfo=IST)
    events: list[ScanEvent] = []
    cur = start
    from uuid import uuid4

    while cur <= end:
        events.append(
            ScanEvent(
                id=uuid4(),
                ts=cur,
                message="synthetic_scan",
                metadata={"scan_interval_seconds": scan_interval_sec},
            )
        )
        cur += timedelta(seconds=scan_interval_sec)
    return ReplaySchedule(day=journal.day, source="synthetic", events=events)
