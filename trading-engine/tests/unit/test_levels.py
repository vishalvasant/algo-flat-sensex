"""Tests for support/resistance level map and entry filters."""
from datetime import datetime, timezone
from decimal import Decimal

from algoflat.features.levels import (
    LevelMap,
    breakout_confirmed,
    build_level_map,
    detect_level_rejection,
    detect_liquidity_sweep_at_level,
    level_entry_block_reasons,
    level_trap_reasons,
)
from algoflat.models.events import Bias, Candle, CandleInterval, CandidateSignal, FeatureSnapshot


def _candle(o: str, h: str, l: str, c: str, i: int = 0, interval: CandleInterval = CandleInterval.M5) -> Candle:
    return Candle(
        instrument_token="26000",
        ts=datetime(2026, 7, 20, 5, i, tzinfo=timezone.utc),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(l),
        close=Decimal(c),
        interval=interval,
    )


def test_build_level_map_nearest_levels() -> None:
    ind = {
        "pdh": Decimal("25100"),
        "pdl": Decimal("24900"),
        "or_high": Decimal("25050"),
        "or_low": Decimal("24950"),
        "cpr": {"pivot": 25000.0, "tc": 25020.0, "bc": 24980.0},
    }
    m5 = [_candle("24980", "25010", "24970", "25000", i) for i in range(8)]
    m15 = m5[:4]
    spot = Decimal("25040")
    vwap = Decimal("25000")

    level_map = build_level_map(
        spot=spot,
        vwap=vwap,
        m5=m5,
        m15=m15,
        ind=ind,
        cfg={"proximity_points": 25},
    )

    assert level_map.nearest_support is not None
    assert level_map.nearest_resistance is not None
    assert "pdh" in level_map.level_labels
    assert level_map.to_dict()["nearest_resistance"] == level_map.nearest_resistance


def test_level_entry_blocks_ce_into_resistance() -> None:
    level_map = LevelMap(
        nearest_resistance=25050.0,
        break_confirmed={"resistance": False},
        level_labels={"or_high": 25050.0},
    )
    reasons = level_entry_block_reasons(
        side="CE",
        spot=Decimal("25040"),
        level_map=level_map,
        setup_type="vwap_trend",
        cfg={"proximity_points": 20},
    )
    assert any("ce_into_resistance" in r for r in reasons)


def test_trend_ignores_unlabeled_swing_under_price() -> None:
    level_map = LevelMap(
        nearest_support=22444.0,
        break_confirmed={"support": False},
        level_labels={"session_low": 22390.0},
    )
    reasons = level_entry_block_reasons(
        side="PE",
        spot=Decimal("22457"),
        level_map=level_map,
        setup_type="vwap_trend",
        cfg={"proximity_points": 20},
    )
    assert reasons == []


def test_level_entry_exempts_counter_trend() -> None:
    level_map = LevelMap(
        nearest_resistance=25050.0,
        break_confirmed={"resistance": False},
    )
    reasons = level_entry_block_reasons(
        side="CE",
        spot=Decimal("25040"),
        level_map=level_map,
        setup_type="level_rejection",
        cfg={"proximity_points": 20},
    )
    assert reasons == []


def test_breakout_confirmed_requires_close_beyond() -> None:
    level = 25000.0
    weak = [
        _candle("24990", "25005", "24980", "24995"),
        _candle("24995", "25010", "24990", "25002"),
    ]
    strong = [
        _candle("24990", "25005", "24980", "24995"),
        _candle("24995", "25020", "24990", "25010"),
    ]
    cfg = {"require_close_beyond_level": True, "break_buffer_points": 5}
    assert breakout_confirmed(side="bull", level=level, m5=weak, cfg=cfg) is False
    assert breakout_confirmed(side="bull", level=level, m5=strong, cfg=cfg) is True


def test_detect_level_rejection_at_pdh() -> None:
    pdh = 25100.0
    ind = {"pdh": Decimal(str(pdh)), "pdl": Decimal("24900")}
    m5 = [
        _candle("25050", "25080", "25040", "25070"),
        _candle("25070", "25090", "25060", "25085"),
        _candle("25085", "25110", "25080", "25095"),  # wick above PDH
        _candle("25095", "25105", "25070", "25075"),  # bearish close back
    ]
    m15 = m5
    level_map = build_level_map(
        spot=Decimal("25075"),
        vwap=Decimal("25020"),
        m5=m5,
        m15=m15,
        ind=ind,
        cfg={"rejection_wick_proximity_points": 15, "min_rejection_signals": 1},
    )
    level_map = detect_level_rejection(
        spot=Decimal("25075"),
        vwap=Decimal("25020"),
        bias=Bias.BULLISH,
        m5=m5,
        m15=m15,
        level_map=level_map,
        cfg={"rejection_wick_proximity_points": 15, "min_rejection_signals": 1},
    )
    assert level_map.rejection_setup == "bear"
    assert level_map.rejection_level == "pdh"
    assert len(level_map.rejection_signals) >= 2


def test_liquidity_sweep_bear_at_pdh() -> None:
    pdh = 25100.0
    ind = {"pdh": Decimal(str(pdh)), "pdl": Decimal("24900")}
    m5 = [
        _candle("25080", "25105", "25070", "25095"),
        _candle("25095", "25115", "25090", "25100"),
        _candle("25100", "25120", "25095", "25098"),
        _candle("25098", "25102", "25070", "25085"),
    ]
    level_map = build_level_map(
        spot=Decimal("25085"),
        vwap=Decimal("25050"),
        m5=m5,
        m15=m5,
        ind=ind,
        cfg={},
    )
    sweep = detect_liquidity_sweep_at_level(m5, level_map, {"sweep_proximity_points": 10})
    assert sweep == "bear"


def test_level_trap_reasons_integration() -> None:
    signal = CandidateSignal(
        ts=datetime.now(tz=timezone.utc),
        setup_type="vwap_trend",
        side="CE",
        instrument_token="1",
        tsym="NIFTY21JUL26C25000",
        strategy_version="test",
        feature_snapshot=FeatureSnapshot(
            ts=datetime.now(tz=timezone.utc),
            nifty_spot=Decimal("25040"),
            session_vwap=Decimal("25000"),
            bias_5m=Bias.BULLISH,
            extra={
                "levels": {
                    "nearest_resistance": 25050.0,
                    "nearest_support": 24980.0,
                    "break_confirmed": {"resistance": False, "support": False},
                    "level_labels": {"or_high": 25050.0},
                }
            },
        ),
    )
    reasons = level_trap_reasons(
        signal,
        signal.feature_snapshot.extra or {},
        spot=Decimal("25040"),
        levels_cfg={"proximity_points": 20, "entry_filter_enabled": True},
    )
    assert any("ce_into_resistance" in r for r in reasons)
