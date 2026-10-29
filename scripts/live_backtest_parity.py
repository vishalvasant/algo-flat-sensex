#!/usr/bin/env python3
"""Compare live journal (DB) vs honest day_backtest replay for parity validation.

Usage (from repo root):
  CONFIG_DIR=./config PYTHONPATH=trading-engine/src \\
    trading-engine/.venv/bin/python scripts/live_backtest_parity.py --date 2026-07-28

Requires Postgres with today's live session data (candidate_signals, closed_trades).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading-engine" / "src"))
os.chdir(ROOT)
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
os.environ.setdefault("STRUCTLOG_LEVEL", "WARNING")

IST = ZoneInfo("Asia/Kolkata")


def _option_side_from_tsym(tsym: str) -> str:
    import re

    m = re.search(r"([CP])(\d+)\s*$", tsym.upper())
    if not m:
        return "CE"
    return "CE" if m.group(1) == "C" else "PE"


def _parse_day(raw: str) -> date:
    return date.fromisoformat(raw)


def _to_ist(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc).astimezone(IST)
    return ts.astimezone(IST)


def _trade_key(tsym: str, side: str) -> str:
    return f"{tsym}|{side.upper()}"


@dataclass
class TradeRow:
    source: str
    tsym: str
    side: str
    setup_type: str
    entry_ist: str
    exit_ist: str
    entry_epoch: float
    lots: int
    qty: int
    pnl: float
    exit_reason: str
    mode: str = ""

    @property
    def key(self) -> str:
        return _trade_key(self.tsym, self.side)


def _closed_to_row(source: str, t: Any, *, mode: str = "") -> TradeRow:
    if isinstance(t, dict):
        entry_ts = datetime.fromisoformat(t["entry_ist"].replace(" ", "T"))
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.replace(tzinfo=IST)
        exit_ts = datetime.fromisoformat(t["exit_ist"].replace(" ", "T"))
        if exit_ts.tzinfo is None:
            exit_ts = exit_ts.replace(tzinfo=IST)
        lots = int(t.get("lots") or 0)
        lot_size = int(t.get("lot_size") or 65)
        return TradeRow(
            source=source,
            tsym=str(t.get("tsym") or ""),
            side=str(t.get("side") or ""),
            setup_type=str(t.get("strategy") or t.get("setup_type") or ""),
            entry_ist=entry_ts.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S"),
            exit_ist=exit_ts.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S"),
            entry_epoch=entry_ts.timestamp(),
            lots=lots,
            qty=int(t.get("qty") or lots * lot_size),
            pnl=float(t.get("pnl") or 0),
            exit_reason=str(t.get("exit_reason") or ""),
            mode=mode,
        )
    entry_ts = _to_ist(t.entry_ts)
    exit_ts = _to_ist(t.exit_ts)
    return TradeRow(
        source=source,
        tsym=str(t.tsym or ""),
        side=str(t.side or ""),
        setup_type=str(t.strategy or ""),
        entry_ist=entry_ts.strftime("%Y-%m-%d %H:%M:%S"),
        exit_ist=exit_ts.strftime("%Y-%m-%d %H:%M:%S"),
        entry_epoch=entry_ts.timestamp(),
        lots=int(t.lots),
        qty=int(t.lots) * int(t.lot_size),
        pnl=float(t.pnl),
        exit_reason=str(t.exit_reason or ""),
        mode=mode,
    )


def _match_trades(
    live: list[TradeRow],
    backtest: list[TradeRow],
    *,
    tolerance_sec: float,
) -> tuple[list[dict], list[TradeRow], list[TradeRow]]:
    """Return (matched_pairs, live_only, backtest_only)."""
    used_bt: set[int] = set()
    matched: list[dict] = []
    live_only: list[TradeRow] = []

    for lt in live:
        best_i: int | None = None
        best_dt = tolerance_sec + 1
        for i, bt in enumerate(backtest):
            if i in used_bt:
                continue
            if lt.key != bt.key:
                continue
            dt = abs(lt.entry_epoch - bt.entry_epoch)
            if dt <= tolerance_sec and dt < best_dt:
                best_dt = dt
                best_i = i
        if best_i is None:
            live_only.append(lt)
        else:
            used_bt.add(best_i)
            bt = backtest[best_i]
            matched.append(
                {
                    "tsym": lt.tsym,
                    "side": lt.side,
                    "setup_type": lt.setup_type,
                    "live_entry": lt.entry_ist,
                    "bt_entry": bt.entry_ist,
                    "entry_delta_sec": round(abs(lt.entry_epoch - bt.entry_epoch), 1),
                    "live_pnl": lt.pnl,
                    "bt_pnl": bt.pnl,
                    "live_lots": lt.lots,
                    "bt_lots": bt.lots,
                    "live_mode": lt.mode,
                }
            )

    bt_only = [bt for i, bt in enumerate(backtest) if i not in used_bt]
    return matched, live_only, bt_only


def _nearest_decision(
    decisions: list[dict[str, Any]],
    *,
    ts_epoch: float,
    setup_type: str,
    side: str,
    tsym: str,
    window_sec: float,
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_dt = window_sec + 1
    for d in decisions:
        if d.get("setup_type") != setup_type or d.get("side") != side:
            continue
        if tsym and d.get("tsym") and d["tsym"] != tsym:
            continue
        dt = abs(float(d["ts_epoch"]) - ts_epoch)
        if dt <= window_sec and dt < best_dt:
            best_dt = dt
            best = d
    return best


async def _fetch_live_signals(conn, day: date, underlying: str) -> list[dict[str, Any]]:
    prefix = f"{underlying.upper()}%"
    rows = await conn.fetch(
        """
        SELECT
            cs.id,
            cs.ts,
            cs.setup_type,
            cs.side,
            cs.tsym,
            cs.instrument_token,
            cs.scanner_metadata,
            vr.passed AS validator_passed,
            vr.rejection_reasons,
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
    out: list[dict[str, Any]] = []
    for r in rows:
        ts = _to_ist(r["ts"])
        meta = r["scanner_metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        out.append(
            {
                "id": str(r["id"]),
                "ts_ist": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "ts_epoch": ts.timestamp(),
                "setup_type": r["setup_type"],
                "side": r["side"],
                "tsym": r["tsym"],
                "validator_passed": r["validator_passed"],
                "rejection_reasons": list(r["rejection_reasons"] or []),
                "order_status": r["order_status"],
                "order_filled": r["ts_filled"] is not None,
                "fill_price": float(r["fill_price"]) if r["fill_price"] is not None else None,
                "order_mode": r["order_mode"],
            }
        )
    return out


async def _fetch_live_closed_breakdown(conn, day: date, underlying: str) -> dict[str, int]:
    prefix = f"{underlying.upper()}%"
    rows = await conn.fetch(
        """
        SELECT ct.setup_type, COUNT(*)::int AS n
        FROM closed_trades ct
        JOIN positions p ON p.id = ct.position_id
        WHERE (ct.entry_ts AT TIME ZONE 'Asia/Kolkata')::date = $1::date
          AND p.tsym LIKE $2
        GROUP BY ct.setup_type
        ORDER BY n DESC
        """,
        day,
        prefix,
    )
    return {str(r["setup_type"]): int(r["n"]) for r in rows}


async def _fetch_live_closed(conn, day: date, underlying: str, *, algo_only: bool) -> list[TradeRow]:
    prefix = f"{underlying.upper()}%"
    algo_clause = "AND ct.candidate_signal_id IS NOT NULL" if algo_only else ""
    rows = await conn.fetch(
        f"""
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
          {algo_clause}
        ORDER BY ct.entry_ts ASC
        """,
        day,
        prefix,
    )
    trades: list[TradeRow] = []
    for r in rows:
        entry_ts = _to_ist(r["entry_ts"])
        exit_ts = _to_ist(r["exit_ts"])
        qty = int(r["quantity"])
        lot_size = 65
        lots = max(1, qty // lot_size) if qty >= lot_size else 1
        trades.append(
            TradeRow(
                source="live_db",
                tsym=str(r["tsym"]),
                side=_option_side_from_tsym(str(r["tsym"])),
                setup_type=str(r["setup_type"] or ""),
                entry_ist=entry_ts.strftime("%Y-%m-%d %H:%M:%S"),
                exit_ist=exit_ts.strftime("%Y-%m-%d %H:%M:%S"),
                entry_epoch=entry_ts.timestamp(),
                lots=lots,
                qty=qty,
                pnl=float(r["pnl"]),
                exit_reason=str(r["exit_reason"] or ""),
                mode=str(r["mode"] or ""),
            )
        )
    return trades


async def _fetch_funnel_counts(conn, day: date, underlying: str) -> dict[str, int]:
    prefix = f"{underlying.upper()}%"
    signals = await conn.fetchval(
        """
        SELECT COUNT(*) FROM candidate_signals cs
        WHERE (cs.ts AT TIME ZONE 'Asia/Kolkata')::date = $1::date
          AND cs.tsym LIKE $2
        """,
        day,
        prefix,
    )
    passed = await conn.fetchval(
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
    orders = await conn.fetchval(
        """
        SELECT COUNT(*) FROM orders o
        JOIN candidate_signals cs ON cs.id = o.candidate_signal_id
        WHERE (cs.ts AT TIME ZONE 'Asia/Kolkata')::date = $1::date
          AND cs.tsym LIKE $2
        """,
        day,
        prefix,
    )
    filled = await conn.fetchval(
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
    closed = await conn.fetchval(
        """
        SELECT COUNT(*) FROM closed_trades ct
        JOIN positions p ON p.id = ct.position_id
        WHERE (ct.entry_ts AT TIME ZONE 'Asia/Kolkata')::date = $1::date
          AND p.tsym LIKE $2
        """,
        day,
        prefix,
    )
    skipped = await conn.fetchval(
        """
        SELECT COUNT(*) FROM system_events
        WHERE event_type = 'entry_skipped'
          AND (ts AT TIME ZONE 'Asia/Kolkata')::date = $1::date
        """,
        day,
    )
    return {
        "candidate_signals": int(signals or 0),
        "validator_passed": int(passed or 0),
        "orders_placed": int(orders or 0),
        "orders_filled": int(filled or 0),
        "closed_trades": int(closed or 0),
        "entry_skipped_events": int(skipped or 0),
    }


def _compare_signals(
    live_signals: list[dict[str, Any]],
    bt_decisions: list[dict[str, Any]],
    *,
    window_sec: float,
) -> dict[str, Any]:
    matched: list[dict] = []
    live_only: list[dict] = []
    validator_mismatch: list[dict] = []

    used_bt: set[int] = set()
    for ls in live_signals:
        best_i: int | None = None
        best_dt = window_sec + 1
        for i, bd in enumerate(bt_decisions):
            if i in used_bt:
                continue
            if ls["setup_type"] != bd.get("setup_type"):
                continue
            if ls["side"] != bd.get("side"):
                continue
            if ls["tsym"] != bd.get("tsym"):
                continue
            dt = abs(float(ls["ts_epoch"]) - float(bd["ts_epoch"]))
            if dt <= window_sec and dt < best_dt:
                best_dt = dt
                best_i = i
        if best_i is None:
            live_only.append(ls)
            continue
        used_bt.add(best_i)
        bd = bt_decisions[best_i]
        live_passed = bool(ls.get("validator_passed"))
        bt_passed = bool(bd.get("validator_passed"))
        row = {
            "setup_type": ls["setup_type"],
            "side": ls["side"],
            "tsym": ls["tsym"],
            "live_ts": ls["ts_ist"],
            "bt_ts": bd["ts_ist"],
            "delta_sec": round(abs(float(ls["ts_epoch"]) - float(bd["ts_epoch"])), 1),
            "live_validator_passed": live_passed,
            "bt_validator_passed": bt_passed,
            "live_rejections": ls.get("rejection_reasons") or [],
            "bt_rejections": bd.get("rejection_reasons") or [],
            "live_order_filled": ls.get("order_filled"),
            "bt_entered": bd.get("entered"),
        }
        matched.append(row)
        if live_passed != bt_passed:
            validator_mismatch.append(row)

    bt_only = [bd for i, bd in enumerate(bt_decisions) if i not in used_bt]
    return {
        "matched": matched,
        "live_only": live_only,
        "bt_only": bt_only,
        "validator_mismatch": validator_mismatch,
    }


def _render_markdown(report: dict[str, Any]) -> str:
    day = report["date"]
    ul = report["underlying"]
    tf = report["trade_match"]
    sf = report["signal_match"]
    funnel_live = report["live_funnel"]
    funnel_bt = report["backtest_funnel"]
    live_breakdown = report.get("live_setup_breakdown") or {}

    lines = [
        f"# Live vs Backtest Parity — {day} ({ul})",
        "",
        "## Summary",
        "",
        "| Metric | Live (DB) | Backtest |",
        "|---|---:|---:|",
        f"| Closed trades | {funnel_live['closed_trades']} | {funnel_bt['closed_trades']} |",
        f"| Candidate signals | {funnel_live['candidate_signals']} | {funnel_bt['candidate_signals']} |",
        f"| Validator passed | {funnel_live['validator_passed']} | {funnel_bt['validator_passed']} |",
        f"| Orders filled | {funnel_live['orders_filled']} | {funnel_bt['entries_filled']} |",
        "",
    ]
    if live_breakdown:
        adopted = live_breakdown.get("broker_adopted", 0)
        if adopted:
            lines.append(
                f"> Live includes **{adopted}** `broker_adopted` rows (phantom sync) — "
                f"use `--algo-only` to exclude."
            )
            lines.append("")
    lines += [
        f"**Trade match rate:** {tf['match_rate_pct']:.1f}% "
        f"({tf['matched_count']} matched / {tf['live_count']} live)",
        "",
        f"**Signal validator agreement:** "
        f"{sf['validator_agreement_pct']:.1f}% on matched signals "
        f"({sf['validator_mismatch_count']} mismatches)",
        "",
        "## Trade reconciliation",
        "",
        f"- Matched: **{tf['matched_count']}**",
        f"- Live only (missed by backtest): **{tf['live_only_count']}**",
        f"- Backtest only (extra): **{tf['bt_only_count']}**",
        "",
    ]

    if tf["matched"]:
        lines += ["### Matched trades", ""]
        lines.append(
            "| Contract | Setup | Live entry | BT entry | Δsec | Live P&L | BT P&L |"
        )
        lines.append("|---|---|---|---|---:|---:|---:|")
        for m in tf["matched"][:40]:
            lines.append(
                f"| {m['tsym']} | {m['setup_type']} | {m['live_entry'][11:]} | "
                f"{m['bt_entry'][11:]} | {m['entry_delta_sec']} | "
                f"₹{m['live_pnl']:,.0f} | ₹{m['bt_pnl']:,.0f} |"
            )
        lines.append("")

    if tf["live_only"]:
        lines += ["### Live only — backtest did not reproduce", ""]
        for t in tf["live_only"][:25]:
            hint = t.get("nearest_bt") or {}
            lines.append(
                f"- **{t['entry_ist'][11:]}** `{t['tsym']}` {t['setup_type']} "
                f"lots={t['lots']} P&L=₹{t['pnl']:,.0f} mode={t['mode']}"
            )
            if hint:
                lines.append(
                    f"  - Nearest BT signal: {hint.get('bt_ts', '—')} "
                    f"validator={hint.get('validator_passed')} "
                    f"entered={hint.get('entered')} "
                    f"rejections={hint.get('rejection_reasons', [])[:2]}"
                )
        lines.append("")

    if tf["bt_only"]:
        lines += ["### Backtest only — not in live DB", ""]
        for t in tf["bt_only"][:25]:
            lines.append(
                f"- **{t['entry_ist'][11:]}** `{t['tsym']}` {t['setup_type']} "
                f"lots={t['lots']} P&L=₹{t['pnl']:,.0f}"
            )
        lines.append("")

    if sf["validator_mismatch"]:
        lines += ["## Validator mismatches (same signal, different pass/fail)", ""]
        for m in sf["validator_mismatch"][:20]:
            lines.append(
                f"- `{m['tsym']}` {m['setup_type']} @ {m['live_ts'][11:]} — "
                f"live={'PASS' if m['live_validator_passed'] else 'FAIL'} vs "
                f"bt={'PASS' if m['bt_validator_passed'] else 'FAIL'}"
            )
            if m["live_rejections"]:
                lines.append(f"  - live reject: {m['live_rejections'][:3]}")
            if m["bt_rejections"]:
                lines.append(f"  - bt reject: {m['bt_rejections'][:3]}")
        lines.append("")

    lines += [
        "## Interpretation",
        "",
        "- **High trade match + high validator agreement** → backtest is valid for that day.",
        "- **Live-only trades** often = phantom adopts, tick vs 1m snap timing, or config drift at runtime.",
        "- **BT-only trades** = snap replay produced signals live did not log (or live was blocked).",
        "",
        f"_Generated by `scripts/live_backtest_parity.py` · tolerance={report['trade_tolerance_sec']}s_",
    ]
    return "\n".join(lines)


async def run_parity(
    *,
    day: date,
    underlying: str,
    scan_interval: int,
    capital: Decimal | None,
    trade_tolerance_sec: float,
    signal_window_sec: float,
    refresh_snap: bool,
    algo_only: bool,
    live_parity: bool = False,
    journal_replay: bool = False,
) -> dict[str, Any]:
    import importlib.util

    from algoflat.db.connection import close_pool, init_pool

    bt_spec = importlib.util.spec_from_file_location(
        "day_backtest", ROOT / "scripts" / "day_backtest.py"
    )
    if bt_spec is None or bt_spec.loader is None:
        raise RuntimeError("Could not load day_backtest.py")
    day_backtest = importlib.util.module_from_spec(bt_spec)
    sys.modules["day_backtest"] = day_backtest
    bt_spec.loader.exec_module(day_backtest)
    run_backtest = day_backtest.run_backtest

    pool = await init_pool()
    try:
        async with pool.acquire() as conn:
            live_signals = await _fetch_live_signals(conn, day, underlying)
            live_trades = await _fetch_live_closed(conn, day, underlying, algo_only=algo_only)
            live_funnel = await _fetch_funnel_counts(conn, day, underlying)
            live_setup_breakdown = await _fetch_live_closed_breakdown(conn, day, underlying)
    finally:
        await close_pool()

    if journal_replay:
        from algoflat.replay.compare import compare_closed_trades, compare_signal_decisions
        from algoflat.replay.full_session import run_full_session_replay
        from algoflat.replay.loader import JournalLoader
        from algoflat.replay.report import (
            journal_closed_trade_row,
            journal_signal_row,
            replay_closed_trade_row,
        )
        from algoflat.replay.scheduler import build_schedule

        pool = await init_pool()
        try:
            async with pool.acquire() as conn:
                journal = await JournalLoader(conn).load_day(day, underlying)
        finally:
            await close_pool()

        schedule = build_schedule(journal, scan_interval_sec=scan_interval)
        jr_result = run_full_session_replay(
            journal,
            schedule,
            live_parity=live_parity,
        )
        if jr_result is None:
            raise SystemExit(f"Journal replay failed — no snap for {day}.")

        decision_log = jr_result.decisions
        bt_trades = [
            _closed_to_row("journal_replay", replay_closed_trade_row(t))
            for t in jr_result.closed
        ]
        signal_cmp = compare_signal_decisions(
            [journal_signal_row(s) for s in journal.signals],
            decision_log,
            window_sec=signal_window_sec,
        )
        trade_cmp = compare_closed_trades(
            [journal_closed_trade_row(t) for t in journal.closed_trades],
            [replay_closed_trade_row(t) for t in jr_result.closed],
            tolerance_sec=trade_tolerance_sec,
        )
        matched_pairs = trade_cmp["matched"]
        live_only = [
            _closed_to_row("live", t) for t in trade_cmp["live_only"]
        ]
        bt_only = [
            _closed_to_row("journal_replay", t) for t in trade_cmp["replay_only"]
        ]

        live_count = len(live_trades)
        match_rate = trade_cmp["match_rate_pct"]
        val_agree_pct = signal_cmp["validator_agreement_pct"]

        report: dict[str, Any] = {
            "date": day.isoformat(),
            "underlying": underlying.upper(),
            "replay_mode": "journal_full",
            "trade_tolerance_sec": trade_tolerance_sec,
            "signal_window_sec": signal_window_sec,
            "scan_interval_sec": scan_interval,
            "algo_trades_only": algo_only,
            "live_funnel": live_funnel,
            "live_setup_breakdown": live_setup_breakdown,
            "backtest_funnel": {
                "candidate_signals": len(decision_log),
                "validator_passed": jr_result.validator_passed,
                "entries_filled": jr_result.entries,
                "closed_trades": len(bt_trades),
                "signals_seen_router": jr_result.signals_seen,
                "realized_pnl": float(jr_result.realized_pnl),
                "skips_top": sorted(jr_result.skips.items(), key=lambda x: -x[1])[:12],
            },
            "trade_match": {
                "matched_count": trade_cmp["matched_count"],
                "live_count": live_count,
                "bt_count": len(bt_trades),
                "live_only_count": trade_cmp["live_only_count"],
                "bt_only_count": trade_cmp["replay_only_count"],
                "match_rate_pct": match_rate,
                "pnl_direction_agreement_pct": trade_cmp["pnl_direction_agreement_pct"],
                "matched": matched_pairs,
                "live_only": [asdict(t) for t in live_only],
                "bt_only": [asdict(t) for t in bt_only],
            },
            "signal_match": {
                "matched_count": signal_cmp["matched_count"],
                "live_only_count": signal_cmp["live_only_count"],
                "bt_only_count": signal_cmp["replay_only_count"],
                "validator_mismatch_count": len(signal_cmp["validator_mismatch"]),
                "validator_agreement_pct": val_agree_pct,
                "validator_mismatch": signal_cmp["validator_mismatch"][:50],
                "live_only_sample": signal_cmp["live_only"][:30],
                "bt_only_sample": signal_cmp["replay_only"][:30],
            },
        }
        return report

    decision_log: list[dict[str, Any]] = []
    capital_pool = None
    if capital is not None:
        capital_pool = {
            "starting": capital,
            "available": capital,
            "deployed": Decimal("0"),
            "realized": Decimal("0"),
        }

    bt_result = await run_backtest(
        day,
        underlying=underlying,
        scan_interval=scan_interval,
        from_snap=not refresh_snap,
        refresh_snap=refresh_snap,
        capital_pool=capital_pool,
        decision_log=decision_log,
        live_parity=live_parity,
        honest_replay=not live_parity,
    )
    if bt_result is None:
        raise SystemExit(f"Backtest failed — no snap for {day}. Try --refresh-snap.")

    bt_trades = [
        _closed_to_row("backtest", t) for t in bt_result.get("closed") or []
    ]

    matched, live_only, bt_only = _match_trades(
        live_trades, bt_trades, tolerance_sec=trade_tolerance_sec
    )

    # Enrich live-only with nearest backtest decision
    live_only_enriched: list[dict] = []
    for lt in live_only:
        near = _nearest_decision(
            decision_log,
            ts_epoch=lt.entry_epoch,
            setup_type=lt.setup_type,
            side=lt.side,
            tsym=lt.tsym,
            window_sec=signal_window_sec * 3,
        )
        live_only_enriched.append({**asdict(lt), "nearest_bt": near})

    signal_cmp = _compare_signals(
        live_signals, decision_log, window_sec=signal_window_sec
    )

    bt_passed = sum(1 for d in decision_log if d.get("validator_passed"))
    bt_entered = sum(1 for d in decision_log if d.get("entered"))

    live_count = len(live_trades)
    match_rate = (len(matched) / live_count * 100) if live_count else 0.0
    sig_matched = signal_cmp["matched"]
    val_agree = sum(
        1
        for m in sig_matched
        if m["live_validator_passed"] == m["bt_validator_passed"]
    )
    val_agree_pct = (val_agree / len(sig_matched) * 100) if sig_matched else 0.0

    report: dict[str, Any] = {
        "date": day.isoformat(),
        "underlying": underlying.upper(),
        "trade_tolerance_sec": trade_tolerance_sec,
        "signal_window_sec": signal_window_sec,
        "scan_interval_sec": scan_interval,
        "algo_trades_only": algo_only,
        "live_funnel": live_funnel,
        "live_setup_breakdown": live_setup_breakdown,
        "backtest_funnel": {
            "candidate_signals": len(decision_log),
            "validator_passed": bt_passed,
            "entries_filled": bt_entered,
            "closed_trades": len(bt_trades),
            "signals_seen_router": bt_result.get("signals_seen", 0),
            "skips_top": sorted(
                (bt_result.get("skips") or {}).items(), key=lambda x: -x[1]
            )[:12],
        },
        "trade_match": {
            "matched_count": len(matched),
            "live_count": live_count,
            "bt_count": len(bt_trades),
            "live_only_count": len(live_only),
            "bt_only_count": len(bt_only),
            "match_rate_pct": round(match_rate, 1),
            "matched": matched,
            "live_only": live_only_enriched,
            "bt_only": [asdict(t) for t in bt_only],
        },
        "signal_match": {
            "matched_count": len(sig_matched),
            "live_only_count": len(signal_cmp["live_only"]),
            "bt_only_count": len(signal_cmp["bt_only"]),
            "validator_mismatch_count": len(signal_cmp["validator_mismatch"]),
            "validator_agreement_pct": round(val_agree_pct, 1),
            "validator_mismatch": signal_cmp["validator_mismatch"][:50],
            "live_only_sample": signal_cmp["live_only"][:30],
            "bt_only_sample": signal_cmp["bt_only"][:30],
        },
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Live vs backtest parity report")
    parser.add_argument("--date", required=True, help="IST session date YYYY-MM-DD")
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--scan-interval", type=int, default=10)
    parser.add_argument("--capital", type=float, default=None)
    parser.add_argument(
        "--trade-tolerance-sec",
        type=float,
        default=120,
        help="Max entry time delta to count as same trade (default 120s)",
    )
    parser.add_argument(
        "--signal-window-sec",
        type=float,
        default=90,
        help="Window to match candidate signals (default 90s)",
    )
    parser.add_argument(
        "--algo-only",
        action="store_true",
        help="Only compare live closed_trades linked to candidate_signal_id",
    )
    parser.add_argument(
        "--refresh-snap",
        action="store_true",
        help="Re-fetch broker snap before replay",
    )
    parser.add_argument(
        "--live-parity",
        action="store_true",
        help="Replay with live-parity mode (intra-bar path, skip option-chart veto like live)",
    )
    parser.add_argument(
        "--journal-replay",
        action="store_true",
        help="Use journal scan schedule + exit replay instead of day_backtest",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Print JSON to stdout only",
    )
    args = parser.parse_args()

    day = _parse_day(args.date)
    capital = Decimal(str(args.capital)) if args.capital is not None else None

    report = asyncio.run(
        run_parity(
            day=day,
            underlying=args.underlying.upper(),
            scan_interval=args.scan_interval,
            capital=capital,
            trade_tolerance_sec=args.trade_tolerance_sec,
            signal_window_sec=args.signal_window_sec,
            refresh_snap=args.refresh_snap,
            algo_only=args.algo_only,
            live_parity=args.live_parity,
            journal_replay=args.journal_replay,
        )
    )

    out_dir = ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"parity_{day.isoformat()}_{args.underlying.upper()}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"

    json_path.write_text(json.dumps(report, indent=2, default=str))
    md_path.write_text(_render_markdown(report))

    if args.json_only:
        print(json.dumps(report, indent=2, default=str))
        return

    tf = report["trade_match"]
    sf = report["signal_match"]
    print("=" * 72)
    print(f"PARITY REPORT — {day.isoformat()} {args.underlying.upper()}")
    print("=" * 72)
    print(
        f"Live closed: {report['live_funnel']['closed_trades']}  |  "
        f"Backtest closed: {report['backtest_funnel']['closed_trades']}"
    )
    print(
        f"Trade match: {tf['matched_count']}/{tf['live_count']} "
        f"({tf['match_rate_pct']:.1f}%)  |  "
        f"Live-only: {tf['live_only_count']}  |  BT-only: {tf['bt_only_count']}"
    )
    print(
        f"Signals: live={report['live_funnel']['candidate_signals']}  "
        f"bt_decisions={report['backtest_funnel']['candidate_signals']}  |  "
        f"Validator agree: {sf['validator_agreement_pct']:.1f}%  "
        f"mismatches: {sf['validator_mismatch_count']}"
    )
    print(f"\nJSON: {json_path}")
    print(f"Markdown: {md_path}")
    print("=" * 72)


if __name__ == "__main__":
    main()
