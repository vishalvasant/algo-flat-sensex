"""Call-writer resistance and put-writer support entry blocks."""
from datetime import datetime, timezone
from decimal import Decimal

from algoflat.models.events import Bias, CandidateSignal, FeatureSnapshot
from algoflat.validator.trap_avoidance import trap_rejection_reasons
from algoflat.validator.writer_walls import writer_wall_rejection_reasons

WALL_CFG = {
    "enabled": True,
    "proximity_points": 60,
    "break_buffer_points": 8,
    "min_oi_ratio": 0.55,
    "min_oi": 500_000,
}

CHAIN = {
    "strikes": [
        {"strike": 22600, "ce_oi": 200_000, "pe_oi": 4_000_000},
        {"strike": 22650, "ce_oi": 800_000, "pe_oi": 1_200_000},
        {"strike": 22700, "ce_oi": 3_500_000, "pe_oi": 900_000},
        {"strike": 22750, "ce_oi": 1_000_000, "pe_oi": 300_000},
    ]
}


def test_blocks_call_under_call_writer_wall() -> None:
    reasons = writer_wall_rejection_reasons(
        side="CE",
        spot=Decimal("22680"),
        chain=CHAIN,
        cfg=WALL_CFG,
    )
    assert reasons == ["ce_into_call_writer@22700"]


def test_allows_call_after_wall_breaks() -> None:
    reasons = writer_wall_rejection_reasons(
        side="CE",
        spot=Decimal("22720"),
        chain=CHAIN,
        cfg=WALL_CFG,
    )
    assert reasons == []


def test_blocks_put_above_put_writer_wall() -> None:
    reasons = writer_wall_rejection_reasons(
        side="PE",
        spot=Decimal("22640"),
        chain=CHAIN,
        cfg=WALL_CFG,
    )
    assert reasons == ["pe_into_put_writer@22600"]


def test_allows_put_after_support_breaks() -> None:
    reasons = writer_wall_rejection_reasons(
        side="PE",
        spot=Decimal("22580"),
        chain=CHAIN,
        cfg=WALL_CFG,
    )
    assert reasons == []


def test_ignores_thin_open_interest() -> None:
    thin = {"strikes": [{"strike": 22700, "ce_oi": 10_000, "pe_oi": 10_000}]}
    reasons = writer_wall_rejection_reasons(
        side="CE",
        spot=Decimal("22680"),
        chain=thin,
        cfg=WALL_CFG,
    )
    assert reasons == []


def test_trap_path_blocks_trend_call() -> None:
    now = datetime.now(tz=timezone.utc)
    signal = CandidateSignal(
        ts=now,
        setup_type="vwap_trend",
        side="CE",
        instrument_token="1",
        tsym="NIFTY06OCT26C22700",
        strategy_version="test",
        confidence=90,
        feature_snapshot=FeatureSnapshot(
            ts=now,
            nifty_spot=Decimal("22680"),
            session_vwap=Decimal("22640"),
            bias_5m=Bias.BULLISH,
            extra={
                "chain": CHAIN,
                "bias_1m": "bullish",
                "structure_5m": "hhhl",
                "bars_with_vwap_3m": 4,
                "bars_against_vwap_3m": 0,
                "mtf_score_ce": 80,
                "trigger_vwap_pullback": "ok",
            },
        ),
    )
    reasons = trap_rejection_reasons(
        signal,
        {
            "enabled": True,
            "require_spot_vwap_alignment": False,
            "require_bias_side_match": False,
            "require_1m_5m_bias_agree": False,
            "require_5m_structure_align": False,
            "require_3m_bars_with_bias": False,
            "require_pullback_trigger": False,
            "require_mtf_alignment": False,
            "writer_walls": WALL_CFG,
        },
    )
    assert any(r.startswith("ce_into_call_writer@") for r in reasons)
