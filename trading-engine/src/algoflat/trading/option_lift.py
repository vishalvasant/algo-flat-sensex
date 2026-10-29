"""Entry from a live ATM±N option watch.

A call or put in the band is a candidate when its own premium is rising or its
last minute shows a volume step-up. The index only confirms the side: a call
needs NIFTY above VWAP or an up move, a put needs NIFTY below VWAP or a down move.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from algoflat.contract_selector.selector import ContractUniverse
from algoflat.contract_selector.strike_picker import atm_band_instruments
from algoflat.models.events import Candle, CandidateSignal, FeatureSnapshot, OptionState
from algoflat.validator.option_chart_confirm import closed_bars_before

SETUP_TYPE = "option_lift"


def _pct(old: Decimal, new: Decimal) -> float:
  if old <= 0:
    return 0.0
  return float((new - old) / old * Decimal("100"))


def _index_move(spot_m1: list[Candle], when: datetime, lookback: int) -> float | None:
  closed = closed_bars_before(spot_m1, when)
  if len(closed) <= lookback:
    return None
  return float(closed[-1].close - closed[-lookback - 1].close)


def index_confirms_side(
  side: str,
  *,
  spot: Decimal | None,
  vwap: Decimal | None,
  spot_m1: list[Candle],
  when: datetime,
  cfg: dict[str, Any],
) -> tuple[bool, str]:
  """True when the index agrees with buying this side.

  A call qualifies above VWAP, or when the short move is up. A put is the mirror.
  A flat index on the right side of VWAP is still a buy. Both sides can qualify
  on the same scan when each rule is met.
  """
  move = _index_move(spot_m1, when, max(1, int(cfg.get("index_lookback_bars", 3))))
  min_pts = float(cfg.get("min_index_move_points", 8))
  above = spot is not None and vwap is not None and spot > vwap
  below = spot is not None and vwap is not None and spot < vwap
  rising = move is not None and move >= min_pts
  falling = move is not None and move <= -min_pts
  if side == "CE":
    if above and rising:
      return True, "above_vwap_and_rising"
    if rising:
      return True, "index_rising"
    if above:
      return True, "above_vwap"
    return False, "index_not_bullish"
  if below and falling:
    return True, "below_vwap_and_falling"
  if falling:
    return True, "index_falling"
  if below:
    return True, "below_vwap"
  return False, "index_not_bearish"


def _lift_on_bars(
  bars: list[Candle],
  when: datetime,
  cfg: dict[str, Any],
) -> tuple[bool, float, float, datetime | None]:
  """Return (qualifies, premium_rise_pct, volume_ratio, last_bar_ts)."""
  lookback = max(2, int(cfg.get("lookback_bars", 3)))
  vol_lb = max(2, int(cfg.get("volume_lookback_bars", 5)))
  closed = closed_bars_before(bars, when)
  need = max(lookback + 1, vol_lb + 1, int(cfg.get("min_closed_bars", 6)))
  if len(closed) < need:
    return False, 0.0, 0.0, None
  last = closed[-1]
  if last.close < last.open:
    return False, 0.0, 0.0, last.ts
  anchor = closed[-lookback - 1].close
  rise = _pct(anchor, last.close)
  prev = [b.volume or 0 for b in closed[-(vol_lb + 1):-1]]
  avg = sum(prev) / len(prev) if prev else 0.0
  last_vol = float(last.volume or 0)
  ratio = (last_vol / avg) if avg > 0 else 0.0
  min_rise = float(cfg.get("min_premium_rise_pct", 0.75))
  min_ratio = float(cfg.get("min_volume_ratio", 1.8))
  min_bar_vol = float(cfg.get("min_bar_volume", 1000))
  premium_ok = rise >= min_rise
  volume_ok = ratio >= min_ratio and last_vol >= min_bar_vol
  return premium_ok or volume_ok, rise, ratio, last.ts


def find_option_lift(
  *,
  features: FeatureSnapshot,
  universe: ContractUniverse,
  band_states: dict[str, OptionState | None],
  option_m1_by_token: dict[str, list[Candle]],
  spot_m1: list[Candle],
  cfg: dict[str, Any],
  strategy_version: str,
  when: datetime | None = None,
) -> tuple[CandidateSignal | None, dict[str, Any]]:
  """Pick the strongest confirmed lift inside ATM ± band, or explain the miss."""
  when = when or datetime.now(tz=timezone.utc)
  band = int(cfg.get("atm_band_steps", 2))
  step = Decimal(str(cfg.get("strike_step", 50)))
  extra = features.extra or {}
  cash = extra.get("cash_index_spot")
  spot = Decimal(str(cash)) if cash is not None else features.nifty_spot
  vwap = features.session_vwap
  note: dict[str, Any] = {
    "watched": 0,
    "side": None,
    "tsym": None,
    "reason": "watching ATM band",
    "premium_rise_pct": None,
    "volume_ratio": None,
    "index_confirm": None,
    "bar_ts": None,
  }
  if universe.atm_strike is None or spot is None:
    note["reason"] = "ATM or index quote missing"
    return None, note

  ranked: list[tuple[float, CandidateSignal, dict[str, Any]]] = []
  watched = 0
  blocked_index = 0
  quiet = 0
  for side in ("CE", "PE"):
    ok, why = index_confirms_side(
      side, spot=spot, vwap=vwap, spot_m1=spot_m1, when=when, cfg=cfg
    )
    contracts = atm_band_instruments(universe, side, band_steps=band, step=step)
    for inst in contracts:
      watched += 1
      if not ok:
        blocked_index += 1
        continue
      state = band_states.get(inst.token)
      if state is None or state.ltp is None:
        continue
      qualifies, rise, ratio, bar_ts = _lift_on_bars(
        option_m1_by_token.get(inst.token) or [], when, cfg
      )
      if not qualifies or bar_ts is None:
        quiet += 1
        continue
      trigger = "both" if rise >= float(cfg.get("min_premium_rise_pct", 0.75)) and ratio >= float(cfg.get("min_volume_ratio", 1.8)) else (
        "premium" if rise >= float(cfg.get("min_premium_rise_pct", 0.75)) else "volume"
      )
      # Prefer the side the index agrees with on both VWAP and the short move.
      align = 12.0 if why in ("above_vwap_and_rising", "below_vwap_and_falling") else 4.0
      score = align + max(rise, 0.0) * 2.0 + min(ratio, 5.0)
      confidence = max(76, min(96, 78 + int(max(rise, 0.0))))
      signal = CandidateSignal(
        ts=when,
        setup_type=SETUP_TYPE,
        side=side,
        instrument_token=inst.token,
        tsym=inst.tsym,
        strategy_version=strategy_version,
        feature_snapshot=features,
        confidence=confidence,
        scanner_metadata={
          "atm_strike": str(universe.atm_strike),
          "option_ltp": str(state.ltp),
          "exchange": inst.exchange,
          "lot_size": inst.lot_size,
          "strike": str(inst.strike),
          "lift": trigger,
          "premium_rise_pct": round(rise, 3),
          "volume_ratio": round(ratio, 3),
          "index_confirm": why,
          "bar_ts": bar_ts.isoformat(),
        },
      )
      row = {
        "side": side,
        "tsym": inst.tsym,
        "premium_rise_pct": round(rise, 3),
        "volume_ratio": round(ratio, 3),
        "index_confirm": why,
        "bar_ts": bar_ts.isoformat(),
        "lift": trigger,
        "reason": (
          f"{side} {inst.tsym} {trigger} "
          f"premium {rise:+.2f}% vol {ratio:.1f}x · {why.replace('_', ' ')}"
        ),
      }
      ranked.append((score, signal, row))

  note["watched"] = watched
  ranked.sort(key=lambda item: item[0], reverse=True)
  if ranked:
    _score, signal, row = ranked[0]
    note.update(row)
    signals = [item[1] for item in ranked]
    note["candidates"] = len(signals)
    note["_signals"] = signals
    if len(signals) > 1:
      names = ", ".join(sig.tsym for sig in signals[:6])
      why = str((signal.scanner_metadata or {}).get("index_confirm") or "").replace("_", " ")
      note["reason"] = f"{len(signals)} lifts: {names}" + (f" · {why}" if why else "")
    return signal, note
  if watched == 0:
    note["reason"] = "no ATM±2 contracts in the chain"
  elif blocked_index == watched:
    _ce_ok, ce_why = index_confirms_side(
      "CE", spot=spot, vwap=vwap, spot_m1=spot_m1, when=when, cfg=cfg
    )
    note["reason"] = "index has not confirmed a side"
    note["index_confirm"] = ce_why
  else:
    side_txt = "below VWAP" if vwap is not None and spot < vwap else "above VWAP" if vwap is not None and spot > vwap else "index"
    note["reason"] = f"watching {watched} ATM±{band} contracts · NIFTY {side_txt} · no premium or volume lift"
  return None, note



def split_confirmed_lifts(
  signals: list[CandidateSignal],
  streaks: dict[str, int],
  *,
  required: int = 2,
) -> tuple[list[CandidateSignal], list[tuple[CandidateSignal, int]], dict[str, int]]:
  """Return lifts that have qualified on `required` consecutive passes.

  A contract that drops out of this pass loses its streak. The second list is
  still waiting, each item as (signal, streak so far).
  """
  required = max(1, int(required))
  seen: dict[str, CandidateSignal] = {}
  order: list[str] = []
  for sig in signals:
    key = f"{sig.instrument_token}:{sig.side}"
    if key in seen:
      continue
    seen[key] = sig
    order.append(key)
  updated: dict[str, int] = {}
  ready: list[CandidateSignal] = []
  waiting: list[tuple[CandidateSignal, int]] = []
  for key in order:
    streak = int(streaks.get(key, 0)) + 1
    updated[key] = streak
    if streak >= required:
      ready.append(seen[key])
    else:
      waiting.append((seen[key], streak))
  return ready, waiting, updated


def find_option_lifts(
  **kwargs: Any,
) -> tuple[list[CandidateSignal], dict[str, Any]]:
  """Every index-confirmed lift in the band, strongest first."""
  _signal, note = find_option_lift(**kwargs)
  signals = list(note.get("_signals") or [])
  return signals, note


def remap_lift_to_opposite_atm(
  signal: CandidateSignal,
  *,
  universe: ContractUniverse,
  band_states: dict[str, OptionState | None],
  step: Decimal,
  target_steps: int = 2,
  atm_plus_only: bool = True,
) -> CandidateSignal | None:
  """Fade map: CE ATM+ → PE ATM-N, PE ATM+ (OTM) → CE ATM+N.

  Returns a new signal on the opposite contract, or None if it cannot be mapped.
  """
  atm = universe.atm_strike
  if atm is None:
    return None
  meta = dict(signal.scanner_metadata or {})
  try:
    src_strike = Decimal(str(meta.get("strike") or "0"))
  except Exception:
    return None
  if src_strike <= 0:
    return None

  steps = max(1, int(target_steps))
  if signal.side == "CE":
    if atm_plus_only and src_strike <= atm:
      return None
    target_side = "PE"
    target_strike = atm - step * Decimal(steps)
  elif signal.side == "PE":
    # PE ATM+ = OTM put = strike below ATM
    if atm_plus_only and src_strike >= atm:
      return None
    target_side = "CE"
    target_strike = atm + step * Decimal(steps)
  else:
    return None

  instruments = getattr(universe, "instruments", None) or []
  inst = next(
    (
      i
      for i in instruments
      if i.option_type == target_side and i.strike == target_strike
    ),
    None,
  )
  if inst is None:
    return None
  state = band_states.get(inst.token)
  if state is None or state.ltp is None or state.ltp <= 0:
    return None

  new_meta = dict(meta)
  new_meta.update(
    {
      "strike": str(inst.strike),
      "option_ltp": str(state.ltp),
      "exchange": inst.exchange,
      "lot_size": inst.lot_size,
      "opposite_trade": True,
      "opposite_of_side": signal.side,
      "opposite_of_tsym": signal.tsym,
      "opposite_of_token": signal.instrument_token,
      "opposite_of_strike": str(src_strike),
      "atm_strike": str(atm),
      "lift": f"opposite_of_{meta.get('lift') or 'lift'}",
    }
  )
  return signal.model_copy(
    update={
      "side": target_side,
      "instrument_token": inst.token,
      "tsym": inst.tsym,
      "scanner_metadata": new_meta,
      "setup_type": SETUP_TYPE,
    }
  )


def apply_opposite_trade_today(
  signals: list[CandidateSignal],
  *,
  universe: ContractUniverse,
  band_states: dict[str, OptionState | None],
  cfg: dict[str, Any],
  now: datetime | None = None,
) -> list[CandidateSignal]:
  """If opposite_trade_today is enabled for this IST session date, remap lifts."""
  from zoneinfo import ZoneInfo

  opp = cfg.get("opposite_trade_today") or {}
  if not opp.get("enabled", False):
    return signals
  session = str(opp.get("session_date") or "").strip()
  if not session:
    return signals
  ist = ZoneInfo("Asia/Kolkata")
  clock = (now or datetime.now(tz=timezone.utc)).astimezone(ist).date().isoformat()
  if clock != session:
    return signals

  step = Decimal(str(cfg.get("strike_step", 50)))
  target_steps = int(opp.get("target_steps", 2))
  atm_plus_only = bool(opp.get("atm_plus_only", True))
  out: list[CandidateSignal] = []
  seen: set[str] = set()
  for sig in signals:
    mapped = remap_lift_to_opposite_atm(
      sig,
      universe=universe,
      band_states=band_states,
      step=step,
      target_steps=target_steps,
      atm_plus_only=atm_plus_only,
    )
    chosen = mapped or sig
    if chosen.instrument_token in seen:
      continue
    seen.add(chosen.instrument_token)
    out.append(chosen)
  return out
