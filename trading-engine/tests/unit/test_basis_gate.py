from datetime import datetime, timezone
from decimal import Decimal

from algoflat.models.events import Bias, CandidateSignal, FeatureSnapshot
from algoflat.validator.basis_gate import basis_gate_rejection_reasons


def _signal(*, basis: float, setup: str = "vwap_trend") -> CandidateSignal:
  feat = FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    nifty_spot=Decimal("25030"),
    session_vwap=Decimal("25000"),
    bias_5m=Bias.BULLISH,
    extra={"basis_points": basis, "fut_spot": 25030.0},
  )
  return CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type=setup,
    side="CE",
    instrument_token="tok",
    tsym="NIFTY01SEP26C25000",
    strategy_version="test",
    feature_snapshot=feat,
    confidence=80,
  )


def test_basis_gate_blocks_wide_spread() -> None:
  reasons = basis_gate_rejection_reasons(
    _signal(basis=35.0),
    cfg={"enabled": True, "max_abs_basis_points": 30, "max_basis_pct_of_spot": 0.0005},
  )
  assert reasons == ["basis_spread_too_wide"]


def test_basis_gate_allows_tight_spread() -> None:
  reasons = basis_gate_rejection_reasons(
    _signal(basis=12.5),
    cfg={"enabled": True, "max_abs_basis_points": 30},
  )
  assert reasons == []


def test_basis_gate_allows_far_month_carry() -> None:
  feat = FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    nifty_spot=Decimal("24278"),
    session_vwap=Decimal("24200"),
    bias_5m=Bias.BULLISH,
    extra={"basis_points": 176.7, "fut_spot": 24278.0, "futures_dte": 32},
  )
  sig = CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type="vwap_trend",
    side="CE",
    instrument_token="tok",
    tsym="NIFTY01SEP26C25000",
    strategy_version="test",
    feature_snapshot=feat,
    confidence=80,
  )
  reasons = basis_gate_rejection_reasons(
    sig,
    cfg={
      "enabled": True,
      "max_abs_basis_points": 30,
      "max_basis_pct_of_spot": 0.008,
      "basis_dte_multiplier": 2.5,
    },
  )
  assert reasons == []


def test_basis_gate_exempts_reversal() -> None:
  reasons = basis_gate_rejection_reasons(
    _signal(basis=45.0, setup="peak_reversal_fade"),
    cfg={"enabled": True, "max_abs_basis_points": 30},
  )
  assert reasons == []
