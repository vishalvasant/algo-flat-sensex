"""Scan-parity helpers: forming M1, candidate option M1 fetch, runtime gates."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from algoflat.config import get_config
from algoflat.market_data.engine import MarketDataEngine
from algoflat.models.events import Candle, CandleInterval, QuoteUpdate


class _BrokerStub:
    def __init__(self, rows: list[Candle] | None = None) -> None:
        self.rows = rows or []
        self.calls = 0

    async def get_candles(self, exchange, token, interval, start, end):
        self.calls += 1
        return list(self.rows)


@pytest.fixture
def md_engine():
    cfg = get_config()
    bus = SimpleNamespace()

    async def _publish(*_a, **_k):
        return None

    bus.publish = _publish
    eng = MarketDataEngine(cfg, _BrokerStub(), bus)  # type: ignore[arg-type]
    eng._spot_token = "SPOT1"
    eng._forming_m1_enabled = True
    return eng


def test_forming_m1_updates_current_minute(md_engine: MarketDataEngine):
    base = datetime(2026, 9, 11, 5, 0, tzinfo=timezone.utc)
    md_engine._candles[CandleInterval.M1] = [
        Candle(
            instrument_token="SPOT1",
            ts=base,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=10,
            interval=CandleInterval.M1,
        )
    ]
    md_engine.apply_spot_ltp_to_forming_m1(Decimal("102.5"), base.replace(second=45))
    last = md_engine.candles(CandleInterval.M1)[-1]
    assert last.close == Decimal("102.5")
    assert last.high == Decimal("102.5")
    assert len(md_engine.candles(CandleInterval.M1)) == 1


def test_forming_m1_starts_new_minute(md_engine: MarketDataEngine):
    base = datetime(2026, 9, 11, 5, 0, tzinfo=timezone.utc)
    md_engine._candles[CandleInterval.M1] = [
        Candle(
            instrument_token="SPOT1",
            ts=base,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=10,
            interval=CandleInterval.M1,
        )
    ]
    md_engine.apply_spot_ltp_to_forming_m1(
        Decimal("103"), base.replace(minute=1, second=5)
    )
    rows = md_engine.candles(CandleInterval.M1)
    assert len(rows) == 2
    assert rows[-1].close == Decimal("103")
    assert rows[-1].open == Decimal("100.5")


@pytest.mark.asyncio
async def test_on_quote_updates_forming_via_set_spot_ltp(md_engine: MarketDataEngine):
    base = datetime(2026, 9, 11, 5, 0, tzinfo=timezone.utc)
    md_engine._candles[CandleInterval.M1] = [
        Candle(
            instrument_token="SPOT1",
            ts=base,
            open=Decimal("23400"),
            high=Decimal("23410"),
            low=Decimal("23390"),
            close=Decimal("23405"),
            volume=1,
            interval=CandleInterval.M1,
        )
    ]
    await md_engine.on_quote(
        QuoteUpdate(
            instrument_token="SPOT1",
            exchange="NFO",
            ts=base.replace(second=20),
            ltp=Decimal("23420"),
        )
    )
    assert md_engine.spot_ltp == Decimal("23420")
    assert md_engine.candles(CandleInterval.M1)[-1].close == Decimal("23420")


@pytest.mark.asyncio
async def test_ensure_option_token_m1_fetches_when_short(md_engine: MarketDataEngine):
    rows = [
        Candle(
            instrument_token="OPT1",
            ts=datetime(2026, 9, 11, 4, i, tzinfo=timezone.utc),
            open=Decimal("40"),
            high=Decimal("41"),
            low=Decimal("39"),
            close=Decimal("40.5"),
            volume=100,
            interval=CandleInterval.M1,
        )
        for i in range(8)
    ]
    md_engine._broker = _BrokerStub(rows)  # type: ignore[assignment]
    out = await md_engine.ensure_option_token_m1("OPT1", exchange="NFO", min_bars=5)
    assert len(out) >= 5
    assert md_engine._broker.calls == 1  # type: ignore[attr-defined]
    # Second call should hit cache (no re-fetch)
    out2 = await md_engine.ensure_option_token_m1("OPT1", exchange="NFO", min_bars=5)
    assert len(out2) >= 5
    assert md_engine._broker.calls == 1  # type: ignore[attr-defined]


def test_runtime_scan_parity_keys_present():
    cfg = get_config()
    assert cfg.runtime.get("forming_m1_from_quotes") is True
    assert int(cfg.runtime.get("min_m1_bars_for_entry", 0)) == 20
    assert int(cfg.runtime.get("min_m3_bars_for_entry", 0)) == 5
    assert cfg.runtime.get("force_fetch_candidate_option_m1") is True
    assert int(cfg.runtime.get("min_option_m1_bars_for_chart", 0)) == 5
    assert int(cfg.runtime.get("scan_interval_seconds", 0)) == 5
    assert float(cfg.runtime.get("forming_m1_min_interval_seconds", 0)) == 5
    assert float(cfg.runtime.get("exit_eval_interval_seconds", 0)) == 1
    mark = cfg.runtime.get("option_mark") or {}
    assert mark.get("enabled") is True
    assert mark.get("prefer_mid") is True
    ec = (cfg.strategy.get("entry_confirmation") or {})
    assert ec.get("fast_confirm_on_quote") is True
    assert int(ec.get("required_scans", 0)) == 1
    assert int(ec.get("high_confidence_bypass", 0)) == 88
    assert int((ec.get("setup_overrides") or {}).get("vwap_reclaim", {}).get("required_scans", 0)) == 2
    assert int(cfg.position_exit.get("exit_confirm_ticks", 0)) == 2


def test_option_mark_prefers_mid():
    from algoflat.option_data.layer import OptionDataLayer
    from algoflat.models.events import QuoteUpdate

    cfg = get_config()
    layer = OptionDataLayer(cfg)
    ts = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)
    layer.update_from_quote(
        QuoteUpdate(
            instrument_token="OPT1",
            exchange="NFO",
            ts=ts,
            ltp=Decimal("50"),
            bid=Decimal("49.5"),
            ask=Decimal("50.5"),
        )
    )
    mark = layer.mark_ltp("OPT1")
    assert mark == Decimal("50.00")
    st = layer.state_with_mark("OPT1")
    assert st is not None and st.ltp == Decimal("50.00")
