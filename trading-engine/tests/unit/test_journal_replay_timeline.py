"""Unit tests for journal replay timeline builder."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from algoflat.models.events import Candle, CandleInterval
from algoflat.replay.loader import ScanEvent
from algoflat.replay.scheduler import ReplaySchedule
from algoflat.replay.timeline import build_timeline


def _bar(ts: datetime) -> Candle:
    return Candle(
        instrument_token="NIFTY",
        ts=ts,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=100,
        interval=CandleInterval.M1,
    )


def test_build_timeline_includes_scan_and_exit_ticks():
    day = date(2026, 7, 28)
    t0 = datetime(2026, 7, 28, 4, 45, tzinfo=timezone.utc)
    m1 = [_bar(t0), _bar(datetime(2026, 7, 28, 4, 46, tzinfo=timezone.utc))]
    scan_ts = datetime(2026, 7, 28, 4, 45, 30, tzinfo=timezone.utc)
    schedule = ReplaySchedule(
        day=day,
        source="journal",
        events=[
            ScanEvent(id=uuid4(), ts=scan_ts, message="scan", metadata={}),
        ],
    )
    items = build_timeline(
        schedule,
        m1,
        day=day,
        exit_sec=30,
        honest=False,
    )
    kinds = [k for k, _ in items]
    assert "exit" in kinds
    assert "scan" in kinds
    assert kinds.count("scan") == 1
