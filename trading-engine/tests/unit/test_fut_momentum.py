from datetime import datetime, timezone
from decimal import Decimal

from algoflat.models.events import Bias, CandidateSignal, FeatureSnapshot
from algoflat.validator.fut_momentum import fut_momentum_rejection_reasons


def _signal(
  side: str,
  *,
  fut: float,
  vwap: float,
  setup: str = "vwap_trend",
  bars_with: int = 3,
  vol_strength: float | None = 1.2,
) -> CandidateSignal:
  extra: dict = {"fut_spot": fut, "bars_with_vwap_3m": bars_with}
  if vol_strength is not None:
    extra["futures_volume_strength"] = vol_strength
  feat = FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    nifty_spot=Decimal(str(fut)),
    session_vwap=Decimal(str(vwap)),
    bias_5m=Bias.BULLISH if side == "CE" else Bias.BEARISH,
    extra=extra,
  )
  return CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type=setup,
    side=side,
    instrument_token="tok",
    tsym="NIFTY01SEP26C25000",
    strategy_version="test",
    feature_snapshot=feat,
    confidence=80,
  )


CFG = {
  "enabled": True,
  "require_vwap_sustain": True,
  "min_bars_with_vwap": 2,
  "require_volume_strength": True,
  "min_volume_strength": 1.0,
  "reject_if_volume_unknown": False,
  "vwap_sustain_setups": ["vwap_trend", "opening_range_breakout"],
  "volume_confirm_setups": ["opening_range_breakout", "gap_and_go"],
}


def test_fut_momentum_blocks_ce_below_vwap() -> None:
  reasons = fut_momentum_rejection_reasons(
    _signal("CE", fut=24950, vwap=25000),
    cfg=CFG,
  )
  assert "fut_momentum_not_aligned" in reasons


def test_fut_momentum_allows_ce_above_vwap() -> None:
  reasons = fut_momentum_rejection_reasons(
    _signal("CE", fut=25050, vwap=25000, bars_with=3),
    cfg=CFG,
  )
  assert reasons == []


def test_fut_momentum_exempts_reversal_setups() -> None:
  reasons = fut_momentum_rejection_reasons(
    _signal("PE", fut=25100, vwap=25000, setup="peak_reversal_fade"),
    cfg=CFG,
  )
  assert reasons == []


def test_fut_vwap_sustain_required_for_trend() -> None:
  reasons = fut_momentum_rejection_reasons(
    _signal("CE", fut=25050, vwap=25000, bars_with=1),
    cfg=CFG,
  )
  assert reasons == ["fut_vwap_not_sustained"]


def test_fut_volume_required_for_breakout() -> None:
  reasons = fut_momentum_rejection_reasons(
    _signal(
      "CE",
      fut=25050,
      vwap=25000,
      setup="opening_range_breakout",
      bars_with=3,
      vol_strength=0.5,
    ),
    cfg=CFG,
  )
  assert "fut_volume_weak" in reasons


def test_fut_trend_skips_volume_gate() -> None:
  """Routine vwap_trend should not require volume surge — only VWAP side + sustain."""
  reasons = fut_momentum_rejection_reasons(
    _signal("CE", fut=25050, vwap=25000, setup="vwap_trend", bars_with=3, vol_strength=0.4),
    cfg=CFG,
  )
  assert reasons == []
