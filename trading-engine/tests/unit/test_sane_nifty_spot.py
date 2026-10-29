from decimal import Decimal

from algoflat.symbols_util import is_sane_nifty_spot


def test_sane_nifty_spot_band():
    assert is_sane_nifty_spot(Decimal("23830"))
    assert is_sane_nifty_spot(Decimal("25499"))
    assert not is_sane_nifty_spot(Decimal("72095"))
    assert not is_sane_nifty_spot(None)
