"""Unit tests for candle refresh / staleness / session start."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from algoflat.market_data.engine import MarketDataEngine, session_start_utc
from algoflat.models.events import Candle, CandleInterval


IST = ZoneInfo("Asia/Kolkata")


def _candle(ts: datetime, close: str = "24000") -> Candle:
  return Candle(
    instrument_token="26000",
    ts=ts,
    open=Decimal(close),
    high=Decimal(close),
    low=Decimal(close),
    close=Decimal(close),
    volume=100,
    interval=CandleInterval.M1,
  )


def test_session_start_is_0915_ist():
  # 2026-07-16 12:00 UTC = 17:30 IST → session start 09:15 IST = 03:45 UTC
  now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
  start = session_start_utc(now)
  assert start == datetime(2026, 7, 16, 3, 45, tzinfo=timezone.utc)
  assert start.astimezone(IST).hour == 9
  assert start.astimezone(IST).minute == 15


@pytest.mark.asyncio
async def test_empty_fetch_keeps_bars_and_allows_retry():
  broker = MagicMock()
  broker.get_candles = AsyncMock(return_value=[])
  bus = MagicMock()
  cfg = MagicMock()
  cfg.symbols = {"spot_token": "26000", "exchange_spot": "NSE"}
  engine = MarketDataEngine(cfg, broker, bus)

  old = datetime.now(tz=timezone.utc) - timedelta(seconds=30)
  engine._candles[CandleInterval.M1] = [_candle(old)]
  engine._last_refresh_ok = True

  ok = await engine.refresh_session_candles(force=True)
  assert ok is False
  assert len(engine.candles(CandleInterval.M1)) == 1
  assert engine._last_candle_refresh_minute is None  # not marked done
  assert engine.last_refresh_ok is False


@pytest.mark.asyncio
async def test_successful_refresh_sorts_and_updates():
  now = datetime.now(tz=timezone.utc)
  newer = now
  older = now - timedelta(minutes=2)

  async def fake_get(_ex, _tok, interval, _start, _end):
    # Deliberately reverse-chronological to mimic Noren quirks
    return [
      _candle(newer, "24100").model_copy(update={"interval": interval}),
      _candle(older, "24050").model_copy(update={"interval": interval}),
    ]

  broker = MagicMock()
  broker.get_candles = AsyncMock(side_effect=fake_get)
  bus = MagicMock()
  cfg = MagicMock()
  cfg.symbols = {"spot_token": "26000", "exchange_spot": "NSE"}
  engine = MarketDataEngine(cfg, broker, bus)

  ok = await engine.refresh_session_candles(force=True)
  assert ok is True
  m1 = engine.candles(CandleInterval.M1)
  assert m1[0].ts < m1[-1].ts
  assert m1[-1].close == Decimal("24100")
  assert engine.last_refresh_ok is True


def test_candles_stale_when_last_bar_old():
  broker = MagicMock()
  bus = MagicMock()
  cfg = MagicMock()
  cfg.symbols = {"spot_token": "26000", "exchange_spot": "NSE"}
  engine = MarketDataEngine(cfg, broker, bus)
  # Bar open 3m40s ago → closes 2m40s ago → still fresh (threshold 120s).
  engine._candles[CandleInterval.M1] = [
    _candle(datetime.now(tz=timezone.utc) - timedelta(seconds=220))
  ]
  assert engine.candles_stale() is True

  engine._candles[CandleInterval.M1] = [
    _candle(datetime.now(tz=timezone.utc) - timedelta(seconds=100))
  ]
  assert engine.candles_stale() is False


def test_candles_stale_ist_bar_open_time():
  """Upstox returns IST bar open timestamps — must not false-stale mid-minute."""
  broker = MagicMock()
  bus = MagicMock()
  cfg = MagicMock()
  cfg.symbols = {"spot_token": "26000", "exchange_spot": "NSE"}
  engine = MarketDataEngine(cfg, broker, bus)
  ist = ZoneInfo("Asia/Kolkata")
  now_ist = datetime.now(ist)
  bar_open = now_ist.replace(second=0, microsecond=0) - timedelta(minutes=1)
  engine._candles[CandleInterval.M1] = [_candle(bar_open.astimezone(timezone.utc))]
  assert engine.candles_stale() is False


def test_spot_context_switch_preserves_candle_cache():
  broker = MagicMock()
  bus = MagicMock()
  cfg = MagicMock()
  cfg.symbols = {"spot_token": "NIFTY_FUT", "exchange_spot": "NFO"}
  engine = MarketDataEngine(cfg, broker, bus)

  nifty_bar = _candle(datetime.now(tz=timezone.utc) - timedelta(seconds=30), "24000")
  engine._candles[CandleInterval.M1] = [nifty_bar]
  engine._persist_active_candles()

  engine.set_spot_context(exchange="NFO", spot_token="OLD_NIFTY_FUT")
  assert engine.candles(CandleInterval.M1) == []

  rolled_bar = _candle(datetime.now(tz=timezone.utc) - timedelta(seconds=20), "24100")
  engine._candles[CandleInterval.M1] = [rolled_bar]
  engine._persist_active_candles()

  engine.set_spot_context(exchange="NFO", spot_token="NIFTY_FUT")
  restored = engine.candles(CandleInterval.M1)
  assert len(restored) == 1
  assert restored[0].close == Decimal("24000")


def test_needs_broker_candle_sync_skips_when_fresh():
  broker = MagicMock()
  bus = MagicMock()
  cfg = MagicMock()
  cfg.symbols = {"spot_token": "26000", "exchange_spot": "NSE"}
  engine = MarketDataEngine(cfg, broker, bus)
  recent = datetime.now(tz=timezone.utc) - timedelta(seconds=30)
  engine._candles[CandleInterval.M1] = [_candle(recent)]
  db_stats = {"1m": {"count": 100, "latest": recent}}
  need, reason = engine.needs_broker_candle_sync(db_stats, market_open=True)
  assert need is False
  assert reason == "ok"


def test_needs_broker_candle_sync_when_memory_empty():
  broker = MagicMock()
  bus = MagicMock()
  cfg = MagicMock()
  cfg.symbols = {"spot_token": "26000", "exchange_spot": "NSE"}
  engine = MarketDataEngine(cfg, broker, bus)
  db_stats = {"1m": {"count": 0, "latest": None}}
  need, reason = engine.needs_broker_candle_sync(db_stats, market_open=True)
  assert need is True
  assert reason == "empty"
