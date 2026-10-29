#!/usr/bin/env python3
"""Journal replay — Level 2 live session parity from Postgres.

Modes:
  validate  Re-run RuleValidator on stored candidate_signals (Phase 1)
  diff      Live funnel summary only (no engine replay)
  replay    Full engine replay at journal scan timestamps (Phase 2)
  full      Full replay: scan entries + exit ticks + trade match (Phase 3)

Usage (from repo root):
  CONFIG_DIR=./config PYTHONPATH=trading-engine/src:./scripts \\
    trading-engine/.venv/bin/python scripts/journal_replay.py \\
    --date 2026-07-28 --underlying NIFTY --mode validate

Requires: Postgres with live session journal (docker compose up -d postgres).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading-engine" / "src"))
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from algoflat.db.connection import close_pool, init_pool
from algoflat.replay.compare import compare_closed_trades, compare_signal_decisions
from algoflat.replay.loader import JournalLoader
from algoflat.replay.full_session import run_full_session_replay
from algoflat.replay.report import (
    journal_closed_trade_row,
    journal_signal_row,
    replay_closed_trade_row,
    write_full_report,
    write_replay_report,
    write_schedule_report,
    write_validate_report,
)
from algoflat.replay.scheduler import build_schedule
from algoflat.replay.session import run_session_replay
from algoflat.replay.validate import revalidate_journal

REPORT_DIR = ROOT / "reports"


def _weekdays(start: date, end: date) -> list[date]:
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _load_option_m1_from_snap(day: date, underlying: str) -> dict[str, list]:
    from day_backtest import load_day_snap  # noqa: E402

    snap = load_day_snap(day, underlying)
    if not snap:
        return {}
    return snap.get("option_series") or {}


async def _run_day(
    day: date,
    underlying: str,
    mode: str,
    *,
    with_option_snaps: bool,
    is_expiry_day: bool,
    scan_interval: int,
    live_parity: bool,
    trade_tolerance: float,
    database_url: str | None = None,
) -> None:
    from algoflat.db.connection import close_pool, init_pool

    if database_url:
        import asyncpg
        from algoflat.replay.loader import JournalLoader

        try:
            conn = await asyncpg.connect(database_url)
        except Exception as exc:
            raise SystemExit(f"Database unavailable ({exc})") from exc
        try:
            journal = await JournalLoader(conn).load_day(day, underlying)
        finally:
            await conn.close()
    else:
        try:
            pool = await init_pool()
        except Exception as exc:
            raise SystemExit(
                f"Postgres unavailable ({exc}). Start with: docker compose up -d postgres"
            ) from exc

        try:
            async with pool.acquire() as conn:
                journal = await JournalLoader(conn).load_day(day, underlying)
        finally:
            await close_pool()

    print(
        f"\n=== Journal {mode} {day} {underlying} ===\n"
        f"  scans={journal.funnel.strategy_decisions}  "
        f"signals={journal.funnel.candidate_signals}  "
        f"trades={journal.funnel.closed_trades}"
    )

    if mode == "diff":
        out = REPORT_DIR / f"journal_diff_{underlying}_{day}.md"
        lines = [
            f"# Journal diff — {underlying} {day}",
            "",
            "| Metric | Count |",
            "|--------|------:|",
        ]
        f = journal.funnel
        for k, v in [
            ("strategy_decision", f.strategy_decisions),
            ("candidate_signals", f.candidate_signals),
            ("validator_passed", f.validator_passed),
            ("orders_placed", f.orders_placed),
            ("orders_filled", f.orders_filled),
            ("closed_trades", f.closed_trades),
            ("entry_skipped", f.entry_skipped_events),
        ]:
            lines.append(f"| {k} | {v} |")
        out.write_text("\n".join(lines) + "\n")
        print(f"  Report → {out}")
        return

    if mode == "validate":
        option_m1 = _load_option_m1_from_snap(day, underlying) if with_option_snaps else None
        report = revalidate_journal(
            journal,
            is_expiry_day=is_expiry_day,
            option_m1=option_m1,
        )
        out_md = REPORT_DIR / f"journal_validate_{underlying}_{day}.md"
        out_json = REPORT_DIR / f"journal_validate_{underlying}_{day}.json"
        write_validate_report(report, journal, out_md=out_md, out_json=out_json)
        print(
            f"  Validator agreement: {report.agrees}/{report.comparable} "
            f"({report.agreement_pct:.1f}%)"
        )
        print(f"  New passes: {len(report.new_passes)}  New fails: {len(report.new_fails)}")
        print(f"  Report → {out_md}")
        return

    if mode == "full":
        schedule = build_schedule(journal, scan_interval_sec=scan_interval)
        print(f"  Schedule: {len(schedule.events)} scans (source={schedule.source})")
        result = run_full_session_replay(
            journal,
            schedule,
            live_parity=live_parity,
            option_m1_from_snap=with_option_snaps,
        )
        if result is None:
            print(
                f"  ERROR: No snap data for {day} {underlying}. "
                "Run day_backtest --from-snap first or check reports/snaps/."
            )
            return
        live_signal_rows = [journal_signal_row(s) for s in journal.signals]
        signal_cmp = compare_signal_decisions(live_signal_rows, result.decisions)
        live_trade_rows = [journal_closed_trade_row(t) for t in journal.closed_trades]
        replay_trade_rows = [replay_closed_trade_row(t) for t in result.closed]
        trade_cmp = compare_closed_trades(
            live_trade_rows, replay_trade_rows, tolerance_sec=trade_tolerance
        )
        out_md = REPORT_DIR / f"journal_full_{underlying}_{day}.md"
        out_json = REPORT_DIR / f"journal_full_{underlying}_{day}.json"
        write_full_report(
            result,
            journal,
            signal_cmp,
            trade_cmp,
            out_md=out_md,
            out_json=out_json,
        )
        print(
            f"  Full: scans={result.scans_run} signals={result.signals_seen} "
            f"entries={result.entries} closed={len(result.closed)} "
            f"P&L=₹{result.realized_pnl:,.0f}"
        )
        print(
            f"  Signals: matched={signal_cmp['matched_count']} "
            f"validator_agree={signal_cmp['validator_agreement_pct']}%"
        )
        print(
            f"  Trades: matched={trade_cmp['matched_count']}/{trade_cmp['live_count']} "
            f"({trade_cmp['match_rate_pct']}%) "
            f"pnl_dir_agree={trade_cmp['pnl_direction_agreement_pct']}%"
        )
        print(f"  Report → {out_md}")
        return

    if mode == "replay":
        schedule = build_schedule(journal, scan_interval_sec=scan_interval)
        print(f"  Schedule: {len(schedule.events)} scans (source={schedule.source})")
        result = run_session_replay(
            journal,
            schedule,
            live_parity=live_parity,
            option_m1_from_snap=with_option_snaps,
        )
        if result is None:
            print(
                f"  ERROR: No snap data for {day} {underlying}. "
                "Run day_backtest --from-snap first or check reports/snaps/."
            )
            return
        live_rows = [journal_signal_row(s) for s in journal.signals]
        comparison = compare_signal_decisions(live_rows, result.decisions)
        out_md = REPORT_DIR / f"journal_replay_{underlying}_{day}.md"
        out_json = REPORT_DIR / f"journal_replay_{underlying}_{day}.json"
        write_replay_report(
            result, journal, comparison, out_md=out_md, out_json=out_json
        )
        print(
            f"  Replay: scans={result.scans_run} signals={result.signals_seen} "
            f"validator_pass={result.validator_passed}"
        )
        print(
            f"  Parity: matched={comparison['matched_count']} "
            f"live_only={comparison['live_only_count']} "
            f"replay_only={comparison['replay_only_count']} "
            f"validator_agree={comparison['validator_agreement_pct']}%"
        )
        print(f"  Report → {out_md}")
        return

    raise SystemExit(f"Unknown mode: {mode}")


async def _main_async(args: argparse.Namespace) -> None:
    if args.week:
        if not args.week_start:
            raise SystemExit("--week-start required with --week")
        days = _weekdays(args.week_start, args.week_start + timedelta(days=4))
    elif args.start_date and args.end_date:
        days = _weekdays(args.start_date, args.end_date)
    elif args.date:
        days = [args.date]
    else:
        days = [date.today()]

    for day in days:
        await _run_day(
            day,
            args.underlying.upper(),
            args.mode,
            with_option_snaps=args.with_option_snaps,
            is_expiry_day=args.expiry_day,
            scan_interval=args.scan_interval,
            live_parity=not args.honest_replay,
            trade_tolerance=args.trade_tolerance,
            database_url=args.db_url,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Journal replay from Postgres live session")
    parser.add_argument("--date", type=date.fromisoformat, help="Session date YYYY-MM-DD")
    parser.add_argument("--week", action="store_true")
    parser.add_argument("--week-start", type=date.fromisoformat)
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument(
        "--mode",
        choices=("validate", "diff", "replay", "full"),
        default="validate",
        help="validate=re-run validator; diff=funnel only; replay=scan schedule",
    )
    parser.add_argument(
        "--with-option-snaps",
        action="store_true",
        help="Load option M1 from reports/snaps for option_chart_confirm in validate mode",
    )
    parser.add_argument(
        "--expiry-day",
        action="store_true",
        help="Treat session as weekly expiry day for validator",
    )
    parser.add_argument("--scan-interval", type=int, default=10)
    parser.add_argument(
        "--honest-replay",
        action="store_true",
        help="Use closed-bar honest replay (default: live-parity intra-bar)",
    )
    parser.add_argument(
        "--trade-tolerance",
        type=float,
        default=120.0,
        help="Max entry time delta to count as same trade in full mode (default 120s)",
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("LIVE_DATABASE_URL"),
        help="Postgres URL (default: local docker; or set LIVE_DATABASE_URL)",
    )
    args = parser.parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
