"""Greeks/OI/volume-first opportunity scoring — ranks trade quality beyond regime."""
from __future__ import annotations

from typing import Any

from algoflat.config import AppConfig
from algoflat.features.gap import active_gap_allowlist, gap_mode_cfg
from algoflat.models.events import CandidateSignal, FeatureSnapshot, MarketRegime

CHAIN_DRIVEN_SETUPS = frozenset({"oi_breakout", "delta_momentum", "gamma_expansion", "iv_expansion"})


class OpportunityScorer:
  """Bonus points for chain/greek alignment; soft penalties when regime mismatches."""

  def __init__(self, config: AppConfig) -> None:
    self._config = config
    self._cfg = config.strategy.get("opportunity_score") or {}
    self._regime_cfg = config.strategy.get("regime") or {}
    self.enabled = bool(self._cfg.get("enabled", True))
    self.max_bonus = int(self._cfg.get("max_bonus", 30))

  def score(
    self,
    signal: CandidateSignal,
    features: FeatureSnapshot,
    regime: MarketRegime,
    *,
    context: dict[str, Any] | None = None,
  ) -> tuple[int, list[str]]:
    if not self.enabled:
      return 0, []
    ctx = context or {}
    extra = features.extra or {}
    chain = extra.get("chain") or {}
    logs: list[str] = []
    bonus = 0.0
    side = signal.side
    pick = (signal.scanner_metadata or {}).get("strike_pick") or {}

    if side == "CE" and chain.get("oi_confirms_ce"):
      b = int(self._cfg.get("oi_confirm_bonus", 12))
      bonus += b
      logs.append(f"opp_oi_confirm_ce=+{b}")
    elif side == "PE" and chain.get("oi_confirms_pe"):
      b = int(self._cfg.get("oi_confirm_bonus", 12))
      bonus += b
      logs.append(f"opp_oi_confirm_pe=+{b}")

    if side == "CE" and int(chain.get("oi_delta_ce_atm") or 0) > 0:
      b = int(self._cfg.get("oi_build_bonus", 6))
      bonus += b
      logs.append(f"opp_ce_oi_build=+{b}")
    elif side == "PE" and int(chain.get("oi_delta_pe_atm") or 0) > 0:
      b = int(self._cfg.get("oi_build_bonus", 6))
      bonus += b
      logs.append(f"opp_pe_oi_build=+{b}")

    vol = ctx.get("volume")
    min_vol = int(self._config.validator.get("min_option_volume", 5000))
    if vol is not None and int(vol) >= min_vol:
      b = int(self._cfg.get("volume_bonus", 8))
      bonus += b
      logs.append(f"opp_volume=+{b}")

    oi = ctx.get("oi")
    min_oi = int(self._config.validator.get("min_option_oi", 25000))
    if oi is not None and int(oi) >= min_oi:
      b = int(self._cfg.get("oi_floor_bonus", 6))
      bonus += b
      logs.append(f"opp_oi_floor=+{b}")

    delta = ctx.get("delta")
    moneyness = pick.get("moneyness") or "atm"
    if delta is not None:
      ad = abs(float(delta))
      if moneyness == "otm" and 0.25 <= ad <= 0.45:
        b = int(self._cfg.get("greeks_bonus", 8)) - 2
        bonus += b
        logs.append(f"opp_otm_delta=+{b}")
      elif 0.40 <= ad <= 0.65:
        b = int(self._cfg.get("greeks_bonus", 8))
        bonus += b
        logs.append(f"opp_delta=+{b}")

    gamma = ctx.get("gamma")
    if gamma is not None and float(gamma) >= 0.0008:
      bonus += 3
      logs.append("opp_gamma=+3")

    if signal.setup_type in CHAIN_DRIVEN_SETUPS:
      bonus += 5
      logs.append("opp_chain_setup=+5")

    if chain.get("long_build_up") and (
      (side == "CE" and chain.get("oi_confirms_ce"))
      or (side == "PE" and chain.get("oi_confirms_pe"))
    ):
      bonus += 4
      logs.append("opp_oi_momentum=+4")

    return min(self.max_bonus, int(round(bonus))), logs

  def regime_penalty(
    self,
    strategy_name: str,
    regime: MarketRegime,
    features: FeatureSnapshot,
    *,
    is_expiry_day: bool = False,
  ) -> tuple[int, list[str]]:
    """Soft penalty when strategy is outside regime allowlist (filter_mode=soft)."""
    if self._regime_cfg.get("filter_mode", "soft") != "soft":
      return 0, []

    penalty = 0
    logs: list[str] = []
    extra = features.extra or {}
    gap_cfg = gap_mode_cfg(self._config.strategy)
    gap_pts = extra.get("gap_points")
    gap_allow = active_gap_allowlist(
      float(gap_pts) if gap_pts is not None else None, gap_cfg
    )
    in_open = regime.primary == "opening_range" or (
      regime.health or {}
    ).get("session_phase") == "open"

    if gap_allow is not None and in_open and strategy_name not in gap_allow:
      p = int(self._regime_cfg.get("gap_mode_penalty", 15))
      penalty += p
      logs.append(f"regime_gap_penalty=-{p}")

    if is_expiry_day and bool((self._regime_cfg.get("expiry_mode") or {}).get("enabled", False)):
      allowed = set(
        (self._regime_cfg.get("expiry_mode") or {}).get("allowed_strategies") or []
      )
      if allowed and strategy_name not in allowed:
        p = int(self._regime_cfg.get("expiry_mode_penalty", 12))
        penalty += p
        logs.append(f"regime_expiry_penalty=-{p}")

    if regime.primary == "sideways":
      sideways = self._regime_cfg.get("sideways_allowed_strategies")
      if sideways is not None and strategy_name not in sideways:
        p = int(self._regime_cfg.get("sideways_penalty", 18))
        penalty += p
        logs.append(f"regime_sideways_penalty=-{p}")

    return penalty, logs
