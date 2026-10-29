"""ContractSelector ATM retarget tests."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from algoflat.contract_selector.selector import ContractSelector, ContractUniverse
from algoflat.models.events import Instrument


def _inst(strike: int, side: str) -> Instrument:
  return Instrument(
    exchange="NFO",
    token=f"{strike}{side}",
    tsym=f"NIFTY{strike}{side}",
    underlying="NIFTY",
    strike=Decimal(strike),
    option_type=side,
    lot_size=65,
    is_atm=False,
    in_band=True,
  )


def test_retarget_atm_picks_nearest_strike():
  cfg = SimpleNamespace(symbols={"strike_step": 50, "strike_band_points": 300})
  sel = ContractSelector(cfg, broker=SimpleNamespace())  # type: ignore[arg-type]
  instruments = [
    _inst(24100, "CE"),
    _inst(24100, "PE"),
    _inst(24150, "CE"),
    _inst(24150, "PE"),
    _inst(24200, "CE"),
    _inst(24200, "PE"),
  ]
  uni = ContractUniverse(
    spot=Decimal("24200"),
    atm_strike=Decimal("24200"),
    expiry_symbol="21JUL26",
    instruments=instruments,
    atm_ce=instruments[4],
    atm_pe=instruments[5],
  )
  moved = sel.retarget_atm(uni, Decimal("24148"))
  assert moved.atm_strike == Decimal("24150")
  assert moved.atm_ce is not None and moved.atm_ce.strike == Decimal("24150")
  assert moved.atm_pe is not None and moved.atm_pe.strike == Decimal("24150")


def test_retarget_atm_custom_step():
  cfg = SimpleNamespace(symbols={"strike_step": 50, "strike_band_points": 500})
  sel = ContractSelector(cfg, broker=SimpleNamespace())  # type: ignore[arg-type]
  instruments = [
    _inst(76400, "CE"),
    _inst(76400, "PE"),
    _inst(76500, "CE"),
    _inst(76500, "PE"),
    _inst(76600, "CE"),
    _inst(76600, "PE"),
  ]
  uni = ContractUniverse(
    spot=Decimal("76500"),
    atm_strike=Decimal("76500"),
    expiry_symbol="24JUL26",
    instruments=instruments,
    atm_ce=instruments[2],
    atm_pe=instruments[3],
  )
  moved = sel.retarget_atm(uni, Decimal("76465.55"), strike_step=100)
  assert moved.atm_strike == Decimal("76500")
  assert moved.atm_ce is not None and moved.atm_ce.strike == Decimal("76500")
