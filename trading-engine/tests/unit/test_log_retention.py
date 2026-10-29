from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from algoflat.journal.retention import (
    group_rows_by_ist_day,
    retention_cutoff,
    write_archive_records,
)

IST = ZoneInfo("Asia/Kolkata")


class FakeRow(dict):
    def keys(self):
        return super().keys()


def test_retention_cutoff_keeps_today_only():
    now = datetime(2026, 7, 24, 10, 30, tzinfo=IST)
    cutoff = retention_cutoff(keep_days=1, now=now)
    assert cutoff == datetime(2026, 7, 24, 0, 0, tzinfo=IST)


def test_retention_cutoff_keep_two_days():
    now = datetime(2026, 7, 24, 10, 30, tzinfo=IST)
    cutoff = retention_cutoff(keep_days=2, now=now)
    assert cutoff == datetime(2026, 7, 23, 0, 0, tzinfo=IST)


def test_group_rows_by_ist_day():
    rows = [
        FakeRow(ts=datetime(2026, 7, 23, 15, 0, tzinfo=IST), message="a"),
        FakeRow(ts=datetime(2026, 7, 24, 9, 0, tzinfo=IST), message="b"),
    ]
    grouped = group_rows_by_ist_day(rows)
    assert set(grouped) == {"2026-07-23", "2026-07-24"}
    assert grouped["2026-07-23"][0]["message"] == "a"


def test_write_archive_records_creates_gzip(tmp_path: Path):
    path = write_archive_records(
        tmp_path,
        "system_events",
        "2026-07-23",
        [{"id": "1", "message": "scan"}],
    )
    assert path.exists()
    assert path.name == "system_events.jsonl.gz"
