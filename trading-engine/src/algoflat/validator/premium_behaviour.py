"""Premium responsiveness — spot moved but option premium did not follow."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from algoflat.models.events import CandidateSignal


@dataclass
class _Sample:
  ts: datetime
  spot: float
  premium: float


class PremiumBehaviourTracker:
  """Ring buffer of spot/premium samples per option token."""

  def __init__(self, *, max_samples: int = 12) -> None:
    self._samples: dict[str, deque[_Sample]] = {}
    self._max = max(2, max_samples)

  def record(
    self, token: str, spot: float, premium: float, ts: datetime
  ) -> None:
    if spot <= 0 or premium <= 0:
      return
    q = self._samples.setdefault(token, deque(maxlen=self._max))
    q.append(_Sample(ts=ts, spot=spot, premium=premium))

  def total_samples(self) -> int:
    return sum(len(q) for q in self._samples.values())

  def is_responsive(
    self,
    token: str,
    side: str,
    *,
    min_spot_pct: float = 0.03,
    min_premium_pct: float = 0.5,
    lookback_samples: int = 4,
  ) -> bool | None:
    """True if premium moved with spot. None if insufficient data."""
    q = self._samples.get(token)
    if not q or len(q) < 2:
      return None
    recent = list(q)[-lookback_samples:]
    if len(recent) < 2:
      return None

    oldest = recent[0]
    newest = recent[-1]
    spot_chg_pct = abs(newest.spot - oldest.spot) / oldest.spot * 100
    prem_chg_pct = (newest.premium - oldest.premium) / oldest.premium * 100

    if spot_chg_pct < min_spot_pct:
      return True

    spot_up = newest.spot > oldest.spot
    expected_up = spot_up if side == "CE" else not spot_up
    if expected_up:
      return prem_chg_pct >= min_premium_pct
    return prem_chg_pct <= -min_premium_pct


def premium_responsive_rejection_reasons(
  signal: CandidateSignal,
  tracker: PremiumBehaviourTracker | None,
  cfg: dict,
  *,
  is_expiry_day: bool,
  regime_primary: str | None,
) -> list[str]:
  if not cfg.get("enabled", False) or tracker is None:
    return []

  apply_regimes = set(cfg.get("apply_regimes") or ["expiry_behaviour", "sideways"])
  regime_hit = regime_primary in apply_regimes
  if is_expiry_day and "expiry_behaviour" in apply_regimes:
    regime_hit = True
  if not regime_hit:
    return []

  apply_setups = set(
    cfg.get("apply_setups")
    or ["expiry_scalping", "level_rejection", "peak_reversal_fade"]
  )
  if signal.setup_type not in apply_setups:
    return []

  result = tracker.is_responsive(
    signal.instrument_token,
    signal.side,
    min_spot_pct=float(cfg.get("min_spot_move_pct", 0.03)),
    min_premium_pct=float(cfg.get("min_premium_move_pct", 0.5)),
    lookback_samples=int(cfg.get("lookback_samples", 4)),
  )
  if result is False:
    return ["premium_not_responsive"]
  return []
