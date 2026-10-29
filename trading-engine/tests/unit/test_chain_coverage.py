"""Option chain ATM±steps coverage helpers."""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

from algoflat.config import get_config
from algoflat.contract_selector.selector import ContractUniverse
from algoflat.main import TradingEngineApp
from algoflat.models.events import Instrument

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))


def _inst(strike: int, side: str, exchange: str = "NFO") -> Instrument:
    return Instrument(
        exchange=exchange,
        token=f"{strike}{side}",
        tsym=f"NIFTY{strike}{side}",
        underlying="NIFTY",
        strike=Decimal(strike),
        option_type=side,
        lot_size=65,
        is_atm=strike == 24000,
        in_band=True,
    )


def test_strike_coverage_ok_for_full_band():
    app = TradingEngineApp.__new__(TradingEngineApp)
    app.config = get_config()
    strikes = list(range(23750, 24550, 50))
    instruments = []
    for s in strikes:
        instruments.append(_inst(s, "CE"))
        instruments.append(_inst(s, "PE"))
    uni = ContractUniverse(
        spot=Decimal("24020"),
        atm_strike=Decimal("24000"),
        instruments=instruments,
    )
    assert app._strike_coverage_ok(uni, Decimal("24000"), "NIFTY")


def test_strike_coverage_fails_when_edge_missing():
    app = TradingEngineApp.__new__(TradingEngineApp)
    app.config = get_config()
    strikes = list(range(23850, 24300, 50))
    instruments = []
    for s in strikes:
        instruments.append(_inst(s, "CE"))
        instruments.append(_inst(s, "PE"))
    uni = ContractUniverse(
        spot=Decimal("24020"),
        atm_strike=Decimal("24000"),
        instruments=instruments,
    )
    assert not app._strike_coverage_ok(uni, Decimal("24000"), "NIFTY")
