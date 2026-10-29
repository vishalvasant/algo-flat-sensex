"""Support/resistance map from prior-day levels, session range, and swing structure."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from algoflat.models.events import Bias, Candle


@dataclass
class LevelMap:
    supports: list[float] = field(default_factory=list)
    resistances: list[float] = field(default_factory=list)
    nearest_support: float | None = None
    nearest_resistance: float | None = None
    distance_to_support_pts: float | None = None
    distance_to_resistance_pts: float | None = None
    at_support: bool = False
    at_resistance: bool = False
    confluence_support: int = 0
    confluence_resistance: int = 0
    level_labels: dict[str, float] = field(default_factory=dict)
    rejection_setup: str | None = None
    rejection_level: str | None = None
    rejection_signals: list[str] = field(default_factory=list)
    break_confirmed: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "supports": self.supports,
            "resistances": self.resistances,
            "nearest_support": self.nearest_support,
            "nearest_resistance": self.nearest_resistance,
            "distance_to_support_pts": self.distance_to_support_pts,
            "distance_to_resistance_pts": self.distance_to_resistance_pts,
            "at_support": self.at_support,
            "at_resistance": self.at_resistance,
            "confluence_support": self.confluence_support,
            "confluence_resistance": self.confluence_resistance,
            "level_labels": self.level_labels,
            "rejection_setup": self.rejection_setup,
            "rejection_level": self.rejection_level,
            "rejection_signals": self.rejection_signals,
            "break_confirmed": self.break_confirmed,
        }


def _f(val: Decimal | float | int | None) -> float | None:
    if val is None:
        return None
    return float(val)


def _swing_highs(candles: list[Candle], *, left: int = 2, right: int = 2) -> list[float]:
    if len(candles) < left + right + 1:
        return []
    out: list[float] = []
    for i in range(left, len(candles) - right):
        window = candles[i - left : i + right + 1]
        h = candles[i].high
        if all(h >= c.high for c in window):
            out.append(float(h))
    return out


def _swing_lows(candles: list[Candle], *, left: int = 2, right: int = 2) -> list[float]:
    if len(candles) < left + right + 1:
        return []
    out: list[float] = []
    for i in range(left, len(candles) - right):
        window = candles[i - left : i + right + 1]
        lo = candles[i].low
        if all(lo <= c.low for c in window):
            out.append(float(lo))
    return out


def _cluster_levels(levels: list[float], *, step: float = 5.0) -> list[float]:
    if not levels:
        return []
    sorted_lvls = sorted(levels)
    clusters: list[list[float]] = [[sorted_lvls[0]]]
    for px in sorted_lvls[1:]:
        if abs(px - clusters[-1][-1]) <= step:
            clusters[-1].append(px)
        else:
            clusters.append([px])
    return [sum(c) / len(c) for c in clusters]


def _nearest_below(spot: float, levels: list[float]) -> float | None:
    below = [lv for lv in levels if lv <= spot + 0.01]
    return max(below) if below else None


def _nearest_above(spot: float, levels: list[float]) -> float | None:
    above = [lv for lv in levels if lv >= spot - 0.01]
    return min(above) if above else None


def _count_near(spot: float, levels: list[float], proximity: float) -> int:
    return sum(1 for lv in levels if abs(spot - lv) <= proximity)


def build_level_map(
    *,
    spot: Decimal | None,
    vwap: Decimal | None,
    m5: list[Candle],
    m15: list[Candle],
    ind: dict[str, Any],
    cfg: dict[str, Any] | None = None,
) -> LevelMap:
    """Aggregate S/R from PDH/PDL, CPR, OR, session range, swings, VWAP."""
    cfg = cfg or {}
    proximity = float(cfg.get("proximity_points", 20))
    swing_lookback = int(cfg.get("swing_lookback_bars", 6))
    cluster_step = float(cfg.get("cluster_step_points", 10))

    labels: dict[str, float] = {}
    supports: list[float] = []
    resistances: list[float] = []

    pdh, pdl = ind.get("pdh"), ind.get("pdl")
    if pdh is not None:
        labels["pdh"] = float(pdh)
        resistances.append(float(pdh))
    if pdl is not None:
        labels["pdl"] = float(pdl)
        supports.append(float(pdl))

    cpr = ind.get("cpr") or {}
    if isinstance(cpr, dict):
        if cpr.get("pivot") is not None:
            labels["cpr_pivot"] = float(cpr["pivot"])
            supports.append(float(cpr["pivot"]))
            resistances.append(float(cpr["pivot"]))
        if cpr.get("bc") is not None:
            labels["cpr_bc"] = float(cpr["bc"])
            supports.append(float(cpr["bc"]))
        if cpr.get("tc") is not None:
            labels["cpr_tc"] = float(cpr["tc"])
            resistances.append(float(cpr["tc"]))

    or_h, or_l = ind.get("or_high"), ind.get("or_low")
    if or_h is not None:
        labels["or_high"] = float(or_h)
        resistances.append(float(or_h))
    if or_l is not None:
        labels["or_low"] = float(or_l)
        supports.append(float(or_l))

    if vwap is not None:
        labels["vwap"] = float(vwap)
        supports.append(float(vwap))
        resistances.append(float(vwap))

    session_bars = m5 if m5 else m15
    if session_bars:
        labels["session_high"] = float(max(c.high for c in session_bars))
        labels["session_low"] = float(min(c.low for c in session_bars))
        resistances.append(labels["session_high"])
        supports.append(labels["session_low"])

    for sh in _swing_highs(m5[-swing_lookback * 3 :] if m5 else [], left=1, right=1)[-3:]:
        resistances.append(sh)
    for sl in _swing_lows(m5[-swing_lookback * 3 :] if m5 else [], left=1, right=1)[-3:]:
        supports.append(sl)
    for sh in _swing_highs(m15[-swing_lookback * 2 :] if m15 else [], left=1, right=1)[-2:]:
        resistances.append(sh)
    for sl in _swing_lows(m15[-swing_lookback * 2 :] if m15 else [], left=1, right=1)[-2:]:
        supports.append(sl)

    supports = _cluster_levels(supports, step=cluster_step)
    resistances = _cluster_levels(resistances, step=cluster_step)

    spot_f = _f(spot)
    result = LevelMap(
        supports=supports,
        resistances=resistances,
        level_labels=labels,
    )
    if spot_f is None:
        return result

    result.nearest_support = _nearest_below(spot_f, supports)
    result.nearest_resistance = _nearest_above(spot_f, resistances)
    if result.nearest_support is not None:
        result.distance_to_support_pts = round(spot_f - result.nearest_support, 2)
        result.at_support = abs(spot_f - result.nearest_support) <= proximity
    if result.nearest_resistance is not None:
        result.distance_to_resistance_pts = round(result.nearest_resistance - spot_f, 2)
        result.at_resistance = abs(result.nearest_resistance - spot_f) <= proximity
    result.confluence_support = _count_near(spot_f, supports, proximity)
    result.confluence_resistance = _count_near(spot_f, resistances, proximity)
    if m5:
        if result.nearest_resistance is not None:
            result.break_confirmed["resistance"] = breakout_confirmed(
                side="bull",
                level=result.nearest_resistance,
                m5=m5,
                cfg=cfg,
            )
        if result.nearest_support is not None:
            result.break_confirmed["support"] = breakout_confirmed(
                side="bear",
                level=result.nearest_support,
                m5=m5,
                cfg=cfg,
            )
    return result


def level_entry_block_reasons(
    *,
    side: str,
    spot: Decimal | None,
    level_map: LevelMap,
    setup_type: str,
    m5: list[Candle] | None = None,
    cfg: dict[str, Any] | None = None,
) -> list[str]:
    """Phase B — block trend entries into nearby S/R without a confirmed break."""
    cfg = cfg or {}
    if not cfg.get("entry_filter_enabled", True):
        return []
    if setup_type in set(cfg.get("entry_filter_exempt_setups") or []):
        return []
    if setup_type in set(cfg.get("counter_trend_setups") or ["peak_reversal_fade", "level_rejection", "mean_reversion", "reversal"]):
        return []

    proximity = float(cfg.get("proximity_points", 20))
    spot_f = _f(spot)
    if spot_f is None:
        return []

    # Session extremes and unlabeled swings sit just under a live trend.
    # Only prior reference levels should veto a continuation.
    major_levels = set(
        cfg.get("major_block_labels")
        or ["pdh", "pdl", "cpr_pivot", "cpr_bc", "cpr_tc", "or_high", "or_low", "vwap"]
    )
    continuation = set(
        cfg.get("continuation_setups")
        or ["vwap_trend", "momentum_continuation", "trend_day", "vwap_pullback", "vwap_reclaim"]
    )

    reasons: list[str] = []
    if side == "CE" and level_map.nearest_resistance is not None:
        dist = level_map.nearest_resistance - spot_f
        if 0 <= dist <= proximity:
            broke = level_map.break_confirmed.get("resistance", False)
            if not broke and m5:
                broke = breakout_confirmed(
                    side="bull",
                    level=level_map.nearest_resistance,
                    m5=m5,
                    cfg=cfg,
                )
            if not broke:
                label = _level_label_near(level_map, level_map.nearest_resistance)
                if setup_type in continuation and label not in major_levels:
                    return reasons
                reasons.append(f"ce_into_resistance@{label}")
    if side == "PE" and level_map.nearest_support is not None:
        dist = spot_f - level_map.nearest_support
        if 0 <= dist <= proximity:
            broke = level_map.break_confirmed.get("support", False)
            if not broke and m5:
                broke = breakout_confirmed(
                    side="bear",
                    level=level_map.nearest_support,
                    m5=m5,
                    cfg=cfg,
                )
            if not broke:
                label = _level_label_near(level_map, level_map.nearest_support)
                if setup_type in continuation and label not in major_levels:
                    return reasons
                reasons.append(f"pe_into_support@{label}")
    return reasons


def level_map_from_dict(data: dict[str, Any]) -> LevelMap:
    return LevelMap(
        supports=list(data.get("supports") or []),
        resistances=list(data.get("resistances") or []),
        nearest_support=data.get("nearest_support"),
        nearest_resistance=data.get("nearest_resistance"),
        distance_to_support_pts=data.get("distance_to_support_pts"),
        distance_to_resistance_pts=data.get("distance_to_resistance_pts"),
        at_support=bool(data.get("at_support")),
        at_resistance=bool(data.get("at_resistance")),
        confluence_support=int(data.get("confluence_support") or 0),
        confluence_resistance=int(data.get("confluence_resistance") or 0),
        level_labels=dict(data.get("level_labels") or {}),
        rejection_setup=data.get("rejection_setup"),
        rejection_level=data.get("rejection_level"),
        rejection_signals=list(data.get("rejection_signals") or []),
        break_confirmed=dict(data.get("break_confirmed") or {}),
    )


def level_trap_reasons(
    signal,
    extra: dict[str, Any],
    *,
    spot,
    levels_cfg: dict[str, Any] | None = None,
) -> list[str]:
    raw = extra.get("levels")
    if not raw or not isinstance(raw, dict):
        return []
    return level_entry_block_reasons(
        side=signal.side,
        spot=spot,
        level_map=level_map_from_dict(raw),
        setup_type=signal.setup_type,
        m5=None,
        cfg=levels_cfg,
    )


def _level_label_near(level_map: LevelMap, price: float) -> str:
    for name, val in level_map.level_labels.items():
        if abs(val - price) <= 1:
            return name
    return f"{price:.0f}"


def breakout_confirmed(
    *,
    side: str,
    level: float,
    m5: list[Candle],
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Phase D — close beyond level (+ optional retest)."""
    cfg = cfg or {}
    if not cfg.get("require_close_beyond_level", True):
        return True
    if len(m5) < 2:
        return False
    last = m5[-1]
    buffer = float(cfg.get("break_buffer_points", 5))
    if side == "bull":
        if float(last.close) <= level + buffer:
            return False
    else:
        if float(last.close) >= level - buffer:
            return False
    min_retest = int(cfg.get("min_retest_bars", 0))
    if min_retest <= 0 or len(m5) < min_retest + 2:
        return True
    # Retest: prior bar broke, current holds beyond level
    prev = m5[-2]
    if side == "bull":
        return float(prev.close) > level and float(last.low) >= level - buffer
    return float(prev.close) < level and float(last.high) <= level + buffer


def detect_level_rejection(
    *,
    spot: Decimal | None,
    vwap: Decimal | None,
    bias: Bias,
    m5: list[Candle],
    m15: list[Candle],
    level_map: LevelMap,
    cfg: dict[str, Any] | None = None,
    is_expiry_day: bool = False,
) -> LevelMap:
    """Phase C — wick through level + close back inside → reversal setup."""
    cfg = cfg or {}
    if not cfg.get("rejection_enabled", True):
        return level_map
    if spot is None or len(m5) < 3:
        return level_map

    proximity = float(cfg.get("rejection_wick_proximity_points", 15))
    min_vwap_ext = Decimal(str(cfg.get("min_vwap_extension_points", 12)))
    spot_f = float(spot)
    last = m5[-1]
    prev = m5[-2]

    if is_expiry_day:
        min_sigs = int(cfg.get("expiry_min_rejection_signals", 2))
    else:
        min_sigs = int(cfg.get("min_rejection_signals", 1))

    # Resistance rejection → PE
    for name, level in level_map.level_labels.items():
        if name in ("or_low", "pdl", "cpr_bc", "session_low", "vwap"):
            continue
        if name not in ("pdh", "cpr_tc", "or_high", "session_high") and "swing" not in name:
            if level < spot_f - 50:
                continue
        lv = float(level)
        if prev.high >= lv - proximity and last.close < lv and last.close < last.open:
            sigs = ["wick_above_level", f"level_{name}", "bearish_close"]
            if vwap and spot > vwap + min_vwap_ext:
                sigs.append("extended_above_vwap")
            if len(m15) >= 2 and m15[-1].close < m15[-2].close:
                sigs.append("m15_roll_down")
            if len(sigs) >= min_sigs + 1:
                level_map.rejection_setup = "bear"
                level_map.rejection_level = name
                level_map.rejection_signals = sigs
                return level_map

    # Support rejection → CE
    for name, level in level_map.level_labels.items():
        if name in ("or_high", "pdh", "cpr_tc", "session_high"):
            continue
        lv = float(level)
        if prev.low <= lv + proximity and last.close > lv and last.close > last.open:
            sigs = ["wick_below_level", f"level_{name}", "bullish_close"]
            if vwap and spot < vwap - min_vwap_ext:
                sigs.append("extended_below_vwap")
            if len(m15) >= 2 and m15[-1].close > m15[-2].close:
                sigs.append("m15_roll_up")
            if len(sigs) >= min_sigs + 1:
                level_map.rejection_setup = "bull"
                level_map.rejection_level = name
                level_map.rejection_signals = sigs
                return level_map

    return level_map


def detect_liquidity_sweep_at_level(
    m5: list[Candle],
    level_map: LevelMap,
    cfg: dict[str, Any] | None = None,
) -> str | None:
    """Sweep above resistance / below support then close back (false breakout)."""
    if len(m5) < 4:
        return None
    cfg = cfg or {}
    prox = float(cfg.get("sweep_proximity_points", 10))
    window = m5[-4:]
    last = window[-1]
    for name in ("pdh", "or_high", "session_high", "cpr_tc"):
        lv = level_map.level_labels.get(name)
        if lv is None:
            continue
        if any(c.high > float(lv) + prox for c in window[:-1]) and float(last.close) < float(lv):
            return "bear"
    for name in ("pdl", "or_low", "session_low", "cpr_bc"):
        lv = level_map.level_labels.get(name)
        if lv is None:
            continue
        if any(c.low < float(lv) - prox for c in window[:-1]) and float(last.close) > float(lv):
            return "bull"
    return None
