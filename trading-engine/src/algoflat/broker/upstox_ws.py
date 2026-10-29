"""Upstox V3 market-data WebSocket — protobuf LTPC/full feed."""
from __future__ import annotations

import asyncio
import json
import ssl
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

import structlog
import websockets

from algoflat.broker.upstox_feed_pb2 import FeedResponse
from algoflat.broker.upstox_rest import UpstoxRestClient
from algoflat.models.events import QuoteUpdate
from algoflat.time_util import age_seconds, from_epoch_ms, now_ist

logger = structlog.get_logger(__name__)

WS_V3_URL = "wss://api.upstox.com/v3/feed/market-data-feed"
_FEED_TYPE_NAMES = {0: "initial_feed", 1: "live_feed", 2: "market_info"}


def _ltp_from_feed(feed: Any) -> float | None:
  if feed.HasField("ltpc") and feed.ltpc.ltp > 0:
    return float(feed.ltpc.ltp)
  if feed.HasField("fullFeed"):
    ff = feed.fullFeed
    if ff.HasField("indexFF") and ff.indexFF.HasField("ltpc") and ff.indexFF.ltpc.ltp > 0:
      return float(ff.indexFF.ltpc.ltp)
    if ff.HasField("marketFF") and ff.marketFF.HasField("ltpc") and ff.marketFF.ltpc.ltp > 0:
      return float(ff.marketFF.ltpc.ltp)
  if feed.HasField("firstLevelWithGreeks"):
    fl = feed.firstLevelWithGreeks
    if fl.HasField("ltpc") and fl.ltpc.ltp > 0:
      return float(fl.ltpc.ltp)
  return None


def _ts_from_feed(feed: Any, fallback: datetime) -> datetime:
  """Prefer exchange ltt (epoch ms) when present; else local receive time (IST)."""
  if feed.HasField("ltpc") and feed.ltpc.ltt > 0:
    return from_epoch_ms(int(feed.ltpc.ltt))
  if feed.HasField("fullFeed"):
    ff = feed.fullFeed
    if ff.HasField("indexFF") and ff.indexFF.HasField("ltpc") and ff.indexFF.ltpc.ltt > 0:
      return from_epoch_ms(int(ff.indexFF.ltpc.ltt))
    if ff.HasField("marketFF") and ff.marketFF.HasField("ltpc") and ff.marketFF.ltpc.ltt > 0:
      return from_epoch_ms(int(ff.marketFF.ltpc.ltt))
  if feed.HasField("firstLevelWithGreeks"):
    fl = feed.firstLevelWithGreeks
    if fl.HasField("ltpc") and fl.ltpc.ltt > 0:
      return from_epoch_ms(int(fl.ltpc.ltt))
  return fallback


def _depth_from_feed(feed: Any) -> tuple[float | None, float | None, int | None, float | None]:
  bid = ask = oi = vol = None
  if feed.HasField("fullFeed") and feed.fullFeed.HasField("marketFF"):
    mf = feed.fullFeed.marketFF
    if mf.marketLevel.bidAskQuote:
      q = mf.marketLevel.bidAskQuote[0]
      if q.bidP > 0:
        bid = float(q.bidP)
      if q.askP > 0:
        ask = float(q.askP)
    if mf.oi:
      oi = int(mf.oi)
    if mf.vtt:
      vol = int(mf.vtt)
  elif feed.HasField("firstLevelWithGreeks") and feed.firstLevelWithGreeks.HasField("firstDepth"):
    q = feed.firstLevelWithGreeks.firstDepth
    if q.bidP > 0:
      bid = float(q.bidP)
    if q.askP > 0:
      ask = float(q.askP)
    if feed.firstLevelWithGreeks.oi:
      oi = int(feed.firstLevelWithGreeks.oi)
    if feed.firstLevelWithGreeks.vtt:
      vol = int(feed.firstLevelWithGreeks.vtt)
  return bid, ask, oi, vol


class UpstoxWebSocketFeed:
  """Async Upstox market-data feed with auto-reconnect."""

  def __init__(
    self,
    client: UpstoxRestClient,
    *,
    mode: str = "ltpc",
    stale_seconds: float = 30.0,
    reconnect_delays: tuple[float, ...] = (1, 2, 4, 8, 16, 32, 60),
  ) -> None:
    self._client = client
    self._mode = mode
    self._stale_seconds = stale_seconds
    self._reconnect_delays = reconnect_delays
    self._callback: Callable[[QuoteUpdate], None] | None = None
    self._ikey_map: dict[str, tuple[str, str]] = {}
    self._token_map: dict[str, tuple[str, str]] = {}
    self._instrument_keys: list[str] = []
    self._task: asyncio.Task | None = None
    self._ws: Any = None
    self._connected = False
    self._last_message_ts: datetime | None = None
    self._last_quote_dispatch_ts: datetime | None = None
    self._stop = asyncio.Event()
    self._proto_log_budget = 5

  @property
  def is_open(self) -> bool:
    return self._connected

  @property
  def last_message_ts(self) -> datetime | None:
    return self._last_message_ts

  def is_stale(self, max_age_sec: float | None = None) -> bool:
    """Connection-level staleness — quote freshness is tracked in EngineApp._ws_stale."""
    del max_age_sec
    return not self._connected

  def _sync_maps(self, ikey_map: dict[str, tuple[str, str]]) -> None:
    self._ikey_map = dict(ikey_map)
    self._instrument_keys = list(ikey_map.keys())
    self._token_map = {tok: (ex, tok) for ex, tok in ikey_map.values()}

  def _resolve_mapped(self, ikey: str) -> tuple[str, str] | None:
    hit = self._ikey_map.get(ikey)
    if hit is not None:
      return hit
    for variant in (ikey.replace(":", "|"), ikey.replace("|", ":")):
      hit = self._ikey_map.get(variant)
      if hit is not None:
        return hit
    tail = ikey.rsplit("|", 1)[-1] if "|" in ikey else ikey.rsplit(":", 1)[-1]
    return self._token_map.get(tail)

  @property
  def last_quote_dispatch_ts(self) -> datetime | None:
    return self._last_quote_dispatch_ts

  def quotes_flowing(self, max_age_sec: float = 10.0) -> bool:
    if self._last_quote_dispatch_ts is None:
      return False
    age = age_seconds(self._last_quote_dispatch_ts)
    return age is not None and age <= max_age_sec

  async def start(
    self,
    *,
    instruments: list[str],
    ikey_map: dict[str, tuple[str, str]],
    on_quote: Callable[[QuoteUpdate], None],
  ) -> None:
    self._callback = on_quote
    self._sync_maps(ikey_map)
    self._stop.clear()
    if self._task is None or self._task.done():
      self._task = asyncio.create_task(self._run_loop(), name="upstox-ws-feed")
    # Subscription is sent inside _run_loop after the socket connects.

  async def stop(self) -> None:
    self._stop.set()
    self._connected = False
    task = self._task
    self._task = None
    if task is not None:
      task.cancel()
      try:
        await task
      except asyncio.CancelledError:
        pass

  async def resubscribe(
    self,
    *,
    instruments: list[str],
    ikey_map: dict[str, tuple[str, str]],
  ) -> None:
    del instruments
    self._sync_maps(ikey_map)
    ws = self._ws
    if ws is not None and self._connected and self._instrument_keys:
      await self._send_subscription(ws)

  def _encode_subscription(self, *, method: str = "sub", mode: str | None = None) -> bytes:
    payload: dict[str, Any] = {
      "guid": str(uuid.uuid4()),
      "method": method,
      "data": {"instrumentKeys": self._instrument_keys},
    }
    if mode is not None:
      payload["data"]["mode"] = mode
    # Upstox V3 requires BINARY frames (UTF-8 JSON bytes), not text frames.
    return json.dumps(payload).encode("utf-8")

  async def _send_subscription(self, ws: Any) -> None:
    await ws.send(self._encode_subscription(method="sub", mode=self._mode))
    logger.info(
      "upstox_websocket_subscribed",
      keys=len(self._instrument_keys),
      mode=self._mode,
      frame="binary",
    )

  async def _run_loop(self) -> None:
    attempt = 0
    while not self._stop.is_set():
      try:
        ssl_ctx = ssl.create_default_context()
        # Official SDK connects with Bearer token on the V3 endpoint (not redirect URL).
        async with websockets.connect(
          WS_V3_URL,
          ssl=ssl_ctx,
          additional_headers={"Authorization": f"Bearer {self._client.access_token}"},
          ping_interval=20,
          ping_timeout=20,
          close_timeout=5,
          max_size=8 * 1024 * 1024,
        ) as ws:
          self._ws = ws
          self._connected = True
          self._last_message_ts = now_ist()
          self._proto_log_budget = 5
          attempt = 0
          logger.info("upstox_websocket_connected", mode=self._mode, keys=len(self._instrument_keys))
          await asyncio.sleep(0.5)
          if self._instrument_keys:
            await self._send_subscription(ws)

          while not self._stop.is_set():
            try:
              raw = await ws.recv()
            except asyncio.CancelledError:
              raise
            self._last_message_ts = now_ist()
            if isinstance(raw, str):
              logger.info("upstox_ws_text_message", body=raw[:500])
              continue
            self._dispatch_protobuf(raw)
      except asyncio.CancelledError:
        raise
      except Exception:
        self._connected = False
        logger.exception("upstox_websocket_failed", attempt=attempt)
      finally:
        self._connected = False
        self._ws = None

      if self._stop.is_set():
        break
      delay = self._reconnect_delays[min(attempt, len(self._reconnect_delays) - 1)]
      attempt += 1
      await asyncio.sleep(delay)

  def _dispatch_protobuf(self, raw: bytes) -> None:
    if self._callback is None:
      return
    try:
      resp = FeedResponse()
      resp.ParseFromString(raw)
    except Exception:
      logger.debug("upstox_ws_decode_failed")
      return
    if self._proto_log_budget > 0:
      self._proto_log_budget -= 1
      logger.info(
        "upstox_ws_proto",
        type=_FEED_TYPE_NAMES.get(resp.type, resp.type),
        feeds=len(resp.feeds),
      )
    recv_ts = now_ist()
    dispatched = 0
    for ikey, feed in resp.feeds.items():
      ltp = _ltp_from_feed(feed)
      if ltp is None or ltp <= 0:
        continue
      mapped = self._resolve_mapped(ikey)
      if mapped is None:
        logger.debug("upstox_ws_unmapped_key", ikey=ikey)
        continue
      exchange, token = mapped
      bid, ask, oi, vol = _depth_from_feed(feed)
      quote = QuoteUpdate(
        ts=_ts_from_feed(feed, recv_ts),
        exchange=exchange,
        instrument_token=token,
        ltp=Decimal(str(ltp)),
        bid=Decimal(str(bid)) if bid is not None else None,
        ask=Decimal(str(ask)) if ask is not None else None,
        volume=vol,
        oi=oi,
        source="websocket",
      )
      self._callback(quote)
      dispatched += 1
    if dispatched:
      self._last_quote_dispatch_ts = recv_ts
      logger.debug("upstox_ws_quotes_dispatched", count=dispatched)
