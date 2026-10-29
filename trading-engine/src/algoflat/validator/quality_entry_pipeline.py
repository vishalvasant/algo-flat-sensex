"""Quality entry pipeline for option_lift (live + backtests).

Agreed order:
  1) Option lift already detected (candidate)
  2) Greeks — delta = move potential, theta = time-decay drag
  3) Respective CE/PE chart (premium structure / VWAP)
  4) Nifty50 chart — hard veto (3×5m plan + rolling regime + S/R side)
  5) Enter at current option LTP (caller places order)

Enable via ``quality_entry_pipeline.enabled`` in validator_config.yaml.
When disabled, ``quality_pipeline_rejection_reasons`` is a no-op (pass-through).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from algoflat.option_data.greeks import compute_greeks

IST = ZoneInfo("Asia/Kolkata")

DEFAULT_CFG: dict[str, Any] = {
  "enabled": False,
  "test_only": False,
  "apply_setups": ["option_lift"],
  # Floor |Δ|≥0.25 is the agreed live band; max still caps deep ITM.
  "min_abs_delta": 0.25,
  "max_abs_delta": 0.75,
  "enforce_min_abs_delta": True,
  "max_daily_theta_pct_of_premium": 12.0,  # |θ_day| / premium * 100
  "min_premium": 5.0,
  # Option chart (proxy when no option 1m bars)
  "vwap_buffer_pct": 0.50,
  "require_premium_above_vwap": True,
  "allow_option_chart_if_vwap_missing": False,
  "allow_option_chart_if_vwap_mismatch": True,
  # Nifty hard veto
  "level_near_pts": 30.0,
  "swing_lookback_5m": 12,
  "mid_allow_continuation": True,
  "nifty_side_veto": True,
}


@dataclass(frozen=True)
class SpotBar:
  ts: datetime
  o: float
  h: float
  l: float
  c: float
  v: float = 0.0


@dataclass
class PipelineCandidate:
  """Minimal candidate after option_lift detection."""

  ts: datetime
  side: str  # CE | PE
  premium: float
  strike: float
  spot: float
  expiry: date | None = None
  option_vwap: float | None = None
  # Optional precomputed greeks (else derived via BS)
  delta: float | None = None
  theta: float | None = None
  iv: float | None = None
  tsym: str = ""
  meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StageResult:
  name: str
  passed: bool
  reasons: tuple[str, ...] = ()
  detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineResult:
  accepted: bool
  stages: tuple[StageResult, ...]
  entry_ltp: float | None = None

  @property
  def reject_stage(self) -> str | None:
    for s in self.stages:
      if not s.passed:
        return s.name
    return None

  @property
  def reasons(self) -> list[str]:
    out: list[str] = []
    for s in self.stages:
      out.extend(s.reasons)
    return out


def _closed(bars: list[SpotBar], t: datetime, tf: int) -> list[SpotBar]:
  return [b for b in bars if b.ts + timedelta(minutes=tf) <= t]


def _bias_3(bars: list[SpotBar]) -> str | None:
  if len(bars) < 3:
    return None
  w = bars[-3:]
  greens = sum(1 for b in w if b.c >= b.o)
  reds = 3 - greens
  net = w[-1].c - w[0].o
  hh = w[1].h >= w[0].h and w[2].h >= w[1].h
  hl = w[1].l >= w[0].l and w[2].l >= w[1].l
  lh = w[1].h <= w[0].h and w[2].h <= w[1].h
  ll = w[1].l <= w[0].l and w[2].l <= w[1].l
  if greens == 3 and net > 0:
    return "bull"
  if reds == 3 and net < 0:
    return "bear"
  if greens >= 2 and (hh or hl) and net > 0:
    return "bull"
  if reds >= 2 and (lh or ll) and net < 0:
    return "bear"
  return None


def _regime(bars5: list[SpotBar], bars15: list[SpotBar], t: datetime) -> str | None:
  b5 = _closed(bars5, t, 5)
  b15 = _closed(bars15, t, 15)
  plan = _bias_3(b5)
  if plan is None or not b15:
    return None
  last = b15[-1]
  prior = b15[-2].c if len(b15) >= 2 else last.o
  if last.c > prior and last.c >= last.o:
    b15s = "bull"
  elif last.c < prior and last.c <= last.o:
    b15s = "bear"
  else:
    b15s = "bull" if last.c >= last.o else "bear"
  return plan if plan == b15s else None


def _levels(bars5: list[SpotBar], t: datetime, lookback: int, pdh: float, pdl: float) -> dict:
  day = [b for b in _closed(bars5, t, 5) if b.ts.astimezone(IST).date() == t.astimezone(IST).date()]
  if len(day) < 3:
    return {}
  look = day[-lookback:] if len(day) >= lookback else day
  num = den = 0.0
  for b in day:
    tp = (b.h + b.l + b.c) / 3
    vol = b.v if b.v > 0 else 1.0
    num += tp * vol
    den += vol
  return {
    "session_high": max(b.h for b in day),
    "session_low": min(b.l for b in day),
    "swing_hi": max(b.h for b in look),
    "swing_lo": min(b.l for b in look),
    "vwap": num / den,
    "last": day[-1].c,
    "pdh": pdh,
    "pdl": pdl,
  }


def _zone(price: float, lv: dict, near: float) -> str:
  if not lv:
    return "unknown"
  if abs(price - lv["swing_hi"]) <= near or abs(price - lv["session_high"]) <= near or abs(price - lv["pdh"]) <= near:
    return "resistance"
  if abs(price - lv["swing_lo"]) <= near or abs(price - lv["session_low"]) <= near or abs(price - lv["pdl"]) <= near:
    return "support"
  if abs(price - lv["vwap"]) <= near:
    return "resistance" if price <= lv["vwap"] else "support"
  rng = max(lv["swing_hi"] - lv["swing_lo"], 1.0)
  pos = (price - lv["swing_lo"]) / rng
  if pos >= 0.70:
    return "resistance"
  if pos <= 0.30:
    return "support"
  return "mid"


def _daily_theta(theta: float | None) -> float | None:
  """Normalize stored theta to approx ₹/day on the premium."""
  if theta is None:
    return None
  # Model path stores /365; some snapshots look annual-scale (|θ| >> premium).
  if abs(theta) > 5.0:
    return theta / 365.0
  return theta


def stage_greeks(cand: PipelineCandidate, cfg: dict) -> StageResult:
  reasons: list[str] = []
  detail: dict[str, Any] = {}
  premium = float(cand.premium)
  if premium < float(cfg.get("min_premium", 5.0)):
    reasons.append("premium_too_cheap")

  delta = cand.delta
  theta = cand.theta
  if delta is None or theta is None:
    g = compute_greeks(
      spot=cand.spot,
      strike=cand.strike,
      premium=premium,
      option_type=cand.side,
      expiry=cand.expiry,
      now=cand.ts,
    )
    delta = delta if delta is not None else g.delta
    theta = theta if theta is not None else g.theta
    detail["iv"] = g.iv
    detail["greeks_source"] = "model"
  else:
    detail["greeks_source"] = "snapshot"

  detail["delta"] = delta
  detail["theta_raw"] = theta
  if delta is None:
    reasons.append("greeks_unavailable")
    return StageResult("greeks", False, tuple(reasons), detail)

  ad = abs(float(delta))
  d_min = float(cfg.get("min_abs_delta", 0.30))
  d_max = float(cfg.get("max_abs_delta", 0.75))
  detail["abs_delta"] = ad
  detail["delta_band"] = [d_min, d_max]
  # Max is always enforced (avoid deep ITM / expensive). Min only when enabled —
  # matches live option_lift (not gated by option_entry_veto min_abs_delta).
  if ad > d_max:
    reasons.append("delta_too_high_expensive")
  if cfg.get("enforce_min_abs_delta", False) and ad < d_min:
    reasons.append("delta_too_low_no_move_potential")
  elif ad < d_min:
    detail["delta_below_band_advisory"] = True

  th_day = _daily_theta(float(theta) if theta is not None else None)
  detail["theta_day"] = th_day
  if th_day is not None and premium > 0:
    theta_pct = abs(th_day) / premium * 100.0
    detail["theta_pct_of_premium"] = theta_pct
    max_pct = float(cfg.get("max_daily_theta_pct_of_premium", 12.0))
    if theta_pct > max_pct:
      reasons.append("theta_decay_too_high")

  return StageResult("greeks", not reasons, tuple(reasons), detail)


def stage_option_chart(cand: PipelineCandidate, cfg: dict) -> StageResult:
  """Premium chart check — VWAP proxy when option 1m history is absent."""
  reasons: list[str] = []
  detail: dict[str, Any] = {"mode": "vwap_proxy"}
  vwap = cand.option_vwap
  detail["option_vwap"] = vwap
  detail["premium"] = cand.premium
  if vwap is None or vwap <= 0:
    if cfg.get("allow_option_chart_if_vwap_missing", False):
      return StageResult("option_chart", True, (), detail)
    reasons.append("option_vwap_unavailable")
    return StageResult("option_chart", False, tuple(reasons), detail)

  # Snapshot often stores ATM VWAP while fill is a wing — ignore mismatched VWAP.
  if cand.premium > 0 and abs(vwap - cand.premium) / max(cand.premium, 1e-6) > float(
    cfg.get("max_vwap_premium_divergence", 0.45)
  ):
    detail["mode"] = "vwap_mismatch_skip"
    detail["vwap_mismatch"] = True
    if cfg.get("allow_option_chart_if_vwap_mismatch", True):
      return StageResult("option_chart", True, (), detail)
    reasons.append("option_vwap_mismatch")
    return StageResult("option_chart", False, tuple(reasons), detail)

  if cfg.get("require_premium_above_vwap", True):
    buf = float(cfg.get("vwap_buffer_pct", 0.50)) / 100.0
    floor = vwap * (1.0 - buf)
    detail["vwap_floor"] = floor
    if cand.premium < floor:
      reasons.append("option_premium_below_vwap")

  return StageResult("option_chart", not reasons, tuple(reasons), detail)


def stage_nifty_chart(
  cand: PipelineCandidate,
  bars5: list[SpotBar],
  bars15: list[SpotBar],
  cfg: dict,
  *,
  pdh: float,
  pdl: float,
) -> StageResult:
  reasons: list[str] = []
  t = cand.ts
  lookback = int(cfg.get("swing_lookback_5m", 12))
  near = float(cfg.get("level_near_pts", 30.0))
  plan = _bias_3(_closed(bars5, t, 5))
  reg = _regime(bars5, bars15, t)
  lv = _levels(bars5, t, lookback, pdh, pdl)
  px = lv.get("last", cand.spot)
  zone = _zone(px, lv, near)
  detail = {
    "plan": plan,
    "regime": reg,
    "zone": zone,
    "spot": px,
    "swing": f"{lv.get('swing_lo', 0):.0f}-{lv.get('swing_hi', 0):.0f}" if lv else None,
  }

  if reg is None:
    reasons.append("nifty_regime_not_aligned")
    return StageResult("nifty_chart", False, tuple(reasons), detail)

  side = cand.side.upper()
  allowed: set[str] = set()
  # Regime-aligned both ways at S/R: breakout/breakdown OR bounce/rejection.
  # Previously only fades were allowed (PE@R / CE@S), which blocked most
  # continuation entries when spot pressed a level in a trending regime.
  if not cfg.get("nifty_side_veto", True):
    allowed = {"CE", "PE"}
  elif zone == "resistance":
    if reg == "bull":
      allowed = {"CE"}  # breakout through resistance
    elif reg == "bear":
      allowed = {"PE"}  # rejection from resistance
  elif zone == "support":
    if reg == "bear":
      allowed = {"PE"}  # breakdown through support
    elif reg == "bull":
      allowed = {"CE"}  # bounce from support
  elif zone == "mid" and cfg.get("mid_allow_continuation", True):
    allowed = {"CE"} if reg == "bull" else {"PE"}

  detail["allowed_sides"] = sorted(allowed)
  if side not in allowed:
    reasons.append(f"nifty_side_veto_{side}_at_{zone}_regime_{reg}")

  return StageResult("nifty_chart", not reasons, tuple(reasons), detail)


def candles_to_spot_bars(candles: Sequence[Any] | None) -> list[SpotBar]:
  """Convert Candle-like objects (open/high/low/close/ts[/volume]) to SpotBar."""
  out: list[SpotBar] = []
  for c in candles or []:
    ts = getattr(c, "ts", None)
    if ts is None:
      continue
    out.append(
      SpotBar(
        ts=ts,
        o=float(getattr(c, "open", getattr(c, "o", 0)) or 0),
        h=float(getattr(c, "high", getattr(c, "h", 0)) or 0),
        l=float(getattr(c, "low", getattr(c, "l", 0)) or 0),
        c=float(getattr(c, "close", getattr(c, "c", 0)) or 0),
        v=float(getattr(c, "volume", getattr(c, "v", 0)) or 0),
      )
    )
  return out


def quality_pipeline_rejection_reasons(
  *,
  setup_type: str,
  side: str,
  premium: float,
  strike: float,
  spot: float,
  expiry: date | None,
  option_vwap: float | None,
  delta: float | None,
  theta: float | None,
  tsym: str,
  bars5: list[SpotBar],
  bars15: list[SpotBar],
  cfg: dict | None,
  pdh: float = 0.0,
  pdl: float = 0.0,
  ts: datetime | None = None,
) -> list[str]:
  """RuleValidator hook — empty when disabled or setup not in apply_setups."""
  merged = dict(DEFAULT_CFG)
  if cfg:
    merged.update(cfg)
  if not merged.get("enabled", False):
    return []
  apply = set(merged.get("apply_setups") or ["option_lift"])
  if setup_type not in apply:
    return []
  cand = PipelineCandidate(
    ts=ts or datetime.now(tz=IST),
    side=side,
    premium=premium,
    strike=strike,
    spot=spot,
    expiry=expiry,
    option_vwap=option_vwap,
    delta=delta,
    theta=theta,
    tsym=tsym,
  )
  result = run_quality_pipeline(
    cand, bars5=bars5, bars15=bars15, cfg=merged, pdh=pdh, pdl=pdl
  )
  if result.accepted:
    return []
  # Prefix stage for journal clarity; keep stage reasons intact.
  stage = result.reject_stage or "quality"
  reasons = list(result.reasons) or [f"{stage}_rejected"]
  return [f"quality_{r}" if not r.startswith("quality_") else r for r in reasons]


def run_quality_pipeline(
  cand: PipelineCandidate,
  *,
  bars5: list[SpotBar],
  bars15: list[SpotBar],
  cfg: dict | None = None,
  pdh: float = 0.0,
  pdl: float = 0.0,
) -> PipelineResult:
  """Run lift→greeks→option chart→Nifty veto."""
  merged = dict(DEFAULT_CFG)
  if cfg:
    merged.update(cfg)
  if not merged.get("enabled", False):
    return PipelineResult(
      accepted=False,
      stages=(StageResult("pipeline", False, ("quality_entry_pipeline_disabled",), {}),),
      entry_ltp=None,
    )

  stages: list[StageResult] = []
  # Stage 1 implied: caller already has option_lift
  stages.append(StageResult("option_lift", True, (), {"assumed": True}))

  g = stage_greeks(cand, merged)
  stages.append(g)
  if not g.passed:
    return PipelineResult(False, tuple(stages), None)

  oc = stage_option_chart(cand, merged)
  stages.append(oc)
  if not oc.passed:
    return PipelineResult(False, tuple(stages), None)

  nf = stage_nifty_chart(cand, bars5, bars15, merged, pdh=pdh, pdl=pdl)
  stages.append(nf)
  if not nf.passed:
    return PipelineResult(False, tuple(stages), None)

  return PipelineResult(True, tuple(stages), entry_ltp=float(cand.premium))
