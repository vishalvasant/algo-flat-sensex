"""Parse Flattrade trade book into closed round-trip rows for live UI."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import re
from typing import Any
from zoneinfo import ZoneInfo

from algoflat.symbols_util import lot_size_for_tsym

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class _OpenLot:
    qty: int
    price: Decimal
    ts: datetime


def _parse_fill_ts(row: dict[str, Any]) -> datetime | None:
    for key in ("fltm", "exch_tm", "norentm"):
        raw = str(row.get(key) or "").strip()
        if not raw:
            continue
        for fmt in (
            "%d-%m-%Y %H:%M:%S",
            "%H:%M:%S %d-%m-%Y",
        ):
            try:
                naive = datetime.strptime(raw, fmt)
                return naive.replace(tzinfo=IST)
            except ValueError:
                continue
    return None


def _option_side(tsym: str) -> str:
    upper = tsym.upper()
    # NSE weekly format: NIFTY15SEP26P23350 / ...C23400 — type letter sits before strike digits.
    m = re.search(r"[CP](\d+)$", upper)
    if m:
        return "PE" if upper[m.start()] == "P" else "CE"
    if "PE" in upper:
        return "PE"
    if "CE" in upper:
        return "CE"
    return "CE"


def _iter_trade_fills(
    rows: list[dict[str, Any]],
    *,
    trade_date: date | None = None,
) -> list[tuple[datetime, str, str, int, Decimal, str]]:
    """Normalize today's trade-book rows into sorted fills."""
    if trade_date is None:
        trade_date = datetime.now(IST).date()

    fills: list[tuple[datetime, str, str, int, Decimal, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("stat") or "").lower() == "not_ok":
            continue
        ts = _parse_fill_ts(row)
        if ts is None or ts.date() != trade_date:
            continue
        tsym = str(row.get("tsym") or "").strip()
        if not tsym:
            continue
        side = str(row.get("trantype") or "").upper()
        if side not in ("B", "S"):
            continue
        try:
            # Flattrade trade book: `flqty` is this fill's size. `fillshares` is often
            # cumulative filled qty on the order — using it double-counts multi-leg fills.
            qty = int(float(row.get("flqty") or row.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        try:
            # Prefer executed price fields; bare `prc` is usually the limit, not the fill.
            price = Decimal(str(row.get("flprc") or row.get("avgprc") or 0))
        except Exception:
            price = Decimal("0")
        if price <= 0:
            continue
        order_id = str(row.get("norenordno") or row.get("flid") or "")
        fills.append((ts, tsym, side, qty, price, order_id))

    fills.sort(key=lambda x: x[0])
    return fills


def fill_price_for_broker_order(
    rows: list[dict[str, Any]],
    broker_order_id: str,
    *,
    trade_date: date | None = None,
    side: str | None = None,
) -> tuple[Decimal, int] | None:
  """Per-order executed price from trade book (not position net average).

  ``side`` is BUY/SELL or B/S. When omitted, any fill for the order id matches.
  """
  if not broker_order_id:
    return None
  target = str(broker_order_id).strip()
  want: str | None = None
  if side:
    s = str(side).strip().upper()
    want = "B" if s in ("BUY", "B") else "S" if s in ("SELL", "S") else None

  matched_qty = 0
  matched_cost = Decimal("0")
  for _ts, _tsym, fill_side, qty, price, order_id in _iter_trade_fills(
    rows, trade_date=trade_date
  ):
    if order_id != target:
      continue
    if want and fill_side != want:
      continue
    matched_qty += qty
    matched_cost += price * Decimal(qty)
  if matched_qty <= 0:
    return None
  avg = matched_cost / Decimal(matched_qty)
  return avg, matched_qty


def open_lot_from_trade_book(
    rows: list[dict[str, Any]],
    tsym: str,
    *,
    trade_date: date | None = None,
) -> tuple[Decimal, int] | None:
    """FIFO open-lot entry for a symbol — correct after sell + re-buy at new price."""
    target = str(tsym or "").strip()
    if not target:
        return None

    open_lots: dict[str, deque[_OpenLot]] = defaultdict(deque)
    for _ts, sym, side, qty, price, _order_id in _iter_trade_fills(rows, trade_date=trade_date):
        if sym != target:
            continue
        if side == "B":
            open_lots[sym].append(_OpenLot(qty=qty, price=price, ts=_ts))
            continue
        remaining = qty
        while remaining > 0 and open_lots[sym]:
            lot = open_lots[sym][0]
            match_qty = min(remaining, lot.qty)
            lot.qty -= match_qty
            remaining -= match_qty
            if lot.qty <= 0:
                open_lots[sym].popleft()

    lots = open_lots.get(target)
    if not lots:
        return None
    total_qty = sum(lot.qty for lot in lots)
    if total_qty <= 0:
        return None
    cost = sum(lot.price * Decimal(lot.qty) for lot in lots)
    return cost / Decimal(total_qty), total_qty


def round_trips_from_trade_book(
    rows: list[dict[str, Any]],
    *,
    trade_date: date | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """FIFO-match buy/sell fills per symbol into closed trades (live broker truth)."""
    fills = _iter_trade_fills(rows, trade_date=trade_date)
    open_lots: dict[str, deque[_OpenLot]] = defaultdict(deque)
    closed: list[dict[str, Any]] = []

    for ts, tsym, side, qty, price, order_id in fills:
        if side == "B":
            open_lots[tsym].append(_OpenLot(qty=qty, price=price, ts=ts))
            continue

        remaining = qty
        while remaining > 0 and open_lots[tsym]:
            lot = open_lots[tsym][0]
            match_qty = min(remaining, lot.qty)
            pnl = (price - lot.price) * match_qty
            lot_size = lot_size_for_tsym(tsym)
            hold_seconds = max(0, int((ts - lot.ts).total_seconds()))
            closed.append(
                {
                    "id": f"broker-{order_id}-{len(closed)}",
                    "tsym": tsym,
                    "side": _option_side(tsym),
                    "instrument_token": str(
                        next(
                            (r.get("token") for r in rows if str(r.get("tsym")) == tsym),
                            "",
                        )
                    ),
                    "entry_ts": lot.ts.isoformat(),
                    "exit_ts": ts.isoformat(),
                    "entry_price": float(lot.price),
                    "exit_price": float(price),
                    "quantity": match_qty,
                    "lot_size": lot_size,
                    "lots": max(1, match_qty // max(lot_size, 1)),
                    "pnl": float(pnl),
                    "pnl_pct": float((price - lot.price) / lot.price * 100) if lot.price > 0 else None,
                    "mfe": None,
                    "mae": None,
                    "exit_reason": "broker_fill",
                    "setup_type": "broker",
                    "hold_seconds": hold_seconds,
                    "mode": "live",
                    "source": "broker",
                }
            )
            lot.qty -= match_qty
            remaining -= match_qty
            if lot.qty <= 0:
                open_lots[tsym].popleft()

    closed.sort(key=lambda r: r["exit_ts"], reverse=True)
    return closed[: max(1, min(limit, 2000))]
