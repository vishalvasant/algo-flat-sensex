"""Shared Upstox v2 REST client."""
from __future__ import annotations

import asyncio
import time
import urllib.parse
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

BASE_URL = "https://api.upstox.com/v2"
_MIN_INTERVAL_SEC = 0.22
_RETRY_BACKOFF_SEC = (0.0, 1.0, 2.5, 5.0)


class UpstoxRestClient:
    def __init__(self, access_token: str) -> None:
        self._token = access_token
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    @property
    def access_token(self) -> str:
        return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    async def _throttle(self) -> None:
        async with self._lock:
            elapsed = time.monotonic() - self._last_call
            if elapsed < _MIN_INTERVAL_SEC:
                await asyncio.sleep(_MIN_INTERVAL_SEC - elapsed)
            self._last_call = time.monotonic()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{BASE_URL}{path}"
        headers = self._headers()
        if json_body is not None:
            headers = {**headers, "Content-Type": "application/json"}

        last_resp: httpx.Response | None = None
        for attempt, delay in enumerate(_RETRY_BACKOFF_SEC):
            if delay > 0:
                await asyncio.sleep(delay)
            await self._throttle()
            async with httpx.AsyncClient(timeout=45.0) as client:
                if method == "GET":
                    last_resp = await client.get(url, params=params, headers=headers)
                else:
                    last_resp = await client.post(url, json=json_body, headers=headers)
            if last_resp.status_code != 429 or attempt >= len(_RETRY_BACKOFF_SEC) - 1:
                return self._parse(last_resp)
            logger.warning(
                "upstox_rate_limited_retry",
                path=path,
                attempt=attempt + 1,
                wait_sec=_RETRY_BACKOFF_SEC[attempt + 1],
            )
        assert last_resp is not None
        return self._parse(last_resp)

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, *, json_body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", path, json_body=json_body)

    @staticmethod
    def _parse(resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code == 401:
            raise ConnectionError("Upstox access token expired or invalid")
        try:
            data = resp.json()
        except Exception:
            raise ConnectionError(f"Upstox API {resp.status_code}: {resp.text[:200]}")
        if not resp.is_success:
            errors = data.get("errors") or data.get("message") or data
            raise ConnectionError(f"Upstox API {resp.status_code}: {errors}")
        return data if isinstance(data, dict) else {"data": data}

    async def get_profile(self) -> dict[str, Any]:
        payload = await self.get("/user/profile")
        return payload.get("data") or {}

    async def get_funds(self) -> dict[str, Any]:
        payload = await self.get("/user/get-funds-and-margin")
        return payload.get("data") or {}

    async def get_positions(self) -> list[dict[str, Any]]:
        payload = await self.get("/portfolio/short-term-positions")
        rows = payload.get("data") or []
        return rows if isinstance(rows, list) else []

    async def get_order_book(self) -> list[dict[str, Any]]:
        payload = await self.get("/order/retrieve-all")
        rows = payload.get("data") or []
        return rows if isinstance(rows, list) else []

    async def get_trades_for_day(self) -> list[dict[str, Any]]:
        payload = await self.get("/order/trades-for-day")
        rows = payload.get("data") or []
        return rows if isinstance(rows, list) else []

    async def place_order(self, body: dict[str, Any]) -> dict[str, Any]:
        payload = await self.post("/order/place", json_body=body)
        return payload.get("data") or {}

    async def get_quotes(self, instrument_keys: list[str]) -> dict[str, Any]:
        if not instrument_keys:
            return {}
        payload = await self.get(
            "/market-quote/quotes",
            params={"instrument_key": ",".join(instrument_keys)},
        )
        return payload.get("data") or {}

    async def get_option_contracts(self, expiry_date: str) -> list[dict[str, Any]]:
        payload = await self.get(
            "/option/contract",
            params={
                "instrument_key": "NSE_INDEX|Nifty 50",
                "expiry_date": expiry_date,
            },
        )
        rows = payload.get("data") or []
        return rows if isinstance(rows, list) else []

    async def get_intraday_candles(
        self,
        instrument_key: str,
        interval: str,
    ) -> list[list[Any]]:
        enc = urllib.parse.quote(instrument_key, safe="")
        path = f"/historical-candle/intraday/{enc}/{interval}"
        payload = await self.get(path)
        return list(payload.get("data", {}).get("candles") or [])

    async def get_historical_candles(
        self,
        instrument_key: str,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> list[list[Any]]:
        enc = urllib.parse.quote(instrument_key, safe="")
        path = f"/historical-candle/{enc}/{interval}/{to_date}/{from_date}"
        payload = await self.get(path)
        return list(payload.get("data", {}).get("candles") or [])

    async def authorize_market_feed(self) -> str:
        payload = await self.get("/feed/market-data-feed/authorize")
        data = payload.get("data") or {}
        url = data.get("authorized_redirect_uri") or data.get("authorizedRedirectUri")
        if not url:
            raise ConnectionError("Upstox market feed authorize returned no websocket URL")
        return str(url)

    async def authorize_market_feed_v3(self) -> str:
        """V3 authorize — preferred for protobuf LTPC/full stream."""
        await self._throttle()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                "https://api.upstox.com/v3/feed/market-data-feed/authorize",
                headers=self._headers(),
            )
        if resp.status_code == 401:
            raise ConnectionError("Upstox access token expired or invalid")
        body = resp.json()
        if not resp.is_success:
            raise ConnectionError(f"Upstox V3 feed authorize failed: {body}")
        data = body.get("data") or {}
        url = data.get("authorized_redirect_uri") or data.get("authorizedRedirectUri")
        if not url:
            raise ConnectionError("Upstox V3 market feed authorize returned no websocket URL")
        return str(url)
