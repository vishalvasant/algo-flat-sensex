"""Broker factory — selects live data/order adapter from config."""
from __future__ import annotations

from algoflat.broker.flattrade import FlattradeAdapter
from algoflat.broker.upstox import UpstoxAdapter
from algoflat.config import AppConfig


def create_live_broker(config: AppConfig):
    provider = str(config.broker.get("provider", "upstox")).lower().strip()
    if provider == "flattrade":
        return FlattradeAdapter(config)
    if provider == "upstox":
        return UpstoxAdapter(config)
    raise ValueError(f"Unknown broker provider: {provider}")
