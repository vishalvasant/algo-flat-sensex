"""Load live session journal rows from Postgres for replay."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from algoflat.models.events import CandidateSignal, FeatureSnapshot

IST = ZoneInfo("Asia/Kolkata")


def to_ist(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=ZoneInfo("UTC")).astimezone(IST)
    return ts.astimezone(IST)


@dataclass
class JournalSignal:
    id: UUID
    ts: datetime
    setup_type: str
    side: str
    instrument_token: str
    tsym: str
    strategy_version: str
    feature_snapshot: FeatureSnapshot
    scanner_metadata: dict[str, Any]
    confidence: int | None
    validator_passed: bool | None
    rejection_reasons: list[str]
    validator_version: str | None
    order_status: str | None
    order_filled: bool
    fill_price: float | None
    order_mode: str | None


@dataclass
class ScanEvent:
    id: UUID
    ts: datetime
    message: str
    metadata: dict[str, Any]


@dataclass
class JournalClosedTrade:
    entry_ts: datetime
    exit_ts: datetime
    tsym: str
    side: str
    setup_type: str
    pnl: float
    exit_reason: str
    mode: str
    quantity: int
    candidate_signal_id: UUID | None


@dataclass
class SessionFunnel:
    candidate_signals: int = 0
    validator_passed: int = 0
    orders_placed: int = 0
    orders_filled: int = 0
    closed_trades: int = 0
    entry_skipped_events: int = 0
    strategy_decisions: int = 0


@dataclass
class DayJournal:
    day: date
    underlying: str
    signals: list[JournalSignal] = field(default_factory=list)
    scans: list[ScanEvent] = field(default_factory=list)
    closed_trades: list[JournalClosedTrade] = field(default_factory=list)
    funnel: SessionFunnel = field(default_factory=SessionFunnel)


def _parse_json(val: Any) -> dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return {}
    return {}


class JournalLoader:
    """Fetch one session day from the algo-flat journal tables."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def load_day(self, day: date, underlying: str = "NIFTY") -> DayJournal:
        prefix = f"{underlying.upper()}%"
        signals = await self._load_signals(day, prefix)
        scans = await self._load_scans(day)
        trades = await self._load_closed_trades(day, prefix)
        funnel = await self._load_funnel(day, prefix)
        return DayJournal(
            day=day,
            underlying=underlying.upper(),
            signals=signals,
            scans=scans,
            closed_trades=trades,
            funnel=funnel,
        )

    async def _load_signals(self, day: date, prefix: str) -> list[JournalSignal]:
        rows = await self._conn.fetch(
            """
            SELECT
                cs.id,
                cs.ts,
                cs.setup_type,
                cs.side,
                cs.instrument_token,
                cs.tsym,
                cs.strategy_version,
                cs.feature_snapshot,
                cs.scanner_metadata,
                vr.passed AS validator_passed,
                vr.rejection_reasons,
                vr.validator_version,
                o.status AS order_status,
                o.fill_price,
                o.ts_filled,
                o.mode AS order_mode
            FROM candidate_signals cs
            LEFT JOIN validation_results vr ON vr.candidate_signal_id = cs.id
            LEFT JOIN orders o ON o.candidate_signal_id = cs.id
            WHERE (cs.ts AT TIME ZONE 'Asia/Kolkata')::date = $1::date
              AND cs.tsym LIKE $2
            ORDER BY cs.ts ASC
            """,
            day,
            prefix,
        )
        out: list[JournalSignal] = []
        for r in rows:
            feat_raw = _parse_json(r["feature_snapshot"])
            feat = FeatureSnapshot.model_validate(feat_raw)
            meta = _parse_json(r["scanner_metadata"])
            conf = meta.get("confidence")
            if conf is None and r.get("confidence"):
                conf = r["confidence"]
            out.append(
                JournalSignal(
                    id=r["id"],
                    ts=to_ist(r["ts"]),
                    setup_type=r["setup_type"],
                    side=r["side"],
                    instrument_token=str(r["instrument_token"]),
                    tsym=r["tsym"],
                    strategy_version=r["strategy_version"],
                    feature_snapshot=feat,
                    scanner_metadata=meta,
                    confidence=int(conf) if conf is not None else None,
                    validator_passed=r["validator_passed"],
                    rejection_reasons=list(r["rejection_reasons"] or []),
                    validator_version=r["validator_version"],
                    order_status=r["order_status"],
                    order_filled=r["ts_filled"] is not None,
                    fill_price=float(r["fill_price"]) if r["fill_price"] is not None else None,
                    order_mode=r["order_mode"],
                )
            )
        return out

    async def _load_scans(self, day: date) -> list[ScanEvent]:
        rows = await self._conn.fetch(
            """
            SELECT id, ts, message, metadata
            FROM system_events
            WHERE event_type = 'strategy_decision'
              AND (ts AT TIME ZONE 'Asia/Kolkata')::date = $1::date
            ORDER BY ts ASC
            """,
            day,
        )
        return [
            ScanEvent(
                id=r["id"],
                ts=to_ist(r["ts"]),
                message=r["message"] or "",
                metadata=_parse_json(r["metadata"]),
            )
            for r in rows
        ]

    async def _load_closed_trades(self, day: date, prefix: str) -> list[JournalClosedTrade]:
        rows = await self._conn.fetch(
            """
            SELECT
                ct.entry_ts,
                ct.exit_ts,
                ct.pnl,
                ct.exit_reason,
                ct.setup_type,
                ct.mode,
                ct.candidate_signal_id,
                p.tsym,
                p.side,
                p.quantity
            FROM closed_trades ct
            JOIN positions p ON p.id = ct.position_id
            WHERE (ct.entry_ts AT TIME ZONE 'Asia/Kolkata')::date = $1::date
              AND p.tsym LIKE $2
            ORDER BY ct.entry_ts ASC
            """,
            day,
            prefix,
        )
        return [
            JournalClosedTrade(
                entry_ts=to_ist(r["entry_ts"]),
                exit_ts=to_ist(r["exit_ts"]),
                tsym=r["tsym"],
                side=r["side"],
                setup_type=r["setup_type"],
                pnl=float(r["pnl"]),
                exit_reason=r["exit_reason"],
                mode=r["mode"],
                quantity=int(r["quantity"]),
                candidate_signal_id=r["candidate_signal_id"],
            )
            for r in rows
        ]

    async def _load_funnel(self, day: date, prefix: str) -> SessionFunnel:
        signals = await self._conn.fetchval(
            """
            SELECT COUNT(*) FROM candidate_signals
            WHERE (ts AT TIME ZONE 'Asia/Kolkata')::date = $1::date
              AND tsym LIKE $2
            """,
            day,
            prefix,
        )
        passed = await self._conn.fetchval(
            """
            SELECT COUNT(*) FROM validation_results vr
            JOIN candidate_signals cs ON cs.id = vr.candidate_signal_id
            WHERE (cs.ts AT TIME ZONE 'Asia/Kolkata')::date = $1::date
              AND cs.tsym LIKE $2
              AND vr.passed = true
            """,
            day,
            prefix,
        )
        orders = await self._conn.fetchval(
            """
            SELECT COUNT(*) FROM orders o
            JOIN candidate_signals cs ON cs.id = o.candidate_signal_id
            WHERE (cs.ts AT TIME ZONE 'Asia/Kolkata')::date = $1::date
              AND cs.tsym LIKE $2
            """,
            day,
            prefix,
        )
        filled = await self._conn.fetchval(
            """
            SELECT COUNT(*) FROM orders o
            JOIN candidate_signals cs ON cs.id = o.candidate_signal_id
            WHERE (cs.ts AT TIME ZONE 'Asia/Kolkata')::date = $1::date
              AND cs.tsym LIKE $2
              AND o.ts_filled IS NOT NULL
            """,
            day,
            prefix,
        )
        closed = await self._conn.fetchval(
            """
            SELECT COUNT(*) FROM closed_trades ct
            JOIN positions p ON p.id = ct.position_id
            WHERE (ct.entry_ts AT TIME ZONE 'Asia/Kolkata')::date = $1::date
              AND p.tsym LIKE $2
            """,
            day,
            prefix,
        )
        skipped = await self._conn.fetchval(
            """
            SELECT COUNT(*) FROM system_events
            WHERE event_type = 'entry_skipped'
              AND (ts AT TIME ZONE 'Asia/Kolkata')::date = $1::date
            """,
            day,
        )
        scans = await self._conn.fetchval(
            """
            SELECT COUNT(*) FROM system_events
            WHERE event_type = 'strategy_decision'
              AND (ts AT TIME ZONE 'Asia/Kolkata')::date = $1::date
            """,
            day,
        )
        return SessionFunnel(
            candidate_signals=int(signals or 0),
            validator_passed=int(passed or 0),
            orders_placed=int(orders or 0),
            orders_filled=int(filled or 0),
            closed_trades=int(closed or 0),
            entry_skipped_events=int(skipped or 0),
            strategy_decisions=int(scans or 0),
        )
