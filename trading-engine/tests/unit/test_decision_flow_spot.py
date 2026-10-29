from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from algoflat.config import AppConfig
from algoflat.features.engine import FeatureEngine
from algoflat.market_data.engine import MarketDataEngine
from algoflat.models.events import Candle, CandleInterval


def _candle(close: str, i: int) -> Candle:
  return Candle(
    instrument_token="fut",
    ts=datetime(2026, 8, 28, 4, i, tzinfo=timezone.utc),
    open=Decimal(close),
    high=Decimal(close),
    low=Decimal(close),
    close=Decimal(close),
    interval=CandleInterval.M1,
  )


def test_levels_use_cash_index_when_dual_spot() -> None:
  config = AppConfig()
  config.symbols.update(
    {
      "price_source": "futures",
      "index_spot_exchange": "NSE",
      "index_spot_token": "26000",
    }
  )
  md = MagicMock(spec=MarketDataEngine)
  md.candles.side_effect = lambda interval: [_candle("25010", i) for i in range(20)]
  md.spot_ltp = Decimal("25010")
  md.cash_index_ltp = Decimal("24980")

  engine = FeatureEngine(config, md)
  engine.prior_high = Decimal("25100")
  engine.prior_low = Decimal("24900")
  engine.prior_close = Decimal("25000")
  snap = engine.compute()

  extra = snap.extra or {}
  assert extra["cash_index_spot"] == 24980.0
  assert extra["fut_spot"] == 25010.0
  assert extra["decision_flow"]["levels_anchor"] == "cash_index"
  levels = extra.get("levels") or {}
  assert levels.get("nearest_resistance") is not None
