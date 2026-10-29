"""Lightweight CE/PE entry confirmation — premium + greeks + MTF side score."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from algoflat.models.events import CandidateSignal

if TYPE_CHECKING:
  from algoflat.validator.premium_behaviour import PremiumBehaviourTracker


def option_entry_veto_reasons(
  signal: CandidateSignal,
  option_ltp: float | None,
  *,
  cfg: dict,
  premium_tracker: PremiumBehaviourTracker | None = None,
  now: datetime | None = None,
) -> list[str]:
  """Veto when option premium/greeks disagree with the intended entry."""
  if not cfg.get("enabled", False):
    return []

  exempt = set(cfg.get("exempt_setups") or ["trend_reversal_flip"])
  if signal.setup_type in exempt:
    return []

  apply_setups = set(
    cfg.get("apply_setups")
    or [
      "vwap_trend",
      "vwap_reclaim",
      "vwap_pullback",
      "momentum_continuation",
      "opening_range_breakout",
      "gap_and_go",
      "gap_fade",
      "peak_reversal_fade",
      "level_rejection",
      "pdh_pdl_break",
    ]
  )
  if signal.setup_type not in apply_setups:
    return []

  reasons: list[str] = []
  meta = signal.scanner_metadata or {}
  pick = meta.get("strike_pick") or {}
  feat = signal.feature_snapshot
  extra = (feat.extra or {}) if feat is not None else {}

  mtf_setups = set(
    cfg.get("mtf_weak_apply_setups")
    or [
      "vwap_trend",
      "vwap_reclaim",
      "vwap_pullback",
      "momentum_continuation",
      "delta_momentum",
      "trend_day",
    ]
  )
  if signal.setup_type in mtf_setups:
    min_side_mtf = int(cfg.get("min_side_mtf_score", 42))
    side_key = "mtf_score_ce" if signal.side == "CE" else "mtf_score_pe"
    side_mtf = extra.get(side_key)
    if side_mtf is not None and int(side_mtf) < min_side_mtf:
      reasons.append("option_side_mtf_weak")

  delta = pick.get("delta")
  if delta is not None:
    d = abs(float(delta))
    d_min = float(cfg.get("min_abs_delta", 0.30))
    d_max = float(cfg.get("max_abs_delta", 0.75))
    if d < d_min or d > d_max:
      reasons.append("option_delta_out_of_band")

  spread = pick.get("spread_pct") or meta.get("spread_pct")
  if spread is not None and float(spread) > float(cfg.get("max_spread_pct", 10)):
    reasons.append("option_spread_wide")

  premium_setups = set(
    cfg.get("premium_responsive_apply_setups")
    or [
      "expiry_scalping",
      "level_rejection",
      "peak_reversal_fade",
      "vwap_trend",
      "vwap_reclaim",
      "vwap_pullback",
      "momentum_continuation",
      "delta_momentum",
      "trend_day",
      "gap_and_go",
      "opening_range_breakout",
      "pdh_pdl_break",
      "oi_breakout",
    ]
  )
  if (
    premium_tracker is not None
    and cfg.get("require_premium_responsive", True)
    and signal.setup_type in premium_setups
  ):
    responsive = premium_tracker.is_responsive(
      signal.instrument_token,
      signal.side,
      min_spot_pct=float(cfg.get("min_spot_move_pct", 0.02)),
      min_premium_pct=float(cfg.get("min_premium_move_pct", 0.35)),
      lookback_samples=int(cfg.get("lookback_samples", 4)),
    )
    if responsive is False:
      reasons.append("option_premium_not_responsive")
    elif responsive is None and cfg.get("reject_if_premium_unknown", True):
      reasons.append("option_confirmation_unavailable")

  min_oi = int(cfg.get("min_oi", 0))
  oi = pick.get("oi")
  if min_oi > 0 and oi is not None and int(oi) < min_oi:
    reasons.append("option_oi_low")

  if option_ltp is not None and option_ltp <= 0:
    reasons.append("option_ltp_invalid")

  mi = extra.get("market_intelligence") or {}
  premium = mi.get("premium") or {}
  if cfg.get("require_mi_premium_confirm", True) and premium.get("available"):
    if signal.side == "CE" and not premium.get("ce_confirm"):
      reasons.append("mi_premium_ce_not_confirmed")
    if signal.side == "PE" and not premium.get("pe_confirm"):
      reasons.append("mi_premium_pe_not_confirmed")

  return reasons
