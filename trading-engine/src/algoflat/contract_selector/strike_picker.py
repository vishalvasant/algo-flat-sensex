"""Select CE/PE among ATM ± N and OTM strikes using Greeks + OI + liquidity."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from algoflat.config import AppConfig
from algoflat.contract_selector.selector import ContractUniverse
from algoflat.models.events import Instrument, OptionState
from algoflat.option_data.greeks import compute_greeks


@dataclass
class StrikePick:
  instrument: Instrument
  option_state: OptionState
  score: float
  delta: float | None
  gamma: float | None
  iv: float | None
  theta: float | None
  vega: float | None
  reason: str
  candidates_considered: int
  moneyness: str = "atm"


def effective_quote_band_steps(cfg: dict[str, Any] | None) -> int:
  """Max ATM distance (in strike steps) needed for quotes and band state loading."""
  cfg = cfg or {}
  band = int(cfg.get("atm_band_steps", 1))
  modes = cfg.get("modes") or ["atm"]
  otm_steps = [int(s) for s in (cfg.get("otm_steps") or [])]
  if "otm" in modes and otm_steps:
    return max(band, max(otm_steps))
  return band


def atm_band_instruments(
  universe: ContractUniverse,
  side: str,
  *,
  band_steps: int = 1,
  step: Decimal = Decimal("50"),
) -> list[Instrument]:
  """Return CE or PE contracts at ATM, ATM±1, … ATM±band_steps."""
  atm = universe.atm_strike
  if atm is None:
    return []
  wanted = {atm + step * Decimal(i) for i in range(-band_steps, band_steps + 1)}
  instruments = getattr(universe, "instruments", None) or []
  out = [
    i
    for i in instruments
    if i.option_type == side and i.strike in wanted
  ]
  out.sort(key=lambda i: abs(i.strike - atm))
  return out


def _is_otm(side: str, strike: Decimal, atm: Decimal) -> bool:
  if side == "CE":
    return strike > atm
  return strike < atm


def candidate_instruments(
  universe: ContractUniverse,
  side: str,
  *,
  band_steps: int = 1,
  otm_steps: list[int] | None = None,
  modes: list[str] | None = None,
  step: Decimal = Decimal("50"),
) -> list[Instrument]:
  """Build strike candidates from ATM band and/or explicit OTM steps."""
  atm = universe.atm_strike
  if atm is None:
    return []
  modes = modes or ["atm"]
  otm_steps = otm_steps or []
  wanted: set[Decimal] = set()

  if "atm" in modes:
    for i in range(-band_steps, band_steps + 1):
      wanted.add(atm + step * Decimal(i))

  if "otm" in modes:
    for o in otm_steps:
      offset = step * Decimal(int(o))
      if side == "CE":
        wanted.add(atm + offset)
      else:
        wanted.add(atm - offset)

  instruments = getattr(universe, "instruments", None) or []
  out = [i for i in instruments if i.option_type == side and i.strike in wanted]
  out.sort(key=lambda i: (0 if not _is_otm(side, i.strike, atm) else 1, abs(i.strike - atm)))
  return out


def _atm_in_scope(pick: StrikePick) -> bool:
  """ATM is tradable when quote/filters passed and delta/OI look usable."""
  reason = pick.reason or ""
  if "thin_oi" in reason:
    return False
  if "delta_out_of_band" in reason:
    return False
  return True


def select_option_contract(
  *,
  config: AppConfig,
  universe: ContractUniverse,
  side: str,
  spot: Decimal,
  option_states: dict[str, OptionState | None],
  expiry: date | None = None,
  now: datetime | None = None,
) -> StrikePick | None:
  """Pick best contract for ``side`` using delta/gamma/OI/volume/spread.

  Priority: ATM first when it has scope; otherwise best strike within ATM±band.
  """
  cfg = config.strategy.get("strike_selection") or {}
  if not cfg.get("enabled", True):
    inst = universe.atm_ce if side == "CE" else universe.atm_pe
    if inst is None:
      return None
    state = option_states.get(inst.token)
    if state is None or state.ltp is None:
      return None
    return StrikePick(
      instrument=inst,
      option_state=state,
      score=0.0,
      delta=None,
      gamma=None,
      iv=None,
      reason="atm_only",
      candidates_considered=1,
      moneyness="atm",
    )

  step = Decimal(str((getattr(config, "symbols", None) or {}).get("strike_step", 50)))
  band = int(cfg.get("atm_band_steps", 1))
  prefer_atm_first = bool(cfg.get("prefer_atm_first", True))
  modes = list(cfg.get("modes") or ["atm"])
  otm_steps = [int(s) for s in (cfg.get("otm_steps") or [])]
  atm_target = float(cfg.get("atm_target_delta", cfg.get("target_delta", 0.50)))
  otm_target = float(cfg.get("otm_target_delta", 0.35))
  atm_delta_min = float(cfg.get("delta_min", 0.40))
  atm_delta_max = float(cfg.get("delta_max", 0.65))
  otm_delta_min = float(cfg.get("otm_delta_min", 0.25))
  otm_delta_max = float(cfg.get("otm_delta_max", 0.45))
  prefer_gamma = bool(cfg.get("prefer_higher_gamma", True))
  max_spread = Decimal(str(cfg.get("max_spread_pct", 8)))
  min_oi = int(cfg.get("min_oi", 0))
  oi_weight = float(cfg.get("oi_weight", 15))
  volume_weight = float(cfg.get("volume_weight", 10))
  min_vol = int(cfg.get("min_volume", 0))

  candidates = candidate_instruments(
    universe,
    side,
    band_steps=band,
    otm_steps=otm_steps,
    modes=modes,
    step=step,
  )
  if not candidates:
    candidates = atm_band_instruments(universe, side, band_steps=band, step=step)
  if not candidates:
    inst = universe.atm_ce if side == "CE" else universe.atm_pe
    candidates = [inst] if inst else []

  exp = expiry
  if exp is None:
    for c in candidates:
      if c.expiry_date is not None:
        exp = c.expiry_date.date()
        break

  atm_strike = universe.atm_strike or Decimal("0")
  scored: list[StrikePick] = []
  for inst in candidates:
    state = option_states.get(inst.token)
    if state is None or state.ltp is None or state.ltp <= 0:
      continue
    if state.spread_pct is not None and state.spread_pct > max_spread:
      continue
    if min_vol > 0 and (state.volume is None or int(state.volume) < min_vol):
      continue

    otm = _is_otm(side, inst.strike, atm_strike) if atm_strike else False
    moneyness = "otm" if otm else "atm"
    target_delta = otm_target if otm else atm_target
    delta_min = otm_delta_min if otm else atm_delta_min
    delta_max = otm_delta_max if otm else atm_delta_max

    delta = gamma = iv = theta = vega = None
    score = 0.0
    reason = "ltp_fallback"
    if exp is not None and spot > 0:
      g = compute_greeks(
        spot=float(spot),
        strike=float(inst.strike),
        premium=float(state.ltp),
        option_type=inst.option_type,
        expiry=exp,
        now=now,
      )
      delta = g.delta
      gamma = g.gamma
      iv = g.iv
      theta = g.theta
      vega = g.vega
      if delta is not None:
        abs_d = abs(delta)
        if delta_min <= abs_d <= delta_max:
          score += 40.0 - abs(abs_d - target_delta) * 80.0
          reason = f"{moneyness}_delta_in_band"
        else:
          score += 10.0 - abs(abs_d - target_delta) * 40.0
          reason = f"{moneyness}_delta_out_of_band"
      if prefer_gamma and gamma is not None:
        score += min(gamma * 5000.0, 25.0)
      steps_away = abs(float(inst.strike - atm_strike)) / float(step) if atm_strike else 0
      if otm:
        score += min(6.0, steps_away * 2.0)
      else:
        # Prefer nearer ATM strikes inside the band.
        score += max(0.0, 12.0 - steps_away * 3.0)

    if state.spread_pct is not None:
      score += max(0.0, 10.0 - float(state.spread_pct))
    if state.volume:
      vol_score = min(state.volume / 50_000.0, 1.0) * volume_weight
      score += vol_score
    if state.oi is not None and int(state.oi) > 0:
      if min_oi > 0 and int(state.oi) < min_oi:
        score -= oi_weight * 0.5
        reason = f"{reason}_thin_oi"
      else:
        ref = max(min_oi, 25_000)
        score += min(int(state.oi) / ref, 3.0) * (oi_weight / 3.0)

    scored.append(
      StrikePick(
        instrument=inst,
        option_state=state,
        score=score,
        delta=delta,
        gamma=gamma,
        iv=iv,
        theta=theta,
        vega=vega,
        reason=reason,
        candidates_considered=len(candidates),
        moneyness=moneyness,
      )
    )

  if not scored:
    return None

  scored.sort(key=lambda p: p.score, reverse=True)

  if prefer_atm_first and atm_strike:
    half = float(step) * 0.5
    atm_picks = [
      p for p in scored if abs(float(p.instrument.strike - atm_strike)) < half
    ]
    viable_atm = [p for p in atm_picks if _atm_in_scope(p)]
    if viable_atm:
      best = max(viable_atm, key=lambda p: p.score)
      best.candidates_considered = len(scored)
      if "atm_priority" not in best.reason:
        best.reason = f"{best.reason}_atm_priority"
      return best

  best = scored[0]
  best.candidates_considered = len(scored)
  return best


def pick_meta(pick: StrikePick) -> dict[str, Any]:
  return {
    "strike": float(pick.instrument.strike),
    "tsym": pick.instrument.tsym,
    "token": pick.instrument.token,
    "score": round(pick.score, 2),
    "delta": round(pick.delta, 4) if pick.delta is not None else None,
    "gamma": round(pick.gamma, 6) if pick.gamma is not None else None,
    "iv": round(pick.iv * 100, 2) if pick.iv is not None else None,
    "theta": round(pick.theta, 4) if pick.theta is not None else None,
    "vega": round(pick.vega, 4) if pick.vega is not None else None,
    "pick_reason": pick.reason,
    "moneyness": pick.moneyness,
    "candidates_considered": pick.candidates_considered,
  }
