#!/usr/bin/env python3
"""Quick Upstox stack verification (docker compose on :8001 / :8080)."""

from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx

ENGINE = "http://127.0.0.1:8001"
WEB = "http://127.0.0.1:8080"


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"OK:   {msg}")


async def wait_health(url: str, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    last = ""
    async with httpx.AsyncClient(timeout=15.0) as client:
        while time.time() < deadline:
            try:
                r = await client.get(f"{url}/health")
                if r.status_code == 200:
                    data = r.json()
                    if data.get("status") == "running" or url == WEB:
                        return data
            except Exception as exc:
                last = str(exc)
            await asyncio.sleep(3)
    fail(f"Health timeout at {url} ({last})")
    return {}


async def main() -> None:
    print("\n==> Engine health")
    health = await wait_health(ENGINE, timeout=240.0)
    print(json.dumps(health, indent=2))
    if not health.get("db_ok"):
        fail("engine db_ok=false")
    creds = health.get("broker_credentials") or health.get("flattrade_credentials") or {}
    provider = creds.get("provider", "upstox")
    if provider != "upstox":
        fail(f"expected provider upstox, got {provider}")
    if not health.get("broker_connected"):
        fail("broker_connected=false — check Upstox token in .env")
    ok(f"engine running, broker={provider}, connected=True")

    async with httpx.AsyncClient(timeout=120.0) as client:
        print("\n==> Sync missing (7-day backfill)")
        sync = await client.post(f"{ENGINE}/control/sync-missing")
        if sync.status_code == 429 or (
            sync.status_code >= 400 and "429" in sync.text
        ):
            print("WARN: sync-missing rate-limited (429) — continuing with existing data")
        elif sync.status_code >= 400:
            fail(f"sync-missing {sync.status_code}: {sync.text[:300]}")
        else:
            sync_body = sync.json()
            print(json.dumps(sync_body, indent=2)[:2000])
            if not sync_body.get("ok", True):
                fail(sync_body.get("message") or "sync-missing failed")
            ok("sync-missing completed")

        print("\n==> Watchlist")
        for attempt in range(3):
            try:
                wl = (await client.get(f"{ENGINE}/watchlist", timeout=90.0)).json()
                break
            except Exception as exc:
                if attempt >= 2:
                    fail(f"watchlist failed: {exc}")
                await asyncio.sleep(5)
        commodities = wl.get("commodities") or []
        nifty = next((c for c in commodities if c.get("underlying") == "NIFTY"), None)
        if not nifty:
            fail("NIFTY missing from watchlist")
        inst = nifty.get("instrument_count") or len(nifty.get("items") or [])
        if inst < 10:
            fail(f"expected ~22 option contracts, got {inst}")
        ok(f"NIFTY expiry={nifty.get('expiry_symbol')} contracts={inst}")

        print("\n==> Market summary")
        summary = (await client.get(f"{ENGINE}/market-summary")).json()
        m1 = summary.get("candle_counts", {}).get("m1") if isinstance(summary.get("candle_counts"), dict) else None
        cap = summary.get("available_capital")
        ok(f"regime={summary.get('regime')} spot={summary.get('spot_ltp')} available={cap}")

        print("\n==> Market intelligence (New-Engine)")
        mi = summary.get("market_intelligence") or {}
        flow = summary.get("decision_flow") or {}
        stages = flow.get("stages") or []
        if mi.get("enabled") is False:
            fail("market_intelligence.enabled=false")
        if len(stages) < 7:
            fail(f"decision_flow expected 7 stages, got {len(stages)}")
        if not mi or mi.get("pending") or mi.get("error"):
            await client.get(f"{ENGINE}/strategy-state")
            await asyncio.sleep(3)
            summary = (await client.get(f"{ENGINE}/market-summary")).json()
            mi = summary.get("market_intelligence") or {}
            flow = summary.get("decision_flow") or {}
            stages = flow.get("stages") or []
        if not mi or mi.get("pending") or mi.get("error"):
            fail(f"market_intelligence not ready: {mi}")
        ok(f"MI version={mi.get('version')} dq={mi.get('data_quality', {}).get('state')}")
        ok(f"decision_flow stages={len(stages)} no_trade={flow.get('no_trade_code')}")
        ml = summary.get("ml_shadow") or {}
        if ml.get("gate_enabled"):
            fail("ml gate must stay disabled (Phase 16 not enabled)")
        ok(f"ML shadow mode={ml.get('mode', 'shadow')} gate={ml.get('gate_enabled', False)}")

        print("\n==> Web app")
        web_health = await wait_health(WEB, timeout=60.0)
        if not web_health.get("db_ok", True):
            fail("web db_ok=false")
        login = await client.post(
            f"{WEB}/api/auth/login",
            json={"username": "admin", "password": "algoflat"},
        )
        if login.status_code != 200:
            fail(f"web login failed {login.status_code}")
        token = login.json().get("access_token")
        headers = {"Authorization": f"Bearer {token}"}
        dash = await client.get(f"{WEB}/api/market-summary", headers=headers)
        if dash.status_code != 200:
            fail(f"market-summary via web {dash.status_code}")
        ok("web login + market-summary proxy")

    print("\n==> Upstox E2E check PASSED\n")


if __name__ == "__main__":
    asyncio.run(main())
