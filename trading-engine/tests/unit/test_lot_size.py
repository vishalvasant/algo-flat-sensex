"""Lot size resolution for index options."""
from __future__ import annotations

from algoflat.symbols_util import NIFTY_LOT_SIZE, lot_size_for_tsym


def test_nifty_lot_size_constant():
    assert NIFTY_LOT_SIZE == 65


def test_lot_size_from_metadata():
    assert lot_size_for_tsym("NIFTY28JUL26C24000", metadata_lot_size=65) == 65


def test_nifty_fallback_without_metadata():
    assert lot_size_for_tsym("NIFTY28JUL26P23650") == 65
