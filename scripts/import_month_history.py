#!/usr/bin/env python3
"""Import multi-timeframe historical candles into Postgres (+ optional JSON snaps).

Fetches from Flattrade TPSeries per session day:
  - Spot/futures: 1m, 3m, 5m (broker-native) + 15m (IST-aligned aggregate from 5m)
  - Options ATM±band: 1m candles per contract token

Usage (from repo root):
  CONFIG_DIR=./config PYTHONPATH=trading-engine/src \\
    trading-engine/.venv/bin/python scripts/import_month_history.py \\
    --start-date 2026-07-08 --end-date 2026-08-07 --underlying NIFTY

  # Also refresh reports/snaps/ for day_backtest --from-snap:
  ... --also-snap

Requires: DATABASE_URL, Flattrade session (.flattrade/session.json)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading-engine" / "src"))
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from algoflat.broker.auth import ensure_session
from algoflat.broker.credentials import FlattradeConfig
from algoflat.broker.base import BrokerAdapter
from algoflat.broker.factory import create_live_broker
from algoflat.config import get_config
from algoflat.contract_selector.selector import ContractSelector
from algoflat.db.connection import close_pool, init_pool
from algoflat.db.migrate import apply_migrations
from algoflat.features.indicators import aggregate_from_m5
from algoflat.journal.writer import JournalWriter
from algoflat.models.events import Candle, CandleInterval
from algoflat.symbols_util import apply_active_underlying, resolve_all_spot_tokens

# Reuse proven fetch/snap helpers from day_backtest.
from day_backtest import (  # noqa: E402
    _fetch_session_candles,
    _load_option_series,
    _prior_snap_universe,
    _weekdays_between,
    save_day_snap,
)

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class DayImportStats:
    day: date
    spot_token: str
    spot_bars: dict[str, int] = field(default_factory=dict)
    option_tokens: int = 0
    option_bars: int = 0
    instruments: int = 0
    snap_saved: bool = False
    error: str | None = None


def _session_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(9, 15), tzinfo=IST).astimezone(timezone.utc)
    end = datetime.combine(day, time(15, 30), tzinfo=IST).astimezone(timezone.utc)
    now = datetime.now(tz=timezone.utc)
    if day == now.astimezone(IST).date() and end > now:
        end = now
    return start, end


def _atm_strike_band(universe, step: Decimal) -> list[Decimal]:
    atm = Decimal(str(universe.atm_strike))
    return [atm + step * i for i in range(-2, 3)]


async def _write_candles_batched(
    writer: JournalWriter,
    candles: list[Candle],
    *,
    batch_size: int = 500,
) -> int:
    if not candles:
        return 0
    total = 0
    for i in range(0, len(candles), batch_size):
        chunk = candles[i : i + batch_size]
        await writer.write_candles(chunk)
        total += len(chunk)
    return total


async def import_session_day(
    *,
    day: date,
    underlying: str,
    broker: BrokerAdapter,
    config,
    selector: ContractSelector,
    writer: JournalWriter,
    also_snap: bool,
    skip_existing: bool,
    db_ok: bool = True,
) -> DayImportStats:
    ul = underlying.upper()
    exchange = config.symbols["exchange_spot"]
    spot_token = str(config.symbols["spot_token"])
    step = Decimal(str(config.symbols.get("strike_step", 50)))

    stats = DayImportStats(day=day, spot_token=spot_token)
    try:
        if skip_existing and db_ok:
            pool = await init_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT COUNT(*)::int AS cnt
                    FROM candles_1m
                    WHERE instrument_token = $1
                      AND ts >= $2 AND ts < $3
                    """,
                    spot_token,
                    datetime.combine(day, time(9, 15), tzinfo=IST).astimezone(timezone.utc),
                    datetime.combine(day, time(15, 31), tzinfo=IST).astimezone(timezone.utc),
                )
                if int(row["cnt"] or 0) >= 300:
                    print(f"  SKIP {day} — spot 1m already in DB ({row['cnt']} bars)")
                    return stats

        print(f"\n==> {day.isoformat()} ({day.strftime('%a')}) — fetching spot 1m/3m/5m...")
        all_candles, spot_token = await _fetch_session_candles(broker, config, day)
        m1 = all_candles.get(CandleInterval.M1) or []
        m5 = all_candles.get(CandleInterval.M5) or []
        if len(m1) < 30:
            stats.error = "insufficient_spot_bars"
            print(f"  WARN: only {len(m1)} 1m bars — skipping")
            return stats

        # Tag spot token on every bar.
        for interval, bars in all_candles.items():
            for c in bars:
                c.instrument_token = spot_token

        m15 = aggregate_from_m5(m5, 15)
        all_candles[CandleInterval.M15] = m15

        for interval, bars in all_candles.items():
            if db_ok:
                written = await _write_candles_batched(writer, bars)
                stats.spot_bars[interval.value] = written
                print(f"  spot {interval.value}: {written} bars → DB")
            else:
                stats.spot_bars[interval.value] = len(bars)
                print(f"  spot {interval.value}: {len(bars)} bars (snap only)")

        mid_spot = m1[len(m1) // 2].close
        print(f"  Building universe (ATM≈{mid_spot})...")
        universe = await selector.build_universe(mid_spot, as_of=day)
        if not universe.instruments:
            from algoflat.contract_selector.expiry import (
                format_expiry_tag,
                next_weekly_expiry_dates,
            )

            cal = next_weekly_expiry_dates(
                underlying=ul, as_of=day, count=1, include_today_if_expiry=True
            )
            primary_exp = format_expiry_tag(cal[0]) if cal else None
            prior_uni = _prior_snap_universe(day, ul)
            if prior_uni and (
                primary_exp is None or prior_uni.expiry_symbol == primary_exp
            ):
                universe = prior_uni
                print(f"  Reused prior snap universe expiry={universe.expiry_symbol}")

        pool = await init_pool() if db_ok else None
        if pool is not None:
            await selector.persist_instruments(pool, universe)
        stats.instruments = len(universe.instruments)

        session_start, session_end = _session_bounds(day)
        strikes = _atm_strike_band(universe, step)
        option_series = await _load_option_series(
            broker, universe, strikes, session_start, session_end
        )
        stats.option_tokens = len(option_series)
        opt_bar_count = 0
        for token, bars in option_series.items():
            for c in bars:
                c.instrument_token = token
            if db_ok:
                opt_bar_count += await _write_candles_batched(writer, bars)
            else:
                opt_bar_count += len(bars)
        stats.option_bars = opt_bar_count
        print(
            f"  options: {stats.option_tokens} tokens, {stats.option_bars} 1m bars"
            + (" → DB" if db_ok else " (snap only)")
        )

        if also_snap:
            prior_day = day - timedelta(days=1)
            while prior_day.weekday() >= 5:
                prior_day -= timedelta(days=1)
            prior_m5: list[Candle] = []
            if prior_day < day:
                try:
                    prev, _ = await _fetch_session_candles(broker, config, prior_day)
                    prior_m5 = prev.get(CandleInterval.M5) or []
                except Exception as exc:  # noqa: BLE001
                    print(f"  WARN: prior day M5 fetch failed: {exc}")
            save_day_snap(
                day,
                underlying=ul,
                all_candles=all_candles,
                prior_day=prior_day if prior_m5 else None,
                prior_m5=prior_m5,
                universe=universe,
                option_series=option_series,
            )
            stats.snap_saved = True
            print(f"  snap saved → reports/snaps/{day.isoformat()}/{ul}/")

    except Exception as exc:  # noqa: BLE001
        stats.error = str(exc)
        print(f"  ERROR {day}: {exc}")
    return stats


async def run_import(
    *,
    start_date: date,
    end_date: date,
    underlying: str = "NIFTY",
    also_snap: bool = False,
    skip_existing: bool = True,
    snap_only: bool = False,
) -> dict[str, Any]:
    days = _weekdays_between(start_date, end_date)
    print("\n" + "=" * 72)
    print(f"HISTORICAL IMPORT — {underlying.upper()} · {start_date} → {end_date}")
    print(f"Trading days: {len(days)}  |  {'snaps only' if snap_only else 'DB' + (' + snaps' if also_snap else '')}")
    print("Timeframes: 1m, 3m, 5m (broker) + 15m (from 5m) + option 1m")
    print("=" * 72)

    db_ok = not snap_only
    if db_ok:
        try:
            await init_pool()
            await apply_migrations()
        except Exception as exc:  # noqa: BLE001
            db_ok = False
            print(f"WARN: Database unavailable ({exc}) — continuing with snaps only")
    if snap_only or also_snap:
        also_snap = True

    config = get_config()
    apply_active_underlying(config, underlying.upper())

    print("==> Ensuring Flattrade session...")
    await ensure_session(FlattradeConfig.from_env())
    broker = create_live_broker(config)
    await broker.connect()
    await resolve_all_spot_tokens(config, broker)

    selector = ContractSelector(config, broker)
    writer = JournalWriter()

    results: list[DayImportStats] = []
    try:
        for i, day in enumerate(days, 1):
            print(f"\n[{i}/{len(days)}]", end="")
            stats = await import_session_day(
                day=day,
                underlying=underlying,
                broker=broker,
                config=config,
                selector=selector,
                writer=writer,
                also_snap=also_snap,
                skip_existing=skip_existing,
                db_ok=db_ok,
            )
            results.append(stats)
            await asyncio.sleep(0.5)
    finally:
        await broker.disconnect()
        if db_ok:
            await close_pool()

    ok = [r for r in results if not r.error and sum(r.spot_bars.values()) > 0]
    failed = [r for r in results if r.error]
    empty = [r for r in results if not r.error and sum(r.spot_bars.values()) == 0]

    summary = {
        "underlying": underlying.upper(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "days_requested": len(days),
        "days_imported": len(ok),
        "days_failed": len(failed),
        "days_skipped_empty": len(empty),
        "total_spot_bars": sum(sum(r.spot_bars.values()) for r in ok),
        "total_option_bars": sum(r.option_bars for r in ok),
        "snaps_saved": sum(1 for r in ok if r.snap_saved),
        "daily": [
            {
                "date": r.day.isoformat(),
                "spot_bars": r.spot_bars,
                "option_tokens": r.option_tokens,
                "option_bars": r.option_bars,
                "instruments": r.instruments,
                "snap": r.snap_saved,
                "error": r.error,
            }
            for r in results
        ],
    }

    print("\n" + "=" * 72)
    print("IMPORT SUMMARY")
    print("=" * 72)
    print(f"  Days imported: {summary['days_imported']} / {summary['days_requested']}")
    print(f"  Spot bars written: {summary['total_spot_bars']:,}")
    print(f"  Option 1m bars written: {summary['total_option_bars']:,}")
    if also_snap:
        print(f"  JSON snaps saved: {summary['snaps_saved']}")
    if failed:
        print(f"  Failed: {', '.join(r.day.isoformat() for r in failed)}")
    if empty:
        print(f"  Empty/skipped: {', '.join(r.day.isoformat() for r in empty)}")
    print("=" * 72)

    out = ROOT / "reports" / f"import_history_{start_date}_{end_date}_{underlying.upper()}.json"
    import json

    out.write_text(json.dumps(summary, indent=2))
    print(f"Report: {out}")
    return summary


def _default_month_range() -> tuple[date, date]:
    today = datetime.now(IST).date()
    end = today - timedelta(days=1)
    while end.weekday() >= 5:
        end -= timedelta(days=1)
    start = end - timedelta(days=31)
    while start.weekday() >= 5:
        start += timedelta(days=1)
    return start, end


def main() -> None:
    parser = argparse.ArgumentParser(description="Import month of multi-TF candles to DB")
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--start-date", help="YYYY-MM-DD (default: ~1 month ago)")
    parser.add_argument("--end-date", help="YYYY-MM-DD (default: last weekday)")
    parser.add_argument(
        "--also-snap",
        action="store_true",
        help="Also save reports/snaps/ JSON for day_backtest --from-snap",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-import even if spot 1m already exists in DB for that day",
    )
    parser.add_argument(
        "--snap-only",
        action="store_true",
        help="Fetch and save JSON snaps only (skip DB writes)",
    )
    args = parser.parse_args()

    if args.start_date and args.end_date:
        start = date.fromisoformat(args.start_date)
        end = date.fromisoformat(args.end_date)
    elif args.start_date or args.end_date:
        parser.error("--start-date and --end-date must be used together")
    else:
        start, end = _default_month_range()

    asyncio.run(
        run_import(
            start_date=start,
            end_date=end,
            underlying=args.underlying.upper(),
            also_snap=args.also_snap or args.snap_only,
            skip_existing=not args.force,
            snap_only=args.snap_only,
        )
    )


if __name__ == "__main__":
    main()
