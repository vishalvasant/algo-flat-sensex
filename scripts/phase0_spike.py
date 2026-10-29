#!/usr/bin/env python3
"""Phase 0: broker connectivity spike (Upstox by default).

Usage (from repo root):
  cp .env.example .env   # add UPSTOX_API_KEY + UPSTOX_API_SECRET, run upstox_login.py
  cd trading-engine && pip install -e . && python ../scripts/phase0_spike.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading-engine" / "src"))

from algoflat.broker.factory import create_live_broker
from algoflat.config import AppConfig, EnvSettings
from algoflat.models.events import CandleInterval


async def main() -> None:
    os.chdir(ROOT)
    os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

    config = AppConfig()
    provider = str(config.broker.get("provider", "upstox")).lower()
    env = EnvSettings()

    if provider == "upstox":
        if not env.upstox_api_key or not env.upstox_api_secret:
            if not env.upstox_access_token:
                print("ERROR: Set UPSTOX_API_KEY/SECRET or UPSTOX_ACCESS_TOKEN in .env")
                sys.exit(1)
        from algoflat.broker.upstox_auth import ensure_session
        from algoflat.broker.upstox_credentials import load_upstox_config

        print("==> Resolving Upstox session...")
        cfg = await load_upstox_config()
        await ensure_session(cfg)
    else:
        if not env.flattrade_api_key or not env.flattrade_api_secret:
            print("ERROR: Set FLATTRADE_API_KEY and FLATTRADE_API_SECRET in .env")
            sys.exit(1)
        from algoflat.broker.auth import ensure_session
        from algoflat.broker.credentials import load_flattrade_config

        print("==> Resolving Flattrade session...")
        cfg = await load_flattrade_config()
        await ensure_session(cfg)

    broker = create_live_broker(config)

    print(f"==> Connecting ({provider})...")
    await broker.connect()

    spot_ex = config.symbols["index_spot_exchange"] if "index_spot_exchange" in config.symbols else "NSE"
    spot_tok = config.symbols.get("index_spot_token") or config.symbols.get("spot_token") or "26000"
    if not spot_tok:
        under = config.symbols["underlyings"][0]
        spot_ex = under.get("index_spot_exchange", "NSE")
        spot_tok = under.get("index_spot_token", "26000")

    print(f"==> Spot quote {spot_ex}|{spot_tok}")
    q = await broker.get_quotes(spot_ex, str(spot_tok))
    print("  ", q)

    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(hours=2)
    print("==> M1 candles (last 2h)")
    candles = await broker.get_candles(spot_ex, str(spot_tok), CandleInterval.M1, start, end)
    print(f"  bars={len(candles)} last={candles[-1].close if candles else None}")

    print("==> Option chain probe (ATM)")
    chain = await broker.get_option_chain("NFO", "NIFTY", float(config.symbols.get("fallback_spot", 24000)), 5)
    print(f"  contracts={len(chain)}")

    received: list = []

    def on_quote(q):
        received.append(q)

    keys = [f"{spot_ex}|{spot_tok}"]
    if chain:
        row = chain[0]
        keys.append(f"NFO|{row.get('token')}")
    print(f"==> Subscribe {len(keys)} instruments (10s)...")
    await broker.subscribe(keys, on_quote)
    await asyncio.sleep(10)
    await broker.stop_websocket()
    print(f"  ticks={len(received)}")
    await broker.disconnect()
    print("OK")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
