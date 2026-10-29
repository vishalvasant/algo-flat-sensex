from __future__ import annotations

import time
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from algoflat.config import AppConfig
from algoflat.features.indicators import (
  aggregate_from_m5,
  cpr_levels,
  ema,
  opening_range,
)
from algoflat.features.counter_bias import detect_peak_reversal_fade
from algoflat.features.levels import (
  build_level_map,
  detect_level_rejection,
  detect_liquidity_sweep_at_level,
)
from algoflat.features.mtf_patterns import build_mtf_alignment
from algoflat.features.fut_first import fut_first_enabled, requires_1m_trigger, trend_uses_5m_momentum
from algoflat.features.gap import gap_class, gap_mode_cfg
from algoflat.features.setups import detect_all_setups
from algoflat.strategy.expiry_distance import (
  classify_expiry_bucket,
  days_to_expiry,
  expiry_distance_cfg,
)
from algoflat.market_data.engine import MarketDataEngine
from algoflat.market_data.vwap import session_vwap
from algoflat.market_intelligence.spot_fut import build_spot_fut_context
from algoflat.market_intelligence.tick_meta import DataValidity, tick_meta
from algoflat.models.events import Bias, Candle, CandleInterval, FeatureSnapshot
from algoflat.symbols_util import uses_dual_index_spot, uses_futures_price

IST = ZoneInfo("Asia/Kolkata")


class FeatureEngine:
  def __init__(self, config: AppConfig, market_data: MarketDataEngine) -> None:
    self._config = config
    self._market_data = market_data
    self._reclaim = config.strategy.get("vwap_reclaim", {})
    self._pullback = config.strategy.get("vwap_pullback", {})
    # Prior day OHLC for CPR / PDH / PDL / gap (set by orchestrator/backtest).
    self.prior_high: Decimal | None = None
    self.prior_low: Decimal | None = None
    self.prior_close: Decimal | None = None
    self.chain_snapshot: dict = {}
    self.is_expiry_day: bool = False
    self.expiry_date: date | None = None
    self.session_date: date | None = None
    self.option_context: dict = {}
    self._last_levels: dict | None = None
    self._last_snapshot: FeatureSnapshot | None = None
    self._last_snapshot_mono: float = 0.0

  def compute_cached(self, max_age_sec: float = 5.0) -> FeatureSnapshot:
    """Return recent snapshot when dashboard polls faster than scan cadence."""
    now = time.monotonic()
    if (
      self._last_snapshot is not None
      and now - self._last_snapshot_mono < max_age_sec
    ):
      return self._last_snapshot
    return self.compute()

  def set_prior_day(
    self,
    high: Decimal | None,
    low: Decimal | None,
    close: Decimal | None,
  ) -> None:
    self.prior_high = high
    self.prior_low = low
    self.prior_close = close

  def set_chain_snapshot(self, snap: dict) -> None:
    self.chain_snapshot = snap or {}

  def set_option_context(self, ctx: dict) -> None:
    self.option_context = ctx or {}

  def compute(self) -> FeatureSnapshot:
    m1 = self._market_data.candles(CandleInterval.M1)
    m3 = self._market_data.candles(CandleInterval.M3)
    m5 = self._market_data.candles(CandleInterval.M5)
    vwap = session_vwap(m1)
    fut_spot = self._market_data.spot_ltp
    if fut_spot is None and m1:
      fut_spot = m1[-1].close

    cash_spot = self._market_data.cash_index_ltp
    sym_cfg = self._config.symbols
    level_anchor = cash_spot if uses_dual_index_spot(sym_cfg) and cash_spot is not None else fut_spot
    spot = fut_spot

    # Structural bias: prefer 5m close vs VWAP (true higher-TF bias), fall back to 1m.
    price_for_bias = None
    if m5:
      price_for_bias = m5[-1].close
    elif m1:
      price_for_bias = m1[-1].close
    else:
      price_for_bias = spot
    bias = Bias.NEUTRAL
    if vwap and price_for_bias is not None:
      if price_for_bias > vwap:
        bias = Bias.BULLISH
      elif price_for_bias < vwap:
        bias = Bias.BEARISH

    # 1m live LTP can disagree with 5m — keep a secondary flag for trap filters.
    bias_1m = Bias.NEUTRAL
    price_1m = m1[-1].close if m1 else spot
    if vwap and price_1m is not None:
      if price_1m > vwap:
        bias_1m = Bias.BULLISH
      elif price_1m < vwap:
        bias_1m = Bias.BEARISH

    lookback = int(self._reclaim.get("setup_lookback_bars", 5))
    max_dist = Decimal(str(self._reclaim.get("max_distance_to_vwap_points", 15)))
    trigger_lb = int(self._reclaim.get("trigger_lookback_bars", 3))

    setup_3m = _detect_reclaim(m3, vwap, lookback) if vwap else None
    trigger_1m = _detect_reclaim_trigger(m1, vwap, trigger_lb) if vwap else None
    trigger_3m = _detect_reclaim_trigger(m3, vwap, max(2, lookback // 2)) if vwap else None

    # FUT-first: trend setups skip 1m triggers unless explicitly required.
    if fut_first_enabled(self._config.strategy):
      if not requires_1m_trigger(self._config.strategy, "vwap_reclaim"):
        trigger_1m = trigger_3m
      if not requires_1m_trigger(self._config.strategy, "vwap_pullback"):
        pass  # pullback trigger handled below with 3m fallback

    # Drop reclaim labels that disagree with structural bias (avoids
    # "PE setup visible but never bought" / reclaim_side_mismatch).
    if setup_3m == "vwap_reclaim_bull" and bias != Bias.BULLISH:
      setup_3m = None
    elif setup_3m == "vwap_reclaim_bear" and bias != Bias.BEARISH:
      setup_3m = None
    if trigger_1m == "vwap_reclaim_cross_up" and bias != Bias.BULLISH:
      trigger_1m = None
    elif trigger_1m == "vwap_reclaim_cross_down" and bias != Bias.BEARISH:
      trigger_1m = None

    # Distance gate for reclaim setup (proximity to VWAP)
    if setup_3m and spot is not None and vwap is not None:
      dist = abs(spot - vwap)
      if dist > max_dist:
        setup_3m = None

    pullback_setup = None
    if vwap and spot is not None:
      pullback_setup = _detect_pullback(
        m3,
        vwap,
        bias,
        lookback=int(self._pullback.get("setup_lookback_bars", 8)),
        min_extension=Decimal(str(self._pullback.get("min_extension_points", 8))),
        max_distance=Decimal(str(self._pullback.get("max_distance_to_vwap_points", 12))),
      )

    pullback_trigger = _detect_pullback_trigger(
      m1,
      vwap,
      bias,
      lookback=int(self._pullback.get("trigger_lookback_bars", 3)),
    ) if vwap else None
    if fut_first_enabled(self._config.strategy) and not requires_1m_trigger(
      self._config.strategy, "vwap_pullback"
    ):
      pullback_trigger = _detect_pullback_trigger(
        m3,
        vwap,
        bias,
        lookback=max(2, int(self._pullback.get("trigger_lookback_bars", 3))),
      ) or pullback_trigger

    trend_cfg = self._config.strategy.get("vwap_trend", {})
    trend_setup = None
    momentum_m5 = m5 if trend_uses_5m_momentum(self._config.strategy) else m1
    if vwap and spot is not None:
      trend_setup = _detect_trend_continuation(
        m3,
        momentum_m5,
        vwap,
        bias,
        min_bars=int(trend_cfg.get("min_bars_on_side", 3)),
        min_distance=Decimal(str(trend_cfg.get("min_distance_to_vwap_points", 3))),
        max_distance=Decimal(str(trend_cfg.get("max_distance_to_vwap_points", 50))),
        require_momentum=bool(trend_cfg.get("require_1m_momentum", True)),
      )

    structure = _structure_5m(m5, 6)
    distance = float(spot - vwap) if spot is not None and vwap is not None else None
    bars_against = _bars_against_vwap(m3, vwap, bias) if vwap else 0
    bars_with = _bars_with_vwap(m3, vwap, bias) if vwap else 0

    closes_1m = [c.close for c in m1]
    e9 = ema(closes_1m, 9)
    e21 = ema(closes_1m, 21)
    e50 = ema(closes_1m, 50)
    m15 = aggregate_from_m5(m5, 15)
    orb = opening_range(m1, minutes=15)
    cpr = None
    if self.prior_high and self.prior_low and self.prior_close:
      cpr = cpr_levels(self.prior_high, self.prior_low, self.prior_close)

    gap_points = None
    if self.prior_close is not None and m1:
      gap_points = float(m1[0].open - self.prior_close)

    opt_ctx = self.option_context
    ind = {
      "spot": spot,
      "cash_index_spot": cash_spot,
      "fut_spot": spot,
      "level_anchor_spot": level_anchor,
      "ema9": e9,
      "ema21": e21,
      "ema50": e50,
      "or_high": orb.get("or_high") if orb else None,
      "or_low": orb.get("or_low") if orb else None,
      "cpr_pivot": cpr["pivot"] if cpr else None,
      "cpr_tc": cpr["tc"] if cpr else None,
      "cpr_bc": cpr["bc"] if cpr else None,
      "pdh": self.prior_high,
      "pdl": self.prior_low,
      "gap_points": gap_points,
      "abs_distance_to_vwap_points": abs(distance) if distance is not None else None,
      "structure_5m": structure,
      "option_delta": opt_ctx.get("delta"),
      "option_gamma": opt_ctx.get("gamma"),
      "option_iv": opt_ctx.get("iv"),
      "option_iv_change": opt_ctx.get("iv_change"),
      "option_vwap": opt_ctx.get("option_vwap"),
      "option_ltp": opt_ctx.get("ltp"),
      "spread_pct": opt_ctx.get("spread_pct"),
      "option_oi": opt_ctx.get("oi"),
      "option_volume": opt_ctx.get("volume"),
      "bias_1m": bias_1m.value,
    }
    if cpr:
      ind["cpr"] = {
        "pivot": float(cpr["pivot"]),
        "tc": float(cpr["tc"]),
        "bc": float(cpr["bc"]),
      }

    levels_cfg = self._config.strategy.get("levels") or {}
    level_map = build_level_map(
      spot=level_anchor,
      vwap=vwap,
      m5=m5,
      m15=m15,
      ind=ind,
      cfg=levels_cfg,
    )
    level_map = detect_level_rejection(
      spot=level_anchor,
      vwap=vwap,
      bias=bias,
      m5=m5,
      m15=m15,
      level_map=level_map,
      cfg=levels_cfg,
      is_expiry_day=self.is_expiry_day,
    )
    self._last_levels = level_map.to_dict()
    ind["levels_cfg"] = levels_cfg

    existing = {
      "setup_3m": setup_3m,
      "setup_vwap_pullback": pullback_setup,
      "setup_vwap_trend": trend_setup,
    }
    strategy_setups = detect_all_setups(
      bias=bias,
      spot=spot,
      vwap=vwap,
      m1=m1,
      m3=m3,
      m5=m5,
      m15=m15,
      existing=existing,
      ind=ind,
      chain=self.chain_snapshot,
      is_expiry=self.is_expiry_day,
      gap_cfg=gap_mode_cfg(self._config.strategy),
    )

    cb_cfg = self._config.strategy.get("counter_bias") or {}

    mtf = build_mtf_alignment(
      m1=m1,
      m3=m3,
      m5=m5,
      vwap=vwap,
      bias=bias,
      structure_5m=structure,
    )

    counter_bias = detect_peak_reversal_fade(
      bias=bias,
      spot=spot,
      vwap=vwap,
      m5=m5,
      m15=m15,
      ind=ind,
      mtf_score_ce=mtf.score_ce,
      mtf_score_pe=mtf.score_pe,
      cfg=cb_cfg,
    )
    if cb_cfg.get("enabled", True):
      strategy_setups["peak_reversal_fade"] = counter_bias.setup

    if level_map.rejection_setup:
      strategy_setups["level_rejection"] = level_map.rejection_setup
    sweep = detect_liquidity_sweep_at_level(m5, level_map, levels_cfg)
    if sweep:
      strategy_setups["liquidity_sweep"] = sweep

    # Why reclaim/pullback/trend may be inactive (for Decision Logs).
    skip_reasons: list[str] = []
    if not m1:
      skip_reasons.append("no_1m_candles")
    if not m3:
      skip_reasons.append("no_3m_candles")
    if vwap is None:
      skip_reasons.append("no_vwap")
    if spot is None:
      skip_reasons.append("no_spot")
    if setup_3m is None and vwap and spot is not None:
      skip_reasons.append("no_reclaim_setup")
    if trigger_1m is None and vwap:
      skip_reasons.append("no_reclaim_trigger")
    if pullback_setup is None:
      skip_reasons.append("no_pullback_setup")
    if pullback_trigger is None:
      skip_reasons.append("no_pullback_trigger")
    if trend_setup is None:
      skip_reasons.append("no_trend_setup")

    active = [k for k, v in strategy_setups.items() if v]
    if not active:
      skip_reasons.append("no_institutional_setups")

    index_base = not uses_futures_price(sym_cfg)
    basis_points = None
    if not index_base and cash_spot is not None and spot is not None:
      basis_points = round(float(spot) - float(cash_spot), 2)

    futures_dte = None
    fut_tsym = str(sym_cfg.get("fut_tsym") or "")
    if fut_tsym and self.session_date:
      import re
      from algoflat.contract_selector.expiry import parse_expiry_tag

      m = re.match(r"^NIFTY(\d{2}[A-Z]{3}\d{2})F$", fut_tsym.upper())
      if m:
        exp = parse_expiry_tag(m.group(1))
        if exp is not None:
          futures_dte = days_to_expiry(self.session_date, exp)

    spot_fut_ctx = build_spot_fut_context(
      cash_spot=float(cash_spot) if cash_spot is not None else None,
      fut_spot=float(spot) if spot is not None else None,
      cash_vwap=None,
      fut_vwap=float(vwap) if vwap is not None else None,
    )
    tick_now = datetime.now(tz=timezone.utc)
    market_ticks = {
      "cash_spot": tick_meta(
        source="nse_index",
        instrument="NIFTY",
        value=float(cash_spot) if cash_spot is not None else None,
        ts=tick_now,
        validity=DataValidity.OK if cash_spot is not None else DataValidity.MISSING,
      ).to_dict(),
      "fut_spot": tick_meta(
        source="nse_index" if index_base else "nfo_futures",
        instrument="NIFTY" if index_base else "NIFTY_FUT",
        value=float(spot) if spot is not None else None,
        ts=tick_now,
        validity=DataValidity.OK if spot is not None else DataValidity.MISSING,
      ).to_dict(),
    }
    price_label = "nse_index" if index_base else "futures"

    extra = {
      "distance_to_vwap_points": distance,
      "abs_distance_to_vwap_points": abs(distance) if distance is not None else None,
      "cash_index_spot": float(cash_spot) if cash_spot is not None else None,
      "fut_spot": float(spot) if spot is not None else None,
      "level_anchor_spot": float(level_anchor) if level_anchor is not None else None,
      "basis_points": basis_points,
      "futures_dte": futures_dte,
      "fut_tsym": fut_tsym or None,
      "spot_fut": spot_fut_ctx,
      "market_ticks": market_ticks,
      "decision_flow": {
        "spot_context": price_label,
        "levels_anchor": price_label,
        "atm_anchor": price_label,
        "vwap_source": price_label,
        "momentum_source": price_label,
        "volume_source": price_label,
      },
      "structure_5m": structure,
      "setup_vwap_pullback": pullback_setup,
      "trigger_vwap_pullback": pullback_trigger,
      "setup_vwap_trend": trend_setup,
      "bars_against_vwap_3m": bars_against,
      "bars_with_vwap_3m": bars_with,
      "futures_volume_strength": _futures_rel_volume(m1),
      "bias_1m": bias_1m.value,
      "bias_5m_price": float(price_for_bias) if price_for_bias is not None else None,
      "max_distance_to_vwap_points": float(max_dist),
      "setup_lookback_bars": lookback,
      "skip_reasons": skip_reasons,
      "candle_counts": {
        "m1": len(m1),
        "m3": len(m3),
        "m5": len(m5),
        "m15": len(m15),
      },
      "ema9": float(e9) if e9 is not None else None,
      "ema21": float(e21) if e21 is not None else None,
      "ema50": float(e50) if e50 is not None else None,
      "or_high": float(orb["or_high"]) if orb else None,
      "or_low": float(orb["or_low"]) if orb else None,
      "cpr": {k: float(v) for k, v in cpr.items()} if cpr else None,
      "pdh": float(self.prior_high) if self.prior_high is not None else None,
      "pdl": float(self.prior_low) if self.prior_low is not None else None,
      "gap_points": gap_points,
      "gap_class": gap_class(
        gap_points,
        min_gap=float(gap_mode_cfg(self._config.strategy).get("min_gap_points", 35)),
      ),
      "option_vwap": opt_ctx.get("option_vwap"),
      "chain": self.chain_snapshot,
      "strategy_setups": strategy_setups,
      "active_setups": active,
      "mtf_patterns": mtf.details,
      "mtf_score_ce": mtf.score_ce,
      "mtf_score_pe": mtf.score_pe,
      "counter_mtf_score_ce": counter_bias.score_ce,
      "counter_mtf_score_pe": counter_bias.score_pe,
      "counter_bias_signals": counter_bias.signals,
      "counter_bias_m5_labels": counter_bias.m5_labels,
      "counter_bias_m15_labels": counter_bias.m15_labels,
      "bias_confidence_mismatch": counter_bias.bias_confidence_mismatch,
      "mtf_confidence_gap": counter_bias.mtf_confidence_gap,
      "bias_side_mtf_score": counter_bias.bias_side_mtf_score,
      "counter_side_mtf_score": counter_bias.counter_side_mtf_score,
      "counter_bias_trigger": counter_bias.trigger_reason,
      "trigger_3m": trigger_3m,
      "fut_first_enabled": fut_first_enabled(self._config.strategy),
      "is_expiry_day": self.is_expiry_day,
    }

    session_d = self.session_date or datetime.now(IST).date()
    dte: int | None = None
    expiry_bucket: str | None = None
    if self.expiry_date is not None:
      dte = days_to_expiry(session_d, self.expiry_date)
      expiry_bucket = classify_expiry_bucket(
        dte,
        is_expiry_day=self.is_expiry_day,
        cfg=expiry_distance_cfg(self._config.strategy),
      )
    extra.update(
      {
        "session_date": session_d.isoformat(),
        "days_to_expiry": dte,
        "expiry_bucket": expiry_bucket,
        "levels": self._last_levels,
      }
    )

    snapshot = FeatureSnapshot(
      ts=datetime.now(tz=timezone.utc),
      nifty_spot=spot,
      session_vwap=vwap,
      bias_5m=bias,
      setup_3m=setup_3m,
      trigger_1m=trigger_1m,
      extra=extra,
    )
    self._last_snapshot = snapshot
    self._last_snapshot_mono = time.monotonic()
    return snapshot


def _detect_reclaim(
  bars: list[Candle],
  vwap: Decimal,
  lookback: int,
) -> str | None:
  """N-bar VWAP reclaim: prior bar(s) on other side, current close reclaimed."""
  if len(bars) < 2:
    return None
  window = bars[-lookback:] if len(bars) >= lookback else bars
  if len(window) < 2:
    return None
  curr = window[-1].close
  priors = window[:-1]
  # Strict inequality first — avoid bull-wins-on-equal VWAP PE block.
  if curr > vwap and any(b.close < vwap for b in priors):
    return "vwap_reclaim_bull"
  if curr < vwap and any(b.close > vwap for b in priors):
    return "vwap_reclaim_bear"
  # Exact touch: infer direction from the most recent clear prior side.
  if curr == vwap:
    for b in reversed(priors):
      if b.close < vwap:
        return "vwap_reclaim_bull"
      if b.close > vwap:
        return "vwap_reclaim_bear"
  return None


def _detect_reclaim_trigger(
  bars: list[Candle],
  vwap: Decimal,
  lookback: int,
) -> str | None:
  """1m reclaim cross within last lookback bars; most recent cross wins."""
  if len(bars) < 2:
    return None
  start = max(1, len(bars) - lookback)
  found: str | None = None
  for i in range(start, len(bars)):
    prev = bars[i - 1].close
    curr = bars[i].close
    if prev < vwap <= curr:
      found = "vwap_reclaim_cross_up"
    elif prev > vwap >= curr:
      found = "vwap_reclaim_cross_down"
  return found


def _detect_pullback(
  bars: list[Candle],
  vwap: Decimal,
  bias: Bias,
  *,
  lookback: int,
  min_extension: Decimal,
  max_distance: Decimal,
) -> str | None:
  """Trend pullback toward VWAP after an extension, still on bias side."""
  if bias == Bias.NEUTRAL or len(bars) < 3:
    return None
  window = bars[-lookback:] if len(bars) >= lookback else bars
  curr = window[-1].close
  dist = abs(curr - vwap)
  if dist > max_distance:
    return None

  if bias == Bias.BULLISH:
    if curr < vwap:
      return None
    extended = any(b.close >= vwap + min_extension for b in window[:-1])
    if extended and curr <= vwap + max_distance:
      return "vwap_pullback_bull"
  elif bias == Bias.BEARISH:
    if curr > vwap:
      return None
    extended = any(b.close <= vwap - min_extension for b in window[:-1])
    if extended and curr >= vwap - max_distance:
      return "vwap_pullback_bear"
  return None


def _detect_pullback_trigger(
  bars: list[Candle],
  vwap: Decimal | None,
  bias: Bias,
  *,
  lookback: int,
) -> str | None:
  """Bounce confirmation: latest 1m turns back in trend direction near VWAP."""
  if vwap is None or bias == Bias.NEUTRAL or len(bars) < 2:
    return None
  window = bars[-lookback:] if len(bars) >= lookback else bars
  if len(window) < 2:
    return None
  prev = window[-2].close
  curr = window[-1].close
  if bias == Bias.BULLISH and curr > prev and curr >= vwap:
    return "vwap_pullback_bounce_up"
  if bias == Bias.BEARISH and curr < prev and curr <= vwap:
    return "vwap_pullback_bounce_down"
  return None


def _detect_trend_continuation(
  m3: list[Candle],
  m1: list[Candle],
  vwap: Decimal,
  bias: Bias,
  *,
  min_bars: int,
  min_distance: Decimal,
  max_distance: Decimal,
  require_momentum: bool,
) -> str | None:
  """Price already established on VWAP side + optional 1m momentum."""
  if bias == Bias.NEUTRAL or len(m3) < min_bars:
    return None
  window = m3[-min_bars:]
  curr = window[-1].close
  dist = abs(curr - vwap)
  if dist < min_distance or dist > max_distance:
    return None

  if bias == Bias.BULLISH:
    if any(b.close < vwap for b in window):
      return None
    if require_momentum:
      if len(m1) < 2 or m1[-1].close <= m1[-2].close:
        return None
    return "vwap_trend_bull"

  if bias == Bias.BEARISH:
    if any(b.close > vwap for b in window):
      return None
    if require_momentum:
      if len(m1) < 2 or m1[-1].close >= m1[-2].close:
        return None
    return "vwap_trend_bear"
  return None


def _structure_5m(m5: list[Candle], lookback: int) -> str:
  if len(m5) < 3:
    return "mixed"
  window = m5[-lookback:] if len(m5) >= lookback else m5
  highs = [c.high for c in window]
  lows = [c.low for c in window]
  hh = highs[-1] >= max(highs[:-1])
  hl = lows[-1] >= min(lows[:-1])
  ll = lows[-1] <= min(lows[:-1])
  lh = highs[-1] <= max(highs[:-1])
  bull = hh and hl
  bear = ll and lh
  # Tie → mixed so quality gate does not give CE a free +8 over PE.
  if bull and bear:
    return "mixed"
  if bull:
    return "hhhl"
  if bear:
    return "lllh"
  return "mixed"


def _bars_against_vwap(bars: list[Candle], vwap: Decimal, bias: Bias) -> int:
  if not bars or bias == Bias.NEUTRAL:
    return 0
  count = 0
  for b in reversed(bars[-8:]):
    if bias == Bias.BULLISH and b.close < vwap:
      count += 1
    elif bias == Bias.BEARISH and b.close > vwap:
      count += 1
    else:
      break
  return count


def _bars_with_vwap(bars: list[Candle], vwap: Decimal, bias: Bias) -> int:
  if not bars or bias == Bias.NEUTRAL:
    return 0
  count = 0
  for b in reversed(bars[-8:]):
    if bias == Bias.BULLISH and b.close > vwap:
      count += 1
    elif bias == Bias.BEARISH and b.close < vwap:
      count += 1
    else:
      break
  return count


def _futures_rel_volume(m1: list[Candle]) -> float | None:
  """Last 1m volume vs session average — institutional conviction proxy on FUT."""
  vols = [c.volume for c in m1 if c.volume is not None and c.volume > 0]
  if len(vols) < 5:
    return None
  avg = sum(vols) / len(vols)
  if avg <= 0:
    return None
  return round(vols[-1] / avg, 2)
