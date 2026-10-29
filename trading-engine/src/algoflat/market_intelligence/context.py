"""Input bundle for market intelligence computation."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from algoflat.models.events import Candle, FeatureSnapshot


@dataclass
class MarketIntelligenceContext:
  features: FeatureSnapshot
  m1: list[Candle] = field(default_factory=list)
  m3: list[Candle] = field(default_factory=list)
  m5: list[Candle] = field(default_factory=list)
  index_ltps: dict[str, Decimal | float] = field(default_factory=dict)
  index_opens: dict[str, Decimal | float] = field(default_factory=dict)
  chain_snapshot: dict[str, Any] = field(default_factory=dict)
  option_states: dict[str, Any] = field(default_factory=dict)
  now: datetime | None = None
  is_expiry_day: bool = False
  days_to_expiry: int | None = None
