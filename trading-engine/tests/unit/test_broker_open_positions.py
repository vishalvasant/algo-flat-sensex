from algoflat.broker.flattrade import _entry_price_from_broker_position, parse_broker_open_positions
from decimal import Decimal


def test_entry_price_prefers_day_buy_average():
    row = {
        "tsym": "NIFTY28MAR2422100PE",
        "netqty": 65,
        "daybuyavgprc": "118.0",
        "netavgprc": "120.5",
    }
    assert _entry_price_from_broker_position(row, 65) == Decimal("118.0")


def test_parse_broker_open_positions_skips_flat_rows():
    raw = [
        {"tsym": "NIFTY28MAR2422000CE", "token": "12345", "netqty": 0, "rpnl": "100"},
        {
            "tsym": "NIFTY28MAR2422100PE",
            "token": "67890",
            "exch": "NFO",
            "netqty": -65,
            "netavgprc": "120.5",
            "lp": "125.0",
            "urmtom": "-292.5",
        },
    ]
    rows = parse_broker_open_positions(raw)
    assert len(rows) == 1
    assert rows[0]["tsym"] == "NIFTY28MAR2422100PE"
    assert rows[0]["side"] == "PE"
    assert rows[0]["quantity"] == 65
    assert rows[0]["source"] == "broker"
    assert rows[0]["position_id"] == "broker-67890"


def test_parse_broker_open_positions_empty_when_not_list():
    assert parse_broker_open_positions(None) == []
    assert parse_broker_open_positions({}) == []
