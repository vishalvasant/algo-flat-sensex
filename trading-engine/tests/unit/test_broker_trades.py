"""Tests for Flattrade trade book → round-trip parsing."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from algoflat.broker.broker_trades import round_trips_from_trade_book


def _fill(
    *,
    order_id: str,
    tsym: str,
    side: str,
    qty: int,
    price: str,
    fltm: str,
    fillshares: str | None = None,
) -> dict:
    row = {
        "stat": "Ok",
        "norenordno": order_id,
        "tsym": tsym,
        "trantype": side,
        "flqty": str(qty),
        "flprc": price,
        "avgprc": price,
        "fltm": fltm,
        "token": "63939",
    }
    if fillshares is not None:
        row["fillshares"] = fillshares
    return row


def test_round_trip_from_buy_sell_pair():
    day = date(2026, 7, 27)
    rows = [
        _fill(
            order_id="1",
            tsym="NIFTY28JUL26C24000",
            side="B",
            qty=260,
            price="67.65",
            fltm="27-07-2026 13:34:55",
        ),
        _fill(
            order_id="2",
            tsym="NIFTY28JUL26C24000",
            side="S",
            qty=260,
            price="67.10",
            fltm="27-07-2026 13:35:12",
        ),
    ]
    trips = round_trips_from_trade_book(rows, trade_date=day, limit=10)
    assert len(trips) == 1
    assert trips[0]["quantity"] == 260
    assert trips[0]["entry_price"] == 67.65
    assert trips[0]["exit_price"] == 67.10
    assert trips[0]["pnl"] == -143.0
    assert trips[0]["source"] == "broker"


def test_ignores_other_days():
    day = date(2026, 7, 27)
    rows = [
        _fill(
            order_id="1",
            tsym="NIFTY28JUL26C24000",
            side="B",
            qty=65,
            price="50.00",
            fltm="26-07-2026 13:34:55",
        ),
    ]
    assert round_trips_from_trade_book(rows, trade_date=day, limit=10) == []


def test_open_lot_after_sell_and_rebuy_uses_latest_fill_not_blended_avg():
    from algoflat.broker.broker_trades import open_lot_from_trade_book

    day = date(2026, 7, 30)
    rows = [
        _fill(
            order_id="1",
            tsym="NIFTY04AUG26C24350",
            side="B",
            qty=65,
            price="100.00",
            fltm="30-07-2026 10:15:00",
        ),
        _fill(
            order_id="2",
            tsym="NIFTY04AUG26C24350",
            side="S",
            qty=65,
            price="105.00",
            fltm="30-07-2026 10:20:00",
        ),
        _fill(
            order_id="3",
            tsym="NIFTY04AUG26C24350",
            side="B",
            qty=65,
            price="110.00",
            fltm="30-07-2026 10:30:00",
        ),
    ]
    lot = open_lot_from_trade_book(rows, "NIFTY04AUG26C24350", trade_date=day)
    assert lot is not None
    assert lot[0] == Decimal("110.00")
    assert lot[1] == 65


def test_fill_price_for_broker_order_uses_order_fill():
    from algoflat.broker.broker_trades import fill_price_for_broker_order

    day = date(2026, 7, 30)
    rows = [
        _fill(
            order_id="99",
            tsym="NIFTY04AUG26C24350",
            side="B",
            qty=65,
            price="110.00",
            fltm="30-07-2026 10:30:00",
        ),
    ]
    matched = fill_price_for_broker_order(rows, "99", trade_date=day)
    assert matched is not None
    assert matched[0] == Decimal("110.00")
    assert matched[1] == 65


def test_partial_fills_use_flqty_not_cumulative_fillshares():
    """Flattrade often sets fillshares cumulative; flqty is the per-print size."""
    from algoflat.broker.broker_trades import fill_price_for_broker_order

    day = date(2026, 9, 15)
    rows = [
        {
            "stat": "Ok",
            "norenordno": "26091500195757",
            "tsym": "NIFTY15SEP26P23350",
            "trantype": "B",
            "flqty": "65",
            "fillshares": "65",
            "flprc": "58.20",
            "avgprc": "58.20",
            "prc": "59.25",
            "fltm": "15-09-2026 11:47:27",
            "qty": "195",
        },
        {
            "stat": "Ok",
            "norenordno": "26091500195757",
            "tsym": "NIFTY15SEP26P23350",
            "trantype": "B",
            "flqty": "65",
            "fillshares": "130",
            "flprc": "58.20",
            "avgprc": "58.20",
            "prc": "59.25",
            "fltm": "15-09-2026 11:47:27",
            "qty": "195",
        },
        {
            "stat": "Ok",
            "norenordno": "26091500195757",
            "tsym": "NIFTY15SEP26P23350",
            "trantype": "B",
            "flqty": "65",
            "fillshares": "195",
            "flprc": "58.20",
            "avgprc": "58.20",
            "prc": "59.25",
            "fltm": "15-09-2026 11:47:27",
            "qty": "195",
        },
    ]
    matched = fill_price_for_broker_order(rows, "26091500195757", trade_date=day, side="BUY")
    assert matched is not None
    assert matched[0] == Decimal("58.20")
    assert matched[1] == 195


def test_option_side_from_weekly_tsym():
    from algoflat.broker.broker_trades import _option_side

    assert _option_side("NIFTY15SEP26P23350") == "PE"
    assert _option_side("NIFTY15SEP26C23400") == "CE"
