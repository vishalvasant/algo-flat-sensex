from decimal import Decimal

from algoflat.broker.flattrade import BrokerDayPnl, _decimal_field, _parse_day_pnl_from_positions


def test_broker_day_pnl_total():
    row = BrokerDayPnl(realized_pnl=Decimal("429"), unrealized_pnl=Decimal("12.5"))
    assert row.total_pnl == Decimal("441.5")


def test_parse_day_pnl_from_positions_sums_closed_rows():
    raw = [
        {"tsym": "NIFTY28JUL26C23900", "netqty": "0", "rpnl": "217.75", "urmtom": "0.00"},
        {"tsym": "NIFTY28JUL26C23950", "netqty": "0", "rpnl": "211.25", "urmtom": "0.00"},
    ]
    day = _parse_day_pnl_from_positions(raw)
    assert day.realized_pnl == Decimal("429.00")
    assert day.total_pnl == Decimal("429.00")
