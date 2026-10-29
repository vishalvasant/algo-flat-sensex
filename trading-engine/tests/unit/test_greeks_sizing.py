"""Tests for greeks-based lot sizing multiplier."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from algoflat.models.events import Bias, CandidateSignal, FeatureSnapshot, OptionState
from algoflat.risk.greeks_sizing import greeks_confirmation_multiplier
from algoflat.risk.engine import fit_lots_to_capital


def _signal(delta: float | None, gamma: float | None = 0.001, iv: float | None = 40.0) -> CandidateSignal:
    return CandidateSignal(
        id=uuid4(),
        ts=datetime.now(tz=timezone.utc),
        tsym="NIFTYTEST",
        instrument_token="1",
        side="CE",
        setup_type="momentum_continuation",
        confidence=92,
        strategy_version="test",
        feature_snapshot=FeatureSnapshot(
            ts=datetime.now(tz=timezone.utc),
            nifty_spot=Decimal("24000"),
            bias_5m=Bias.BULLISH,
        ),
        scanner_metadata={
            "strike_pick": {
                "delta": delta,
                "gamma": gamma,
                "iv": iv,
            }
        },
    )


def test_greeks_ideal_delta_full_multiplier():
    mult = greeks_confirmation_multiplier(_signal(0.48), None, {"greeks_lot_sizing": {"enabled": True}})
    assert mult == Decimal("1")


def test_greeks_weak_delta_reduces_multiplier():
    mult = greeks_confirmation_multiplier(_signal(0.15), None, {"greeks_lot_sizing": {"enabled": True}})
    assert mult < Decimal("1")


def test_high_confidence_uses_available_margin():
    cfg = {
        "max_premium_pct_of_available": 90,
        "max_deployed_pct_of_equity": 90,
        "confidence_lot_sizing": {
            "enabled": True,
            "mode": "dynamic",
            "base_confidence": 76,
            "max_confidence": 100,
            "min_capital_pct": 30,
            "max_capital_pct": 90,
            "aggressive_deploy_min_confidence": 90,
            "max_lots": 0,
        },
    }
    low_lots, _ = fit_lots_to_capital(
        cfg,
        confidence=78,
        entry_ltp=Decimal("100"),
        lot_size=65,
        available=Decimal("50000"),
        deployed=Decimal("0"),
        equity=Decimal("50000"),
        greeks_mult=Decimal("1"),
    )
    high_lots, _ = fit_lots_to_capital(
        cfg,
        confidence=95,
        entry_ltp=Decimal("100"),
        lot_size=65,
        available=Decimal("50000"),
        deployed=Decimal("0"),
        equity=Decimal("50000"),
        greeks_mult=Decimal("1"),
    )
    assert high_lots > low_lots
    # 90% of 50k = 45k budget; NIFTY cost/lot = 100 × 65 = 6500 → 6 lots max
    assert high_lots == 6
