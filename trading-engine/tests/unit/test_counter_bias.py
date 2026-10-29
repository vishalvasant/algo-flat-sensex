"""Tests for counter-bias peak reversal fade."""
from datetime import datetime, timezone
from decimal import Decimal

from algoflat.features.counter_bias import (
    detect_peak_reversal_fade,
    evaluate_bias_confidence_mismatch,
)
from algoflat.models.events import Bias, Candle, CandleInterval, CandidateSignal, FeatureSnapshot
from algoflat.validator.trap_avoidance import trap_rejection_reasons


def _candle(o: str, h: str, l: str, c: str, i: int = 0) -> Candle:
    return Candle(
        instrument_token="26000",
        ts=datetime(2026, 7, 20, 5, i, tzinfo=timezone.utc),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(l),
        close=Decimal(c),
        interval=CandleInterval.M5,
    )


def _bullish_extension_m5() -> list[Candle]:
    """Extended rally then bearish engulf on last bar."""
    return [
        _candle("100", "101", "99", "100.5"),
        _candle("100.5", "102", "100", "101.5"),
        _candle("101.5", "103", "101", "102.5"),
        _candle("102.5", "104", "102", "103.5"),
        _candle("103.5", "104.5", "101", "101.5"),  # bearish engulf body
    ]


def _bearish_extension_m15() -> list[Candle]:
    return [
        _candle("98", "99", "97", "98.5"),
        _candle("98.5", "100", "98", "99.5"),
        _candle("99.5", "101", "99", "100"),
        _candle("100", "102", "99.5", "101.5"),
        _candle("101.5", "103", "100", "101"),  # roll down
    ]


def test_detect_pe_fade_in_bullish_trend() -> None:
    m5 = _bullish_extension_m5()
    m15 = _bearish_extension_m15()
    spot = Decimal("120")
    vwap = Decimal("100")
    ind = {"structure_5m": "hhhl", "bias_1m": "bearish"}
    cfg = {"min_extension_points": 15, "min_reversal_signals": 2, "min_counter_mtf_score": 0}

    result = detect_peak_reversal_fade(
        bias=Bias.BULLISH,
        spot=spot,
        vwap=vwap,
        m5=m5,
        m15=m15,
        ind=ind,
        cfg=cfg,
    )

    assert result.setup == "bear"
    assert len(result.signals) >= 2


def test_bias_confidence_mismatch_detected() -> None:
    cfg = {"min_bias_confidence_gap": 15, "max_bias_side_mtf_score": 52}
    out = evaluate_bias_confidence_mismatch(Bias.BULLISH, mtf_score_ce=40, mtf_score_pe=60, cfg=cfg)
    assert out["mismatch"] is True
    assert out["fade_setup"] == "bear"
    assert out["gap"] == 20


def test_pe_fade_on_bias_confidence_mismatch() -> None:
    m5 = _bullish_extension_m5()
    m15 = _bearish_extension_m15()
    cfg = {
        "min_extension_points": 30,
        "min_reversal_signals": 3,
        "min_counter_mtf_score": 48,
        "min_bias_confidence_gap": 15,
        "max_bias_side_mtf_score": 52,
        "mismatch_min_extension_points": 10,
        "mismatch_min_reversal_signals": 1,
        "mismatch_min_counter_mtf_score": 0,
    }
    result = detect_peak_reversal_fade(
        bias=Bias.BULLISH,
        spot=Decimal("112"),
        vwap=Decimal("100"),
        m5=m5,
        m15=m15,
        ind={"structure_5m": "hhhl", "bias_1m": "bearish"},
        mtf_score_ce=45,
        mtf_score_pe=65,
        cfg=cfg,
    )
    assert result.bias_confidence_mismatch is True
    assert result.setup == "bear"
    assert result.trigger_reason == "bias_confidence_mismatch"
    assert "bias_side_confidence_mismatch" in result.signals


def test_trap_allows_counter_pe_with_greeks() -> None:
    sig = CandidateSignal(
        ts=datetime.now(tz=timezone.utc),
        setup_type="peak_reversal_fade",
        side="PE",
        instrument_token="1",
        tsym="NIFTY21JUL26P24300",
        strategy_version="test",
        feature_snapshot=FeatureSnapshot(
            ts=datetime.now(tz=timezone.utc),
            nifty_spot=Decimal("24320"),
            session_vwap=Decimal("24280"),
            bias_5m=Bias.BULLISH,
            extra={
                "bias_1m": "bearish",
                "counter_mtf_score_pe": 55,
                "counter_bias_signals": ["m5_bearish_engulf", "m5_lower_high", "1m_turning_bearish"],
            },
        ),
        scanner_metadata={
            "strike_pick": {"delta": -0.42, "gamma": 0.0008, "iv": 0.18},
        },
    )
    cfg = {
        "enabled": True,
        "counter_bias": {
            "min_extension_points": 15,
            "min_reversal_signals": 2,
            "min_counter_mtf_score": 48,
            "require_1m_turn": True,
            "require_greeks": True,
            "min_abs_delta": 0.30,
            "max_abs_delta": 0.55,
            "min_gamma": 0.0004,
            "max_iv": 0.85,
            "require_oi_confirm": False,
        },
    }
    reasons = trap_rejection_reasons(sig, cfg)
    assert reasons == []


def test_trap_blocks_counter_pe_weak_greeks() -> None:
    sig = CandidateSignal(
        ts=datetime.now(tz=timezone.utc),
        setup_type="peak_reversal_fade",
        side="PE",
        instrument_token="1",
        tsym="NIFTY21JUL26P24300",
        strategy_version="test",
        feature_snapshot=FeatureSnapshot(
            ts=datetime.now(tz=timezone.utc),
            nifty_spot=Decimal("24320"),
            session_vwap=Decimal("24280"),
            bias_5m=Bias.BULLISH,
            extra={
                "bias_1m": "bearish",
                "counter_mtf_score_pe": 55,
                "counter_bias_signals": ["m5_bearish_engulf", "m5_lower_high"],
            },
        ),
        scanner_metadata={
            "strike_pick": {"delta": -0.12, "gamma": 0.0001, "iv": 0.95},
        },
    )
    cfg = {
        "enabled": True,
        "counter_bias": {
            "min_extension_points": 15,
            "min_reversal_signals": 2,
            "min_counter_mtf_score": 48,
            "require_greeks": True,
            "min_abs_delta": 0.30,
            "max_abs_delta": 0.55,
            "min_gamma": 0.0004,
            "max_iv": 0.85,
            "require_oi_confirm": False,
        },
    }
    reasons = trap_rejection_reasons(sig, cfg)
    assert "counter_pe_delta_weak" in reasons or "counter_greek_delta_out_of_band" in reasons
