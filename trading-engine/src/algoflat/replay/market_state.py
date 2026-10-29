"""Build market state as-of a journal scan timestamp."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from algoflat.models.events import Candle, CandleInterval


def _norm(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def minute_start(ts: datetime) -> datetime:
    t = _norm(ts)
    return t.replace(second=0, microsecond=0)


def bar_for_ts(m1_all: list[Candle], ts: datetime) -> Candle | None:
    """Return the 1m bar whose period contains ``ts``."""
    target = minute_start(ts)
    best: Candle | None = None
    for bar in m1_all:
        bts = minute_start(bar.ts)
        if bts == target:
            return bar
        if bts < target:
            best = bar
        elif bts > target:
            break
    return best


def bar_fraction(bar: Candle, ts: datetime) -> Decimal:
    t = _norm(ts)
    b = _norm(bar.ts)
    sec = (t - b).total_seconds()
    return Decimal(str(min(1.0, max(0.0, sec / 60.0))))


def spot_from_bar(
    bar: Candle,
    ts: datetime,
    *,
    use_intra_bar: bool = True,
) -> Decimal:
    """Spot at ``ts`` — intra-bar path when live-parity, else last closed."""
    if not use_intra_bar:
        prior = bar.open
        return prior
    # Import here to avoid circular deps with scripts/
    from day_backtest import _bar_path_price  # noqa: E402

    frac = bar_fraction(bar, ts)
    return _bar_path_price(bar.open, bar.high, bar.low, bar.close, frac, bar.vwap)


def candles_as_of(
    m1_all: list[Candle],
    m3_all: list[Candle],
    m5_all: list[Candle],
    ts: datetime,
    *,
    spot: Decimal,
    bar: Candle,
    honest: bool = False,
) -> tuple[list[Candle], list[Candle], list[Candle]]:
    """M1/M3/M5 slices for feature engine at scan time ``ts``."""
    from day_backtest import (  # noqa: E402
        _forming_m1,
        _slice_before,
        _slice_closed_period,
        _slice_upto,
    )

    if honest:
        m1_closed = _slice_before(m1_all, bar.ts)
        m1 = m1_closed + [_forming_m1(bar, ts, spot, honest=True)]
        m3 = _slice_closed_period(m3_all, ts, 3)
        m5 = _slice_closed_period(m5_all, ts, 5)
    else:
        m1_closed = _slice_before(m1_all, bar.ts)
        m1 = m1_closed + [_forming_m1(bar, ts, spot, honest=False)]
        m3 = _slice_upto(m3_all, ts)
        m5 = _slice_upto(m5_all, ts)
    return m1, m3, m5
