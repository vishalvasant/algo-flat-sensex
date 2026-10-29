"""Provider interfaces — Upstox is the first concrete implementation via broker layer."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any


class MarketDataProvider(ABC):
  @abstractmethod
  async def get_index_ltp(self, symbol: str) -> Decimal | None: ...

  @abstractmethod
  async def get_candles(
    self, symbol: str, interval: str, start: datetime, end: datetime
  ) -> list[dict[str, Any]]: ...


class OptionChainProvider(ABC):
  @abstractmethod
  async def get_chain_snapshot(self, underlying: str) -> dict[str, Any]: ...


class GlobalMarketProvider(ABC):
  @abstractmethod
  async def get_global_context(self) -> dict[str, Any]: ...


class SectorDataProvider(ABC):
  @abstractmethod
  async def get_sector_features(self, symbols: list[str]) -> dict[str, Any]: ...


class BreadthProvider(ABC):
  @abstractmethod
  async def get_breadth(self) -> dict[str, Any]: ...


class MLPredictionProvider(ABC):
  @abstractmethod
  def predict(self, features: dict[str, Any]) -> dict[str, Any]: ...
