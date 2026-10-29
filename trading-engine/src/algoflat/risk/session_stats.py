"""Per-mode session trade stats from closed_trades (IST calendar day)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from algoflat.db.connection import get_pool

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class SessionTradeStats:
    realized_pnl: Decimal
    trade_count: int
    consecutive_losses: int


def _consecutive_losses(pnls: list[Decimal]) -> int:
    count = 0
    for pnl in reversed(pnls):
        if pnl < 0:
            count += 1
        else:
            break
    return count


async def fetch_session_trade_stats(
    mode: str,
    *,
    trade_date: date | None = None,
) -> SessionTradeStats:
    """Aggregate closed trades for one execution mode on an IST trade date."""
    if trade_date is None:
        trade_date = datetime.now(IST).date()
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pnl
            FROM closed_trades
            WHERE mode = $1
              AND (exit_ts AT TIME ZONE 'Asia/Kolkata')::date = $2
            ORDER BY exit_ts ASC
            """,
            mode,
            trade_date,
        )
    if not rows:
        return SessionTradeStats(
            realized_pnl=Decimal("0"),
            trade_count=0,
            consecutive_losses=0,
        )
    pnls = [Decimal(str(r["pnl"])) for r in rows]
    return SessionTradeStats(
        realized_pnl=sum(pnls, Decimal("0")),
        trade_count=len(pnls),
        consecutive_losses=_consecutive_losses(pnls),
    )
