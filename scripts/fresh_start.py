#!/usr/bin/env python3
"""Wipe trading ledger, journals, candles, and instruments — keep broker credentials."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading-engine" / "src"))
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

IST = ZoneInfo("Asia/Kolkata")

# Order: child tables before parents.
WIPE_TABLES = (
    "validation_results",
    "ml_scores",
    "closed_trades",
    "positions",
    "orders",
    "candidate_signals",
    "option_features",
    "option_snapshots",
    "option_quotes",
    "candles_1m",
    "candles_3m",
    "candles_5m",
    "candles_15m",
    "instruments",
    "daily_risk_state",
    "system_events",
    "notifications",
    "field_availability_log",
)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Full data wipe + paper capital reset")
    parser.add_argument(
        "--skip-capital-reset",
        action="store_true",
        help="Only wipe market/trade data; do not seed daily_risk_state",
    )
    args = parser.parse_args()

    from algoflat.config import get_config
    from algoflat.db.connection import close_pool, get_pool, init_pool
    from algoflat.risk.engine import RiskEngine
    from algoflat.symbols_util import (
        capital_for,
        list_underlyings,
        risk_underlying_key,
        total_account_capital,
        uses_pooled_capital,
    )

    await init_pool()
    pool = get_pool()
    counts: dict[str, int] = {}
    async with pool.acquire() as conn:
        async with conn.transaction():
            for table in WIPE_TABLES:
                result = await conn.execute(f"DELETE FROM {table}")
                # asyncpg returns "DELETE N"
                try:
                    counts[table] = int(str(result).split()[-1])
                except (ValueError, IndexError):
                    counts[table] = 0

            if not args.skip_capital_reset:
                config = get_config()
                today = date.today()
                if uses_pooled_capital(config):
                    rows = [(risk_underlying_key(config), total_account_capital(config))]
                else:
                    rows = []
                    for row in list_underlyings(config):
                        sym = str(row.get("symbol", "")).upper()
                        if sym:
                            rows.append((sym, capital_for(config, sym)))
                    if not rows:
                        rows = [("NIFTY", total_account_capital(config))]

                for underlying, cap in rows:
                    await conn.execute(
                        """
                        INSERT INTO daily_risk_state (
                            trade_date, underlying, starting_capital, available_capital,
                            deployed_capital, realized_pnl, trade_count, consecutive_losses,
                            kill_switch, entries_blocked
                        ) VALUES (
                            $1, $2, $3, $3, 0, 0, 0, 0, FALSE, FALSE
                        )
                        ON CONFLICT (trade_date, underlying) DO UPDATE SET
                            starting_capital = EXCLUDED.starting_capital,
                            available_capital = EXCLUDED.available_capital,
                            deployed_capital = 0,
                            realized_pnl = 0,
                            trade_count = 0,
                            consecutive_losses = 0,
                            kill_switch = FALSE,
                            entries_blocked = FALSE,
                            block_reason = NULL,
                            updated_at = now()
                        """,
                        today,
                        underlying,
                        cap,
                    )
                counts["daily_risk_state_seeded"] = len(rows)

    await close_pool()

    total = sum(v for k, v in counts.items() if k != "daily_risk_state_seeded")
    print(f"OK: wiped {total} rows across {len(WIPE_TABLES)} tables")
    for table in WIPE_TABLES:
        if counts.get(table, 0):
            print(f"  - {table}: {counts[table]}")
    if counts.get("daily_risk_state_seeded"):
        print(f"  - paper capital reset for {counts['daily_risk_state_seeded']} underlying(s)")
    print("Restart trading-engine and run sync-missing to backfill ~7 days of history.")


if __name__ == "__main__":
    asyncio.run(main())
