"""Upstox broker adapter — replaces Flattrade for live + market data."""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

import structlog

from algoflat.broker.base import BrokerAdapter
from algoflat.broker.flattrade import (
    AccountLimits,
    BrokerDayPnl,
    _decimal_field,
    _limit_price_for_api,
)
from algoflat.broker.instruments import to_upstox_instrument_key
from algoflat.broker.upstox_auth import ensure_session, resolve_session
from algoflat.broker.upstox_credentials import load_upstox_config
from algoflat.broker.upstox_rest import UpstoxRestClient
from algoflat.broker.upstox_ws import UpstoxWebSocketFeed
from algoflat.config import AppConfig
from algoflat.contract_selector.expiry import parse_expiry_tag
from algoflat.models.events import Candle, CandleInterval, ExecutionRequest, OrderUpdate, QuoteUpdate, TradingMode
from algoflat.symbols_util import lot_size_for_tsym
from algoflat.time_util import IST, now_ist

logger = structlog.get_logger(__name__)

_INTERVAL_MAP = {
    CandleInterval.M1: "1minute",
    CandleInterval.M3: "3minute",
    CandleInterval.M5: "5minute",
}

_PRODUCT_MAP = {
    "MIS": "I",
    "I": "I",
    "NRML": "D",
    "D": "D",
    "CNC": "D",
}

_OPTION_TSYM = re.compile(r"^(?P<ul>[A-Z0-9]+)(?P<tag>\d{2}[A-Z]{3}\d{2})[CP]\d+")


def _parse_upstox_limits(raw: dict[str, Any]) -> AccountLimits:
    equity = _decimal_field(raw, "equity", "available_margin")
    available = _decimal_field(raw, "available_margin", "equity")
    used = _decimal_field(raw, "used_margin", "span_margin")
    collateral = _decimal_field(raw, "collateral", "adhoc_margin")
    if available <= 0 and equity > 0:
        available = equity
    return AccountLimits(
        cash=equity,
        available=available,
        margin_used=used,
        collateral=collateral,
    )


def _parse_upstox_day_pnl(positions: list[dict[str, Any]]) -> BrokerDayPnl:
    realized = Decimal("0")
    unrealized = Decimal("0")
    for row in positions:
        realized += _decimal_field(row, "realised", "realized")
        unrealized += _decimal_field(row, "pnl", "unrealised", "unrealized")
    return BrokerDayPnl(realized_pnl=realized, unrealized_pnl=unrealized)


def parse_upstox_open_positions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            qty = int(float(row.get("quantity") or row.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty == 0:
            continue
        tsym = str(row.get("tradingsymbol") or row.get("symbol") or "").strip()
        if not tsym:
            continue
        token = str(row.get("instrument_token") or row.get("exchange_token") or tsym)
        exchange = str(row.get("exchange") or "NFO").strip()
        avg = _decimal_field(row, "average_price", "avg_price")
        ltp = _decimal_field(row, "last_price", "ltp")
        if ltp <= 0:
            ltp = avg
        lot_size = lot_size_for_tsym(tsym)
        side = "PE" if tsym.upper().endswith("PE") else "CE"
        out.append(
            {
                "position_id": f"broker-{token}",
                "instrument_token": token,
                "exchange": exchange,
                "tsym": tsym,
                "side": side,
                "quantity": abs(qty),
                "lot_size": lot_size,
                "lots": max(1, abs(qty) // max(lot_size, 1)),
                "entry_price": float(avg),
                "broker_avg_price": float(avg),
                "entry_price_source": "broker_display_only",
                "entry_ts": None,
                "current_ltp": float(ltp),
                "unrealized_pnl": float(_decimal_field(row, "pnl", "unrealised")),
                "premium_deployed": float(avg * abs(qty)),
                "setup_type": "broker",
                "mode": "live",
                "source": "broker",
            }
        )
    return out


def _expiry_from_tsym(tsym: str) -> str | None:
    m = _OPTION_TSYM.match(tsym.upper())
    if not m:
        return None
    return m.group("tag").upper()


def _lookup_quote_row(data: dict[str, Any], instrument_key: str) -> dict[str, Any] | None:
    """Upstox may return keys with ':' or trading symbols instead of request tokens."""
    if not isinstance(data, dict) or not data:
        return None
    row = data.get(instrument_key)
    if row:
        return row
    alt = instrument_key.replace("|", ":")
    row = data.get(alt)
    if row:
        return row
    norm = instrument_key.replace(":", "|")
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        if key.replace(":", "|") == norm:
            return value
        row_key = str(value.get("instrument_token") or "").replace(":", "|")
        if row_key == norm:
            return value
    if len(data) == 1:
        only = next(iter(data.values()))
        return only if isinstance(only, dict) else None
    return None


def _quote_to_noren_shape(
    *,
    exchange: str,
    token: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    ohlc = row.get("ohlc") or {}
    depth = row.get("depth") or {}
    buy = (depth.get("buy") or [{}])[0] if isinstance(depth.get("buy"), list) else {}
    sell = (depth.get("sell") or [{}])[0] if isinstance(depth.get("sell"), list) else {}
    ltp = row.get("last_price") or ohlc.get("close")
    return {
        "stat": "Ok",
        "lp": ltp,
        "c": ohlc.get("close"),
        "bp1": buy.get("price"),
        "sp1": sell.get("price"),
        "tsym": row.get("symbol") or token,
        "v": row.get("volume"),
        "oi": row.get("oi"),
        "exchange": exchange,
        "token": token,
    }


class UpstoxAdapter(BrokerAdapter):
    """Upstox v2 REST broker — drop-in replacement for FlattradeAdapter."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._connected = False
        self._client: UpstoxRestClient | None = None
        self._access_token: str | None = None
        self._session_user_id: str | None = None
        self._quote_callback: Callable[[QuoteUpdate], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribed: list[str] = []
        self._key_map: dict[str, tuple[str, str]] = {}
        self._poll_task: asyncio.Task | None = None
        self._poll_interval = float(config.broker.get("rest_poll_interval_seconds", 1.5))
        self._use_websocket = bool(config.broker.get("use_websocket_feed", True))
        self._ws_mode = str(config.broker.get("websocket_feed_mode", "ltpc"))
        self._ws_feed: UpstoxWebSocketFeed | None = None
        self._ws_stale_sec = float(config.broker.get("stale_feed_seconds", 30))
        self._snapshot_cache: tuple[float, AccountLimits, BrokerDayPnl, list[dict[str, Any]]] | None = None
        self._snapshot_cache_ttl_sec = 12.0
        self._trade_book_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._trade_book_cache_ttl_sec = 30.0
        self._instrument_key_cache: dict[tuple[str, str], str] = {}

    def _instrument_key(self, exchange: str, token: str, tsym: str | None = None) -> str:
        key = (exchange, token)
        if key not in self._instrument_key_cache:
            self._instrument_key_cache[key] = to_upstox_instrument_key(exchange, token, tsym=tsym)
        return self._instrument_key_cache[key]

    @property
    def session_user_id(self) -> str | None:
        return self._session_user_id

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def websocket_open(self) -> bool:
        return bool(self._ws_feed and self._ws_feed.is_open)

    @property
    def websocket_stale(self) -> bool:
        if self._ws_feed is None:
            return True
        return self._ws_feed.is_stale(self._ws_stale_sec)

    @property
    def rest_poll_active(self) -> bool:
        return self._poll_task is not None and not self._poll_task.done()

    @property
    def uses_rest_market_feed(self) -> bool:
        """True when REST poll is the primary feed (WS disabled or unavailable)."""
        return not self._use_websocket or not self.websocket_open

    async def connect(self) -> None:
        cfg = await load_upstox_config()
        session = await ensure_session(cfg)
        self._access_token = session.access_token
        self._client = UpstoxRestClient(session.access_token)
        profile = await self._client.get_profile()
        self._session_user_id = str(profile.get("user_id") or profile.get("user_name") or "")
        self._loop = asyncio.get_running_loop()
        self._connected = True
        logger.info(
            "upstox_connected",
            user_id=self._session_user_id,
            token_source=session.source,
            expires_at=session.expires_at.isoformat(),
        )

    async def disconnect(self) -> None:
        await self.stop_websocket()
        self._connected = False
        self._client = None

    async def stop_websocket(self) -> None:
        if self._ws_feed is not None:
            await self._ws_feed.stop()
            self._ws_feed = None
        task = self._poll_task
        self._poll_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("upstox_stream_stopped")

    async def get_candles(
        self,
        exchange: str,
        token: str,
        interval: CandleInterval,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        if interval in (CandleInterval.M3, CandleInterval.M5):
            from algoflat.features.indicators import aggregate_from_m1

            m1 = await self.get_candles(exchange, token, CandleInterval.M1, start, end)
            minutes = 3 if interval == CandleInterval.M3 else 5
            return aggregate_from_m1(m1, minutes=minutes)
        return await self._fetch_m1_candles(exchange, token, start, end)

    async def _fetch_m1_candles(
        self,
        exchange: str,
        token: str,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        if self._client is None:
            await self.connect()
        assert self._client is not None
        ikey = self._instrument_key(exchange, token)
        intrv = "1minute"
        from_d = start.astimezone(timezone.utc).date().isoformat()
        to_d = end.astimezone(timezone.utc).date().isoformat()
        rows: list[list[Any]] = []
        today_ist = datetime.now(tz=IST).date()
        end_ist = end.astimezone(IST).date()
        start_ist = start.astimezone(IST).date()
        if end_ist >= today_ist:
            try:
                rows.extend(await self._client.get_intraday_candles(ikey, intrv))
            except Exception:
                logger.warning("upstox_intraday_candles_failed", instrument=ikey)
        if start_ist < today_ist:
            hist_to = min(end_ist, today_ist - timedelta(days=1)).isoformat()
            if hist_to >= from_d:
                rows.extend(
                    await self._client.get_historical_candles(ikey, intrv, from_d, hist_to)
                )
        elif not rows:
            rows = await self._client.get_historical_candles(ikey, intrv, from_d, to_d)
        candles: list[Candle] = []
        for row in rows:
            if len(row) < 5:
                continue
            ts_raw = row[0]
            if isinstance(ts_raw, str):
                ts = datetime.fromisoformat(ts_raw)
            else:
                ts = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = ts.astimezone(timezone.utc)
            candles.append(
                Candle(
                    instrument_token=token,
                    ts=ts,
                    open=Decimal(str(row[1])),
                    high=Decimal(str(row[2])),
                    low=Decimal(str(row[3])),
                    close=Decimal(str(row[4])),
                    volume=int(row[5]) if len(row) > 5 and row[5] is not None else None,
                    interval=CandleInterval.M1,
                    oi=int(row[6]) if len(row) > 6 and row[6] is not None else None,
                )
            )
        candles.sort(key=lambda c: c.ts)
        return candles

    async def get_quotes(self, exchange: str, token: str) -> dict[str, Any]:
        if self._client is None:
            await self.connect()
        assert self._client is not None
        ikey = self._instrument_key(exchange, token)
        data = await self._client.get_quotes([ikey])
        row = _lookup_quote_row(data, ikey)
        if not row:
            return {"stat": "Not_Ok", "emsg": "quote unavailable"}
        return _quote_to_noren_shape(exchange=exchange, token=token, row=row)

    async def search_scrip(self, exchange: str, search_text: str) -> list[dict[str, Any]]:
        """Return pseudo Flattrade search rows — NIFTY options or known index tickers."""
        text = search_text.upper().strip()
        if text in ("INDIAVIX", "INDIA VIX"):
            return [
                {
                    "tsym": "INDIAVIX",
                    "token": "India VIX",
                    "instname": "INDEX",
                    "exch": "NSE",
                }
            ]
        if text != "NIFTY":
            return []
        if self._client is None:
            await self.connect()
        assert self._client is not None
        from algoflat.contract_selector.expiry import next_weekly_expiry_dates, format_expiry_tag

        rows: list[dict[str, Any]] = []
        for exp_d in next_weekly_expiry_dates(underlying="NIFTY", count=4):
            tag = format_expiry_tag(exp_d)
            try:
                contracts = await self._client.get_option_contracts(exp_d.isoformat())
            except Exception:
                continue
            for c in contracts[:4]:
                tsym = str(c.get("trading_symbol") or "")
                if not tsym:
                    continue
                rows.append(
                    {
                        "tsym": tsym,
                        "token": str(c.get("exchange_token") or ""),
                        "instname": "OPTIDX",
                        "exch": "NFO",
                        "expiry_tag": tag,
                    }
                )
        return rows

    async def get_option_chain(
        self,
        exchange: str,
        tradingsymbol: str,
        strikeprice: float,
        count: int,
    ) -> list[dict[str, Any]]:
        del exchange
        if self._client is None:
            await self.connect()
        assert self._client is not None
        tag = _expiry_from_tsym(tradingsymbol)
        if not tag:
            return []
        exp_d = parse_expiry_tag(tag)
        if exp_d is None:
            return []
        contracts = await self._client.get_option_contracts(exp_d.isoformat())
        atm = float(strikeprice)
        step = float(self._config.symbols.get("strike_step", 50))
        band = count * step
        out: list[dict[str, Any]] = []
        for c in contracts:
            strike = float(c.get("strike_price") or 0)
            if abs(strike - atm) > band:
                continue
            opt = str(c.get("instrument_type") or "").upper()
            if opt not in ("CE", "PE"):
                continue
            out.append(
                {
                    "strprc": strike,
                    "optt": opt,
                    "token": str(c.get("exchange_token") or ""),
                    "tsym": str(c.get("trading_symbol") or ""),
                    "exch": "NFO",
                }
            )
        return out

    @property
    def websocket_quotes_flowing(self) -> bool:
        if self._ws_feed is None:
            return False
        return self._ws_feed.quotes_flowing(self._ws_stale_sec)

    async def subscribe(
        self,
        instruments: list[str],
        on_quote: Any,
        on_order: Any | None = None,
    ) -> None:
        del on_order
        self._quote_callback = on_quote
        instruments = list(dict.fromkeys(instruments))
        self._subscribed = instruments
        self._key_map = {}
        self._ikey_map: dict[str, tuple[str, str]] = {}
        for key in instruments:
            if "|" not in key:
                continue
            ex, tok = key.split("|", 1)
            self._key_map[key] = (ex, tok)
            ikey = self._instrument_key(ex, tok)
            self._ikey_map[ikey] = (ex, tok)

        if self._client is None:
            await self.connect()
        assert self._client is not None

        if self._use_websocket:
            if self._ws_feed is None:
                self._ws_feed = UpstoxWebSocketFeed(
                    self._client,
                    mode=self._ws_mode,
                    stale_seconds=self._ws_stale_sec,
                )
                await self._ws_feed.start(
                    instruments=instruments,
                    ikey_map=self._ikey_map,
                    on_quote=on_quote,
                )
            else:
                self._ws_feed._callback = on_quote
                await self._ws_feed.resubscribe(
                    instruments=instruments,
                    ikey_map=self._ikey_map,
                )

        # REST poll always runs as backup; slows when WS is healthy.
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop(), name="upstox-quote-poll")
        logger.info(
            "upstox_subscribed",
            instruments=len(instruments),
            websocket=self._use_websocket,
            ws_open=self.websocket_open,
        )

    async def _poll_loop(self) -> None:
        while self._subscribed and self._quote_callback is not None:
            try:
                # Slow REST backup only when WebSocket is actually delivering quotes.
                if (
                    self._use_websocket
                    and self.websocket_open
                    and self.websocket_quotes_flowing
                ):
                    await asyncio.sleep(max(self._poll_interval * 3, 4.0))
                    continue
                if self._client is None:
                    await self.connect()
                assert self._client is not None
                keys = [
                    self._instrument_key(ex, tok)
                    for ex, tok in self._key_map.values()
                ]
                if keys:
                    data = await self._client.get_quotes(keys)
                    for sub_key, (ex, tok) in self._key_map.items():
                        ikey = self._instrument_key(ex, tok)
                        row = _lookup_quote_row(data, ikey)
                        if not row:
                            continue
                        shaped = _quote_to_noren_shape(exchange=ex, token=tok, row=row)
                        ltp = self.parse_decimal(shaped.get("lp"))
                        if ltp is None:
                            continue
                        quote = QuoteUpdate(
                            ts=now_ist(),
                            exchange=ex,
                            instrument_token=tok,
                            tsym=shaped.get("tsym"),
                            ltp=ltp,
                            bid=self.parse_decimal(shaped.get("bp1")),
                            ask=self.parse_decimal(shaped.get("sp1")),
                            volume=int(shaped["v"]) if shaped.get("v") not in (None, "") else None,
                            oi=int(shaped["oi"]) if shaped.get("oi") not in (None, "") else None,
                            source="rest",
                        )
                        if self._quote_callback:
                            self._quote_callback(quote)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("upstox_poll_failed")
            await asyncio.sleep(self._poll_interval)

    async def place_order(self, request: ExecutionRequest) -> OrderUpdate:
        if self._client is None:
            await self.connect()
        assert self._client is not None
        started = time.monotonic()
        now = datetime.now(tz=timezone.utc)
        product = _PRODUCT_MAP.get(str(request.product or "MIS").upper(), "I")
        order_type = (request.order_type or "LMT").upper()
        upstox_type = "MARKET" if order_type == "MKT" else "LIMIT"
        price = float(
            _limit_price_for_api(
                side=request.side,
                reference_ltp=request.reference_ltp,
                limit_price=request.limit_price,
            )
        )
        if upstox_type == "MARKET":
            price = 0.0
        ikey = self._instrument_key(request.exchange, request.instrument_token, tsym=request.tsym)
        body = {
            "quantity": int(request.quantity),
            "product": product,
            "validity": "DAY",
            "price": price,
            "tag": (request.client_order_id or "algoflat")[:20],
            "instrument_token": ikey,
            "order_type": upstox_type,
            "transaction_type": request.side.upper(),
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }
        try:
            data = await self._client.place_order(body)
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            return OrderUpdate(
                ts=now,
                client_order_id=request.client_order_id,
                broker_order_id=None,
                status="REJECTED",
                report_type="Reject",
                fill_price=None,
                filled_qty=0,
                avg_price=None,
                slippage=None,
                latency_ms=latency_ms,
                mode=TradingMode.LIVE,
                rejection_reason=str(exc),
            )

        broker_id = str(data.get("order_id") or "")
        fill_price, filled_qty, status, reason = await self._resolve_order_fill(
            broker_id=broker_id,
            request=request,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        if status == "REJECTED":
            return OrderUpdate(
                ts=now,
                client_order_id=request.client_order_id,
                broker_order_id=broker_id or None,
                status="REJECTED",
                report_type="Reject",
                fill_price=None,
                filled_qty=0,
                avg_price=None,
                slippage=None,
                latency_ms=latency_ms,
                mode=TradingMode.LIVE,
                rejection_reason=reason,
            )
        slippage = None
        if fill_price is not None and request.reference_ltp is not None:
            slippage = fill_price - request.reference_ltp
        self.invalidate_trade_book_cache()
        self._snapshot_cache = None
        return OrderUpdate(
            ts=now,
            client_order_id=request.client_order_id,
            broker_order_id=broker_id or None,
            status=status,
            report_type="Fill" if status == "COMPLETE" else "Ack",
            fill_price=fill_price,
            filled_qty=filled_qty,
            avg_price=fill_price,
            slippage=slippage,
            latency_ms=latency_ms,
            mode=TradingMode.LIVE,
        )

    async def _resolve_order_fill(
        self,
        *,
        broker_id: str,
        request: ExecutionRequest,
        max_wait_sec: float = 12.0,
    ) -> tuple[Decimal | None, int, str, str | None]:
        deadline = time.monotonic() + max_wait_sec
        while time.monotonic() < deadline:
            row = await self._find_order_row(broker_id)
            if row is not None:
                status = str(row.get("status") or "").upper()
                avg = row.get("average_price") or row.get("price")
                qty = row.get("filled_quantity") or row.get("quantity") or request.quantity
                if status in ("COMPLETE", "TRADED", "FILLED"):
                    if avg not in (None, "", 0):
                        return Decimal(str(avg)), int(qty), "COMPLETE", None
                if status in ("REJECTED", "CANCELLED", "CANCELED"):
                    return None, 0, "REJECTED", "broker rejected or canceled order"
            await asyncio.sleep(0.4)

        ref = Decimal(str(request.reference_ltp or 0))
        if ref > 0:
            logger.warning(
                "upstox_fill_using_reference_ltp",
                tsym=request.tsym,
                broker_order_id=broker_id,
            )
            return ref, int(request.quantity), "COMPLETE", None
        return None, 0, "REJECTED", "order not confirmed filled"

    async def _find_order_row(self, broker_id: str) -> dict[str, Any] | None:
        if not broker_id or self._client is None:
            return None
        book = await self._client.get_order_book()
        for row in book:
            if str(row.get("order_id") or "") == broker_id:
                return row
        return None

    def invalidate_trade_book_cache(self) -> None:
        self._trade_book_cache = None

    async def get_account_limits(self) -> AccountLimits:
        limits, _, _ = await self.fetch_broker_snapshot()
        return limits

    async def get_day_pnl(self) -> BrokerDayPnl:
        _, day, _ = await self.fetch_broker_snapshot()
        return day

    async def fetch_trade_book(self, *, force: bool = False) -> list[dict[str, Any]]:
        now = time.monotonic()
        if (
            not force
            and self._trade_book_cache is not None
            and now - self._trade_book_cache[0] < self._trade_book_cache_ttl_sec
        ):
            return list(self._trade_book_cache[1])
        if self._client is None:
            await self.connect()
        assert self._client is not None
        rows = await self._client.get_trades_for_day()
        mapped: list[dict[str, Any]] = []
        for row in rows:
            mapped.append(
                {
                    "norenordno": str(row.get("order_id") or ""),
                    "order_id": str(row.get("order_id") or ""),
                    "tsym": row.get("tradingsymbol") or row.get("trading_symbol"),
                    "qty": row.get("quantity"),
                    "fillshares": row.get("quantity"),
                    "avgprc": row.get("price"),
                    "prc": row.get("price"),
                    "trantype": "B" if str(row.get("transaction_type", "")).upper() == "BUY" else "S",
                    "exch": row.get("exchange") or "NFO",
                    "token": row.get("instrument_token"),
                }
            )
        self._trade_book_cache = (now, mapped)
        return list(mapped)

    async def fetch_open_positions(self, *, force: bool = False) -> list[dict[str, Any]]:
        await self.fetch_broker_snapshot(force=force)
        if self._snapshot_cache is not None:
            return list(self._snapshot_cache[3])
        return []

    async def fetch_broker_snapshot(
        self,
        *,
        force: bool = False,
    ) -> tuple[AccountLimits, BrokerDayPnl, list[dict[str, Any]]]:
        now = time.monotonic()
        if (
            not force
            and self._snapshot_cache is not None
            and now - self._snapshot_cache[0] < self._snapshot_cache_ttl_sec
        ):
            _, limits, day, open_pos = self._snapshot_cache
            return limits, day, list(open_pos)
        if self._client is None:
            await self.connect()
        assert self._client is not None
        funds = await self._client.get_funds()
        positions = await self._client.get_positions()
        limits = _parse_upstox_limits(funds if isinstance(funds, dict) else {})
        day = _parse_upstox_day_pnl(positions)
        open_pos = parse_upstox_open_positions(positions)
        self._snapshot_cache = (now, limits, day, open_pos)
        return limits, day, open_pos
