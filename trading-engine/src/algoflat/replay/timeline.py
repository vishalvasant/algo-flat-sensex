"""Exit tick timeline between journal scan events."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from algoflat.models.events import Candle
from algoflat.replay.loader import ScanEvent
from algoflat.replay.scheduler import ReplaySchedule

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class ExitTick:
    ts: datetime
    spot: Decimal
    bar: Candle
    bar_frac: Decimal


@dataclass(frozen=True)
class ScanTick:
    ts: datetime
    event: ScanEvent


TimelineItem = tuple[str, ExitTick | ScanTick]


def _norm(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def iter_exit_ticks(
    m1_all: list[Candle],
    start: datetime,
    end: datetime,
    *,
    exit_sec: int,
    honest: bool,
) -> Iterator[ExitTick]:
    """Yield fine-grained exit evaluation ticks in (start, end]."""
    from day_backtest import _live_parity_waypoints  # noqa: E402

    start_n = _norm(start)
    end_n = _norm(end)
    if end_n <= start_n:
        return

    exit_sec = max(1, int(exit_sec))
    for bar in m1_all:
        bar_end = bar.ts + timedelta(minutes=1)
        if bar_end <= start_n:
            continue
        if bar.ts >= end_n:
            break
        prior_m1 = [c for c in m1_all if c.ts < bar.ts]
        for ts, spot, _allow in _live_parity_waypoints(
            bar,
            exit_sec=exit_sec,
            scan_sec=exit_sec,
            prior_m1=prior_m1,
            honest=honest,
        ):
            ts_n = _norm(ts)
            if start_n < ts_n <= end_n:
                frac = Decimal(str(min(1.0, max(0.0, (ts_n - bar.ts).total_seconds() / 60.0))))
                yield ExitTick(ts=ts_n, spot=spot, bar=bar, bar_frac=frac)


def build_timeline(
    schedule: ReplaySchedule,
    m1_all: list[Candle],
    *,
    day: date,
    exit_sec: int,
    honest: bool,
    session_start: time | None = None,
    session_end: time | None = None,
) -> list[TimelineItem]:
    """Chronological exit ticks + journal scan events for one session."""
    start_t = session_start or time(9, 15)
    end_t = session_end or time(15, 30)
    session_open = datetime.combine(day, start_t, tzinfo=IST).astimezone(timezone.utc)
    session_close = datetime.combine(day, end_t, tzinfo=IST).astimezone(timezone.utc)

    scans = sorted(schedule.events, key=lambda e: _norm(e.ts))
    boundaries: list[tuple[datetime, ScanEvent | None]] = [(session_open, None)]
    for ev in scans:
        boundaries.append((_norm(ev.ts), ev))
    boundaries.append((session_close, None))

    items: list[TimelineItem] = []
    for i in range(1, len(boundaries)):
        prev_ts, _ = boundaries[i - 1]
        cur_ts, scan_ev = boundaries[i]
        for tick in iter_exit_ticks(
            m1_all, prev_ts, cur_ts, exit_sec=exit_sec, honest=honest
        ):
            items.append(("exit", tick))
        if scan_ev is not None:
            items.append(("scan", ScanTick(ts=cur_ts, event=scan_ev)))

    items.sort(key=lambda x: _norm(x[1].ts))  # type: ignore[union-attr]
    return items
