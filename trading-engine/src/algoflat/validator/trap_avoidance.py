"""Config-driven entry traps filter — blocks setups that lead to adverse_momentum."""
from __future__ import annotations

from decimal import Decimal

from algoflat.models.events import Bias, CandidateSignal
from algoflat.features.levels import level_trap_reasons
from algoflat.validator.writer_walls import writer_wall_rejection_reasons
from algoflat.validator.counter_bias_greeks import (
    COUNTER_BIAS_SETUPS,
    counter_bias_greek_reasons,
)
from algoflat.validator.gap_open_traps import gap_open_trap_reasons

LEVEL_REJECTION_SETUPS = frozenset({"level_rejection"})


def _counter_bias_trap_reasons(
    signal: CandidateSignal,
    trap_cfg: dict,
    feat,
    extra: dict,
    spot,
    vwap,
) -> list[str]:
    reasons: list[str] = []
    cb = trap_cfg.get("counter_bias") or {}

    if signal.setup_type == "gap_fade":
        gap_cls = str(extra.get("gap_class") or "")
        if gap_cls == "gap_up" and signal.side != "PE":
            reasons.append("gap_fade_side_mismatch")
        if gap_cls == "gap_down" and signal.side != "CE":
            reasons.append("gap_fade_side_mismatch")
        reasons.extend(counter_bias_greek_reasons(signal, trap_cfg))
        return reasons

    if signal.side == "CE" and feat.bias_5m != Bias.BEARISH:
        reasons.append("counter_ce_needs_bearish_trend")
    if signal.side == "PE" and feat.bias_5m != Bias.BULLISH:
        reasons.append("counter_pe_needs_bullish_trend")

    min_ext = Decimal(str(cb.get("min_extension_points", 18)))
    if extra.get("bias_confidence_mismatch"):
        min_ext = Decimal(str(cb.get("mismatch_min_extension_points", 12)))

    if spot is not None and vwap is not None:
        if signal.side == "PE" and spot <= vwap + min_ext:
            reasons.append("counter_pe_not_extended")
        if signal.side == "CE" and spot >= vwap - min_ext:
            reasons.append("counter_ce_not_extended")

    min_cb_mtf = int(cb.get("min_counter_mtf_score", 48))
    if extra.get("bias_confidence_mismatch"):
        min_cb_mtf = int(cb.get("mismatch_min_counter_mtf_score", min_cb_mtf - 5))
    key = "counter_mtf_score_pe" if signal.side == "PE" else "counter_mtf_score_ce"
    raw = extra.get(key)
    if raw is None or int(raw) < min_cb_mtf:
        reasons.append("counter_mtf_too_low")

    sigs = extra.get("counter_bias_signals") or []
    min_sigs = int(cb.get("min_reversal_signals", 2))
    if extra.get("bias_confidence_mismatch"):
        min_sigs = int(cb.get("mismatch_min_reversal_signals", 1))
    if signal.setup_type == "level_rejection":
        levels = extra.get("levels") or {}
        sigs = levels.get("rejection_signals") or []
        levels_cfg = trap_cfg.get("levels") or {}
        min_sigs = int(levels_cfg.get("min_rejection_signals", 1)) + 1
    if len(sigs) < min_sigs:
        reasons.append("counter_reversal_signals_weak")

    if cb.get("require_1m_turn", True):
        bias_1m = str(extra.get("bias_1m") or "")
        if signal.side == "PE" and bias_1m == "bullish":
            reasons.append("counter_1m_not_turning")
        if signal.side == "CE" and bias_1m == "bearish":
            reasons.append("counter_1m_not_turning")

    reasons.extend(counter_bias_greek_reasons(signal, trap_cfg))
    return reasons


def _writer_wall_reasons(signal: CandidateSignal, trap_cfg: dict) -> list[str]:
    feat = signal.feature_snapshot
    extra = feat.extra or {}
    return writer_wall_rejection_reasons(
        side=signal.side,
        spot=feat.nifty_spot,
        chain=extra.get("chain") or {},
        setup_type=signal.setup_type,
        cfg=trap_cfg.get("writer_walls") or {},
    )


def trap_rejection_reasons(
    signal: CandidateSignal,
    trap_cfg: dict,
) -> list[str]:
    if not trap_cfg.get("enabled", False):
        return []

    setup = signal.setup_type
    if setup == "option_lift" or setup in set(trap_cfg.get("exempt_setups") or []):
        return []
    if setup in COUNTER_BIAS_SETUPS or setup in LEVEL_REJECTION_SETUPS:
        feat = signal.feature_snapshot
        extra = feat.extra or {}
        reasons = _counter_bias_trap_reasons(
            signal,
            trap_cfg,
            feat,
            extra,
            feat.nifty_spot,
            feat.session_vwap,
        )
        reasons.extend(_writer_wall_reasons(signal, trap_cfg))
        return reasons

    reasons: list[str] = []

    blocked = set(trap_cfg.get("blocked_setups") or [])
    if setup in blocked:
        reasons.append("setup_blocked")

    if trap_cfg.get("block_reversal_flips", False) and setup == "trend_reversal_flip":
        reasons.append("reversal_flip_blocked")

    feat = signal.feature_snapshot
    spot = feat.nifty_spot
    vwap = feat.session_vwap
    extra = feat.extra or {}

    if trap_cfg.get("require_bias_side_match", True):
        if signal.side == "CE" and feat.bias_5m == Bias.BEARISH:
            reasons.append("ce_against_bearish_bias")
        if signal.side == "PE" and feat.bias_5m == Bias.BULLISH:
            reasons.append("pe_against_bullish_bias")

    # Block when 1m bias fights 5m bias (chop / fake reclaim).
    if trap_cfg.get("require_1m_5m_bias_agree", True):
        bias_1m = str(extra.get("bias_1m") or "")
        if signal.side == "CE" and bias_1m == "bearish":
            reasons.append("ce_1m_5m_bias_conflict")
        if signal.side == "PE" and bias_1m == "bullish":
            reasons.append("pe_1m_5m_bias_conflict")

    # Level breaks are valid on either side of VWAP. Pullbacks are valid as soon
    # as price is on the correct side — a 10pt cushion deletes them next to VWAP.
    vwap_exempt = set(
        trap_cfg.get("vwap_alignment_exempt_setups")
        or ["pdh_pdl_break", "opening_range_breakout", "gap_and_go", "oi_breakout"]
    )
    near_vwap = set(
        trap_cfg.get("vwap_alignment_zero_buffer_setups")
        or ["vwap_pullback", "vwap_reclaim", "vwap_trend", "momentum_continuation", "trend_day"]
    )
    buffer = Decimal("0") if setup in near_vwap else Decimal(str(trap_cfg.get("spot_vwap_buffer_points", 8)))
    if (
        trap_cfg.get("require_spot_vwap_alignment", True)
        and setup not in vwap_exempt
        and spot is not None
        and vwap is not None
    ):
        if signal.side == "CE" and spot < vwap + buffer:
            reasons.append("ce_below_vwap_trap")
        if signal.side == "PE" and spot > vwap - buffer:
            reasons.append("pe_above_vwap_trap")

    # 5m structure lags a bias flip. Once bias already agrees with the side, keep the setup.
    if trap_cfg.get("require_5m_structure_align", True):
        structure = str(extra.get("structure_5m") or "")
        if signal.side == "CE" and structure == "lllh" and feat.bias_5m != Bias.BULLISH:
            reasons.append("ce_against_5m_structure")
        if signal.side == "PE" and structure == "hhhl" and feat.bias_5m != Bias.BEARISH:
            reasons.append("pe_against_5m_structure")

    # Prefer trend continuation only when 3m bars are with VWAP side.
    if trap_cfg.get("require_3m_bars_with_bias", True):
        bars_with = int(extra.get("bars_with_vwap_3m") or 0)
        bars_against = int(extra.get("bars_against_vwap_3m") or 0)
        min_with = int(trap_cfg.get("min_3m_bars_with_bias", 2))
        if setup in ("trend_continuation", "vwap_trend", "momentum_continuation", "ema_pullback"):
            if bars_with < min_with or bars_against > bars_with:
                reasons.append("3m_structure_weak")

    # Pullback-family must have 1m bounce trigger (FeatureSetupScanner used to skip this).
    if trap_cfg.get("require_pullback_trigger", True):
        if setup in ("vwap_pullback", "vwap_bounce"):
            trigger = extra.get("trigger_vwap_pullback")
            if not trigger:
                reasons.append("pullback_trigger_missing")

    # Multi-TF candle alignment (Phase B/C) — block weak structure entries.
    if trap_cfg.get("require_mtf_alignment", True):
        min_mtf = int(trap_cfg.get("min_mtf_score", 55))
        if signal.side == "CE":
            mtf_raw = extra.get("mtf_score_ce")
        else:
            mtf_raw = extra.get("mtf_score_pe")
        if mtf_raw is not None and int(mtf_raw) < min_mtf:
            reasons.append("mtf_score_too_low")

    ema_setups = set(trap_cfg.get("require_ema_alignment_for") or [])
    if setup in ema_setups and spot is not None:
        e9 = extra.get("ema9")
        e21 = extra.get("ema21")
        tol = Decimal(str(trap_cfg.get("ema_alignment_tolerance_points", 0)))
        if e9 is not None and e21 is not None:
            e9d, e21d = Decimal(str(e9)), Decimal(str(e21))
            # Soft stack: allow near-flat EMA crossover within tolerance.
            if signal.side == "CE" and not (e9d + tol >= e21d and spot + tol >= e21d):
                reasons.append("ema_structure_not_bull")
            if signal.side == "PE" and not (e9d - tol <= e21d and spot - tol <= e21d):
                reasons.append("ema_structure_not_bear")

    # MTF floor for breakout-family setups. The opening-range high/low check
    # applies only to the opening-range breakout itself.
    strict_breakouts = set(trap_cfg.get("strict_breakout_setups") or [])
    if setup in strict_breakouts:
        min_brk_mtf = int(trap_cfg.get("min_mtf_score_breakout", 62))
        if signal.side == "CE":
            mtf_raw = extra.get("mtf_score_ce")
        else:
            mtf_raw = extra.get("mtf_score_pe")
        if mtf_raw is not None and int(mtf_raw) < min_brk_mtf:
            reasons.append("breakout_mtf_too_low")
    if setup == "opening_range_breakout":
        or_h, or_l = extra.get("or_high"), extra.get("or_low")
        if spot is not None and signal.side == "CE" and or_h is not None:
            if spot <= Decimal(str(or_h)):
                reasons.append("orb_break_too_weak")
        if spot is not None and signal.side == "PE" and or_l is not None:
            if spot >= Decimal(str(or_l)):
                reasons.append("orb_break_too_weak")

    levels_cfg = trap_cfg.get("levels") or {}
    reasons.extend(
        level_trap_reasons(
            signal,
            extra,
            spot=spot,
            levels_cfg=levels_cfg,
        )
    )
    reasons.extend(gap_open_trap_reasons(signal, trap_cfg))
    reasons.extend(_writer_wall_reasons(signal, trap_cfg))

    return reasons
