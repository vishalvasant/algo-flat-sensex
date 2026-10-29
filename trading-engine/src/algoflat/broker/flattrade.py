from __future__ import annotations

import asyncio
import json
import time
import urllib.parse

import requests
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

import structlog
from NorenRestApiPy.NorenApi import NorenApi

from algoflat.broker.auth import ensure_session, resolve_session
from algoflat.broker.broker_trades import fill_price_for_broker_order, open_lot_from_trade_book
from algoflat.broker.credentials import load_flattrade_config
from algoflat.broker.base import BrokerAdapter
from algoflat.broker.flattrade_ws import FlattradeMarketSocket
from algoflat.config import AppConfig
from algoflat.models.events import Candle, CandleInterval, ExecutionRequest, OrderUpdate, QuoteUpdate, TradingMode
from algoflat.symbols_util import lot_size_for_tsym

logger = structlog.get_logger(__name__)


def _optional_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        raw = row.get(key)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None

_INTERVAL_MAP = {
    CandleInterval.M1: "1",
    CandleInterval.M3: "3",
    CandleInterval.M5: "5",
}

# Flattrade/Noren product codes (config uses MIS/NRML labels).
_NOREN_PRODUCT: dict[str, str] = {
    "MIS": "I",
    "NRML": "M",
    "I": "I",
    "M": "M",
    "C": "C",
}


def noren_product_code(product: str) -> str:
    key = str(product or "MIS").upper()
    return _NOREN_PRODUCT.get(key, key)


def _limit_price_for_api(
    *,
    side: str,
    reference_ltp: Decimal | None,
    limit_price: Decimal | None,
    tick: Decimal = Decimal("0.05"),
) -> str:
    """Aggressive LMT proxy for MKT — Flattrade API rejects true market orders.

    Price must be an exact multiple of tick (0.05). Return a string so JSON/float
    never introduces 60.91-style invalid ticks.
    """
    base = limit_price if limit_price is not None else reference_ltp
    px = Decimal(str(base or "0"))
    cushion = max(Decimal("1.00"), (px * Decimal("0.02")).quantize(tick, rounding=ROUND_HALF_UP))
    if side.upper() == "BUY":
        px += cushion
    else:
        px = max(tick, px - cushion)
    # Snap to tick grid explicitly (avoid float artifacts like 60.91 / 65.39).
    ticks = int((px / tick).to_integral_value(rounding=ROUND_HALF_UP))
    snapped = (tick * ticks).quantize(tick, rounding=ROUND_HALF_UP)
    if snapped < tick:
        snapped = tick
    return format(snapped, "f")


@dataclass(frozen=True)
class AccountLimits:
    cash: Decimal
    available: Decimal
    margin_used: Decimal
    collateral: Decimal

    @property
    def equity(self) -> Decimal:
        return self.cash + self.collateral


@dataclass(frozen=True)
class BrokerDayPnl:
    """Intraday P&L from Flattrade positions API (all account trades, not algo-only)."""

    realized_pnl: Decimal
    unrealized_pnl: Decimal

    @property
    def total_pnl(self) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl


def _parse_account_limits(raw: dict[str, Any]) -> AccountLimits:
    cash = _decimal_field(raw, "cash", "availablecash", "availablelimit")
    available = _decimal_field(raw, "marginavailable", "availablemargin", "cash")
    margin_used = _decimal_field(raw, "marginused", "marginusednrml", "marginusedmis")
    collateral = _decimal_field(raw, "collateral", "brkcollamt", "brkcollateral")
    if available <= 0 and cash > 0:
        available = cash
    if margin_used <= 0:
        margin_used = max(Decimal("0"), cash - available)
    return AccountLimits(
        cash=cash,
        available=available,
        margin_used=margin_used,
        collateral=collateral,
    )


def _parse_day_pnl_from_limits(raw: dict[str, Any]) -> BrokerDayPnl | None:
    realized = _decimal_field(raw, "rpnl")
    unrealized = _decimal_field(raw, "unmtom", "urmtom", "urmtm")
    if realized == 0 and unrealized == 0:
        return None
    return BrokerDayPnl(realized_pnl=realized, unrealized_pnl=unrealized)


def _parse_day_pnl_from_positions(raw: Any) -> BrokerDayPnl:
    realized = Decimal("0")
    unrealized = Decimal("0")
    if isinstance(raw, list):
        for row in raw:
            if not isinstance(row, dict):
                continue
            realized += _decimal_field(row, "rpnl")
            unrealized += _decimal_field(row, "urmtom", "urmtm", "urmtm")
    return BrokerDayPnl(realized_pnl=realized, unrealized_pnl=unrealized)


def _option_side_from_tsym(tsym: str) -> str:
    import re

    upper = tsym.upper()
    if upper.endswith("PE"):
        return "PE"
    if upper.endswith("CE"):
        return "CE"
    m = re.search(r"([CP])(\d+)\s*$", upper)
    if m and m.group(1) == "C":
        return "CE"
    if m:
        return "PE"
    return "CE"


def _entry_price_from_broker_position(row: dict[str, Any], netqty: int) -> Decimal:
    """Prefer today's buy/sell average (purchase side) over net average."""
    if netqty > 0:
        keys = ("daybuyavgprc", "cfbuyavgprc", "netavgprc", "dayavgprc", "upldprc")
    else:
        keys = ("daysellavgprc", "cfsellavgprc", "netavgprc", "dayavgprc", "upldprc")
    return _decimal_field(row, *keys)


def parse_broker_open_positions(raw: Any) -> list[dict[str, Any]]:
    """Open holdings from Flattrade positions API (netqty != 0)."""
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            netqty = int(float(row.get("netqty") or 0))
        except (TypeError, ValueError):
            netqty = 0
        if netqty == 0:
            continue
        tsym = str(row.get("tsym") or "").strip()
        if not tsym:
            continue
        token = str(row.get("token") or row.get("tsym") or "").strip()
        exchange = str(row.get("exch") or row.get("exchange") or "NFO").strip()
        avg = _entry_price_from_broker_position(row, netqty)
        broker_net_avg = _decimal_field(row, "netavgprc", "dayavgprc", "upldprc")
        ltp = _decimal_field(row, "lp", "ltp")
        if ltp <= 0:
            ltp = avg
        qty = abs(netqty)
        lot_size = lot_size_for_tsym(tsym)
        ur = _decimal_field(row, "urmtom", "urmtm")
        if ur == 0 and avg > 0 and ltp > 0:
            ur = (ltp - avg) * Decimal(qty)
        out.append(
            {
                "position_id": f"broker-{token}",
                "instrument_token": token,
                "exchange": exchange,
                "tsym": tsym,
                "side": _option_side_from_tsym(tsym),
                "quantity": qty,
                "lot_size": lot_size,
                "lots": max(1, qty // max(lot_size, 1)),
                "entry_price": float(avg),
                "broker_avg_price": float(broker_net_avg) if broker_net_avg > 0 else float(avg),
                "entry_price_source": "broker_display_only",
                "entry_ts": None,
                "current_ltp": float(ltp),
                "unrealized_pnl": float(ur),
                "premium_deployed": float(avg * qty),
                "setup_type": "broker",
                "mode": "live",
                "source": "broker",
            }
        )
    return out


def _decimal_field(raw: dict[str, Any], *keys: str) -> Decimal:
    for key in keys:
        val = raw.get(key)
        if val not in (None, "", "0", "0.00"):
            try:
                return Decimal(str(val))
            except Exception:
                continue
    return Decimal("0")


class _FlattradeNorenApi(NorenApi):
    def __init__(self, host: str, websocket: str) -> None:
        NorenApi.__init__(self, host=host, websocket=websocket)


class FlattradeAdapter(BrokerAdapter):
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._api = _FlattradeNorenApi(
            host=config.broker["api_base_url"],
            websocket=config.broker["websocket_url"],
        )
        self._connected = False
        self._quote_callback: Callable[[QuoteUpdate], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session_user_id: str | None = None
        self._access_token: str | None = None
        self._market_socket: FlattradeMarketSocket | None = None
        self._start_lock = asyncio.Lock()
        self._ws_tick_cache: dict[str, dict[str, Any]] = {}
        self._token_exchange: dict[str, str] = {}
        self._snapshot_cache: tuple[float, AccountLimits, BrokerDayPnl, list[dict[str, Any]]] | None = None
        self._snapshot_cache_ttl_sec = 12.0
        self._trade_book_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._trade_book_cache_ttl_sec = 30.0
        self._rest_lock = asyncio.Lock()
        self._rest_retries = 3
        self._rest_retry_base_sec = 0.35

    async def _call_api(self, fn: Callable[[], Any], *, label: str) -> Any:
        """Serialize Flattrade REST calls and retry transient failures."""
        last_err: Exception | None = None
        async with self._rest_lock:
            for attempt in range(self._rest_retries):
                try:
                    return await asyncio.to_thread(fn)
                except Exception as exc:
                    last_err = exc
                    if attempt + 1 >= self._rest_retries:
                        break
                    delay = self._rest_retry_base_sec * (2**attempt)
                    logger.warning(
                        "flattrade_api_retry",
                        label=label,
                        attempt=attempt + 1,
                        delay_sec=delay,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
        assert last_err is not None
        raise last_err

    def invalidate_trade_book_cache(self) -> None:
        self._trade_book_cache = None

    @property
    def session_user_id(self) -> str | None:
        return self._session_user_id

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def websocket_open(self) -> bool:
        return bool(self._market_socket and self._market_socket.is_open)

    async def connect(self) -> None:
        cfg = await load_flattrade_config()
        session = await ensure_session(cfg)
        user_id = session.user_id or cfg.user_id
        if not user_id:
            raise ValueError("FLATTRADE_USER_ID is required (or returned from OAuth client field)")

        self._loop = asyncio.get_running_loop()
        self._session_user_id = user_id
        self._access_token = session.access_token

        def _set_session(token: str) -> Any:
            return self._api.set_session(
                userid=user_id,
                password="",
                usertoken=token,
            )

        result = await asyncio.to_thread(_set_session, session.access_token)
        if isinstance(result, dict) and result.get("stat") == "Not_Ok":
            raise ConnectionError(result.get("emsg", "Flattrade session failed"))

        # Local expiry cache can outlive broker-side invalidation — verify live.
        limits = await asyncio.to_thread(self._api.get_limits)
        emsg = ""
        if isinstance(limits, dict) and limits.get("stat") == "Not_Ok":
            emsg = str(limits.get("emsg", ""))
        if "session" in emsg.lower() or "invalid" in emsg.lower():
            logger.warning("flattrade_session_rejected_by_broker", emsg=emsg)
            from algoflat.broker.auth import login_and_save

            session = await login_and_save(cfg, force=True)
            user_id = session.user_id or user_id
            self._session_user_id = user_id
            self._access_token = session.access_token
            result = await asyncio.to_thread(_set_session, session.access_token)
            if isinstance(result, dict) and result.get("stat") == "Not_Ok":
                raise ConnectionError(result.get("emsg", "Flattrade re-login failed"))
            limits = await asyncio.to_thread(self._api.get_limits)
            if isinstance(limits, dict) and limits.get("stat") == "Not_Ok":
                raise ConnectionError(
                    limits.get("emsg", "Flattrade session still invalid after re-login")
                )

        self._connected = True
        logger.info("flattrade_connected", user_id=user_id)
        logger.info(
            "flattrade_session_ready",
            user_id=user_id,
            token_source=session.source,
            expires_at=session.expires_at.isoformat(),
        )

    async def disconnect(self) -> None:
        await self.stop_websocket()
        self._connected = False

    async def stop_websocket(self) -> None:
        """Stop market WS only — keep REST session for candles/quotes."""
        sock = self._market_socket
        self._market_socket = None
        if sock is not None:
            await asyncio.to_thread(sock.stop)
            logger.info("flattrade_websocket_stopped")

    async def get_candles(
        self,
        exchange: str,
        token: str,
        interval: CandleInterval,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        intrv = _INTERVAL_MAP[interval]

        def _fetch() -> Any:
            return self._api.get_time_price_series(
                exchange=exchange,
                token=token,
                starttime=str(int(start.timestamp())),
                endtime=str(int(end.timestamp())),
                interval=intrv,
            )

        raw = await asyncio.to_thread(_fetch)
        if not raw:
            return []

        # Noren APIs return {"stat":"Not_Ok","emsg":"..."} on failure — never treat as bars.
        if isinstance(raw, dict):
            logger.warning(
                "flattrade_candles_error",
                exchange=exchange,
                token=token,
                interval=interval.value,
                stat=raw.get("stat"),
                emsg=raw.get("emsg") or raw.get("message"),
            )
            return []

        candles: list[Candle] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            if "ssboe" not in row:
                continue
            ts = datetime.fromtimestamp(int(row["ssboe"]), tz=timezone.utc)
            candles.append(
                Candle(
                    instrument_token=token,
                    ts=ts,
                    open=Decimal(str(row.get("into", row.get("intc", 0)))),
                    high=Decimal(str(row.get("inth", 0))),
                    low=Decimal(str(row.get("intl", 0))),
                    close=Decimal(str(row.get("intc", 0))),
                    volume=int(float(row.get("intv", 0) or 0)) or None,
                    interval=interval,
                    vwap=(
                        Decimal(str(row["intvwap"]))
                        if row.get("intvwap") not in (None, "", "0", "0.00")
                        else None
                    ),
                    oi=int(float(row["oi"])) if row.get("oi") not in (None, "") else None,
                )
            )
        # Always chronological — features/scanners use bars[-1] as latest.
        candles.sort(key=lambda c: c.ts)
        return candles

    async def get_quotes(self, exchange: str, token: str) -> dict[str, Any]:
        def _fetch() -> Any:
            return self._api.get_quotes(exchange=exchange, token=token)

        result = await asyncio.to_thread(_fetch)
        return result if isinstance(result, dict) else {}

    async def search_scrip(self, exchange: str, search_text: str) -> list[dict[str, Any]]:
        def _fetch() -> Any:
            return self._api.searchscrip(exchange=exchange, searchtext=search_text)

        result = await asyncio.to_thread(_fetch)
        if isinstance(result, dict) and result.get("stat") == "Ok":
            values = result.get("values", [])
            return values if isinstance(values, list) else []
        return []

    async def get_option_chain(
        self,
        exchange: str,
        tradingsymbol: str,
        strikeprice: float,
        count: int,
    ) -> list[dict[str, Any]]:
        def _fetch() -> Any:
            return self._api.get_option_chain(
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                strikeprice=strikeprice,
                count=count,
            )

        result = await asyncio.to_thread(_fetch)
        if isinstance(result, dict) and result.get("stat") == "Ok":
            values = result.get("values", [])
            return values if isinstance(values, list) else []
        if isinstance(result, list):
            return result
        logger.warning(
            "option_chain_failed",
            tradingsymbol=tradingsymbol,
            strikeprice=strikeprice,
            response=result,
        )
        return []

    def _register_subscription_keys(self, instruments: list[str]) -> None:
        for key in instruments:
            if "|" not in key:
                continue
            exchange, token = key.split("|", 1)
            if exchange and token:
                self._token_exchange[token] = exchange

    def _handle_feed_update(self, message: dict[str, Any]) -> None:
        if self._quote_callback is None or self._loop is None:
            return
        try:
            msg_type = str(message.get("t", ""))
            if msg_type not in ("tk", "tf", "dk", "df"):
                return

            token = str(message.get("tk") or message.get("ft") or "").strip()
            if not token:
                return

            exchange = str(message.get("e") or self._token_exchange.get(token, "")).strip()
            cache_key = f"{exchange}|{token}" if exchange else token
            prev = self._ws_tick_cache.get(cache_key, {})
            merged = dict(prev)
            for key, value in message.items():
                if value is None or value == "":
                    continue
                merged[key] = value
            self._ws_tick_cache[cache_key] = merged

            ltp = self.parse_decimal(merged.get("lp"))
            if ltp is None:
                return

            quote = QuoteUpdate(
                ts=datetime.now(tz=timezone.utc),
                exchange=exchange or str(merged.get("e", "")),
                instrument_token=token,
                tsym=merged.get("ts") if isinstance(merged.get("ts"), str) else message.get("ts"),
                ltp=ltp,
                bid=self.parse_decimal(merged.get("bp1")),
                ask=self.parse_decimal(merged.get("sp1")),
                volume=int(merged["v"]) if merged.get("v") not in (None, "") else None,
                oi=int(merged["oi"]) if merged.get("oi") not in (None, "") else None,
                broker_delta=_optional_float(merged, "delta", "del"),
                broker_gamma=_optional_float(merged, "gamma", "gam"),
                broker_theta=_optional_float(merged, "theta"),
                broker_iv=_optional_float(merged, "iv"),
                source="websocket",
            )
            self._loop.call_soon_threadsafe(self._quote_callback, quote)
        except Exception:
            logger.exception("feed_update_parse_failed", message=message)

    async def subscribe(
        self,
        instruments: list[str],
        on_quote: Any,
        on_order: Any | None = None,
    ) -> None:
        """Start Flattrade V2 market WebSocket and subscribe to option/spot tokens."""
        del on_order  # order stream not required for paper option-chain ticker
        async with self._start_lock:
            self._quote_callback = on_quote
            instruments = list(dict.fromkeys(instruments))
            self._register_subscription_keys(instruments)

            if self._market_socket and self._market_socket.is_open:
                await asyncio.to_thread(self._market_socket.subscribe, instruments)
                logger.info(
                    "flattrade_websocket_resubscribe",
                    instruments=len(instruments),
                    bfo=sum(1 for k in instruments if k.upper().startswith("BFO|")),
                )
                return

            if self._market_socket is not None:
                await asyncio.to_thread(self._market_socket.stop)
                self._market_socket = None

            # Refresh token in case session was renewed.
            cfg = await load_flattrade_config()
            session = resolve_session(cfg)
            if session is None or not session.is_valid:
                raise ConnectionError("Flattrade session invalid for WebSocket")
            user_id = session.user_id or self._session_user_id or cfg.user_id
            if not user_id:
                raise ValueError("FLATTRADE_USER_ID required for WebSocket")
            self._access_token = session.access_token
            self._session_user_id = user_id

            opened = asyncio.Event()

            def _open_cb() -> None:
                self._loop.call_soon_threadsafe(opened.set)  # type: ignore[union-attr]

            def _err_cb(error: Any) -> None:
                logger.warning("flattrade_market_socket_error", error=str(error))

            sock = FlattradeMarketSocket(
                user_id=user_id,
                access_token=session.access_token,
                actid=user_id,
                on_quote=self._handle_feed_update,
                on_open=_open_cb,
                on_error=_err_cb,
            )
            self._market_socket = sock
            sock.subscribe(instruments)
            await asyncio.to_thread(sock.start)

            try:
                await asyncio.wait_for(opened.wait(), timeout=15)
                bfo_count = sum(1 for k in instruments if k.upper().startswith("BFO|"))
                logger.info(
                    "flattrade_websocket_ready",
                    instruments=len(instruments),
                    bfo_instruments=bfo_count,
                )
            except asyncio.TimeoutError:
                if sock.auth_failed:
                    logger.warning("flattrade_websocket_auth_failed")
                else:
                    logger.warning("flattrade_websocket_open_timeout")

    def _submit_place_order(
        self,
        *,
        buy_or_sell: str,
        product_type: str,
        exchange: str,
        tradingsymbol: str,
        quantity: int,
        price_type: str,
        price: str | float,
        remarks: str,
    ) -> dict[str, Any]:
        """Place order via REST and always return parsed JSON (incl. Not_Ok emsg)."""
        routes = getattr(self._api, "_NorenApi__service_config", {}).get("routes", {})
        host = self._config.broker["api_base_url"].rstrip("/")
        path = routes.get("placeorder", "/PlaceOrder")
        url = f"{host}{path}"
        values: dict[str, Any] = {
            "ordersource": "API",
            "uid": self._session_user_id,
            "actid": self._session_user_id,
            "trantype": buy_or_sell,
            "prd": product_type,
            "exch": exchange,
            "tsym": urllib.parse.quote_plus(tradingsymbol),
            "qty": str(quantity),
            "dscqty": "0",
            "prctyp": price_type,
            "prc": str(price),
            "ret": "DAY",
            "remarks": remarks,
            "amo": "NO",
        }
        token = self._access_token or getattr(self._api, "_NorenApi__susertoken", "")
        payload = f"jData={json.dumps(values)}&jKey={token}"
        res = requests.post(url, data=payload, timeout=15)
        try:
            data = res.json()
        except Exception:
            return {"stat": "Not_Ok", "emsg": res.text[:200]}
        return data if isinstance(data, dict) else {"stat": "Not_Ok", "emsg": "invalid response"}

    async def place_order(self, request: ExecutionRequest) -> OrderUpdate:
        if not self._connected:
            raise ConnectionError("Flattrade session not connected")

        started = time.monotonic()
        buy_or_sell = "B" if request.side.upper() == "BUY" else "S"
        product_type = noren_product_code(request.product)
        # Flattrade API rejects MKT for API-sourced orders — use aggressive LMT.
        order_type = (request.order_type or "LMT").upper()
        price_type = "LMT"
        price = _limit_price_for_api(
            side=request.side,
            reference_ltp=request.reference_ltp,
            limit_price=request.limit_price,
        )
        if order_type == "MKT":
            logger.info(
                "live_order_mkt_converted_to_lmt",
                tsym=request.tsym,
                side=request.side,
                price=price,
            )

        raw = await asyncio.to_thread(
            self._submit_place_order,
            buy_or_sell=buy_or_sell,
            product_type=product_type,
            exchange=request.exchange,
            tradingsymbol=request.tsym,
            quantity=int(request.quantity),
            price_type=price_type,
            price=price,
            remarks=(request.client_order_id or "")[:20],
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        now = datetime.now(tz=timezone.utc)

        if not isinstance(raw, dict) or raw.get("stat") != "Ok":
            emsg = raw.get("emsg", "order rejected") if isinstance(raw, dict) else "invalid response"
            logger.warning(
                "live_order_rejected",
                tsym=request.tsym,
                side=request.side,
                emsg=emsg,
                raw=raw,
            )
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
                rejection_reason=str(emsg),
            )

        broker_id = str(raw.get("norenordno") or raw.get("order_id") or "")
        fill_price, filled_qty, status, reject_reason = await self._resolve_order_fill(
            broker_id=broker_id,
            request=request,
            price_type=price_type,
        )

        if status == "REJECTED":
            logger.warning(
                "live_order_fill_timeout",
                tsym=request.tsym,
                broker_order_id=broker_id,
                price_type=price_type,
                reason=reject_reason,
            )
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
                rejection_reason=reject_reason,
            )

        slippage = None
        if fill_price is not None:
            slippage = fill_price - request.reference_ltp

        logger.info(
            "live_order_filled",
            tsym=request.tsym,
            broker_order_id=broker_id,
            status=status,
            fill_price=str(fill_price) if fill_price is not None else None,
            qty=filled_qty,
        )
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

    async def _confirm_fill_from_trade_book(
        self,
        request: ExecutionRequest,
        *,
        broker_id: str | None = None,
    ) -> tuple[Decimal, int] | None:
        """Confirm fill from trade book — never use position net/day averages."""
        try:
            book = await self.fetch_trade_book(force=True)
        except Exception:
            logger.exception("trade_book_fill_confirm_failed", tsym=request.tsym)
            book = []

        if broker_id:
            matched = fill_price_for_broker_order(
                book, broker_id, side=request.side
            )
            if matched is not None:
                return matched

        # BUY only: open-lot VWAP as last resort when broker id not yet in trade book.
        if request.side.upper() == "BUY":
            lot = open_lot_from_trade_book(book, request.tsym)
            if lot is not None:
                price, qty = lot
                if qty >= int(request.quantity):
                    return price, int(request.quantity)

        return None

    async def _resolve_order_fill(
        self,
        *,
        broker_id: str,
        request: ExecutionRequest,
        price_type: str,
        max_wait_sec: float | None = None,
    ) -> tuple[Decimal | None, int, str, str | None]:
        wait_sec = max_wait_sec if max_wait_sec is not None else (10.0 if price_type == "MKT" else 12.0)
        deadline = time.monotonic() + wait_sec
        while time.monotonic() < deadline:
            row = await self._find_order_row(broker_id)
            if row is not None:
                status = str(row.get("status") or row.get("ordstatus") or "").upper()
                # Use avgprc only — bare `prc` is the limit price, not the fill.
                avgprc = row.get("avgprc")
                fill_qty = row.get("fillshares") or row.get("qty") or request.quantity
                if status in ("COMPLETE", "TRADED", "FILLED"):
                    if avgprc not in (None, "", "0", "0.00"):
                        return Decimal(str(avgprc)), int(fill_qty), "COMPLETE", None
                    tb = await self._confirm_fill_from_trade_book(
                        request, broker_id=broker_id
                    )
                    if tb is not None:
                        return tb[0], tb[1], "COMPLETE", None
                if status in ("REJECTED", "CANCELED", "CANCELLED"):
                    # Still check trade book — some rejects are status lag after a fill.
                    tb = await self._confirm_fill_from_trade_book(
                        request, broker_id=broker_id
                    )
                    if tb is not None:
                        return tb[0], tb[1], "COMPLETE", None
                    return None, 0, "REJECTED", "broker rejected or canceled order"
            await asyncio.sleep(0.4)

        confirmed = await self._confirm_fill_from_trade_book(request, broker_id=broker_id)
        if confirmed is not None:
            fill_price, filled_qty = confirmed
            logger.info(
                "live_order_fill_confirmed_via_trade_book",
                tsym=request.tsym,
                side=request.side,
                fill_price=str(fill_price),
                qty=filled_qty,
                broker_order_id=broker_id,
            )
            return fill_price, filled_qty, "COMPLETE", None

        # Trade book can lag order book — poll a few more seconds before rejecting.
        extra_deadline = time.monotonic() + 10.0
        while time.monotonic() < extra_deadline:
            confirmed = await self._confirm_fill_from_trade_book(request, broker_id=broker_id)
            if confirmed is not None:
                fill_price, filled_qty = confirmed
                logger.info(
                    "live_order_fill_confirmed_via_trade_book_delayed",
                    tsym=request.tsym,
                    side=request.side,
                    fill_price=str(fill_price),
                    qty=filled_qty,
                    broker_order_id=broker_id,
                )
                return fill_price, filled_qty, "COMPLETE", None
            await asyncio.sleep(0.5)

        reason = f"{price_type} order not confirmed filled within {wait_sec:.0f}s"
        return None, 0, "REJECTED", reason

    async def _find_order_row(self, broker_id: str) -> dict[str, Any] | None:
        if not broker_id:
            return None

        book = await self._call_api(self._api.get_order_book, label="order_book")
        if not isinstance(book, list):
            return None
        for row in book:
            if not isinstance(row, dict):
                continue
            if str(row.get("norenordno") or row.get("order_id") or "") == broker_id:
                return row
        return None

    async def get_account_limits(self) -> AccountLimits:
        if not self._connected:
            await self.connect()

        raw = await self._call_api(self._api.get_limits, label="limits")
        if not isinstance(raw, dict) or raw.get("stat") == "Not_Ok":
            emsg = raw.get("emsg", "limits unavailable") if isinstance(raw, dict) else "limits unavailable"
            raise ConnectionError(str(emsg))
        return _parse_account_limits(raw)

    async def get_day_pnl(self) -> BrokerDayPnl:
        """Account day P&L from Flattrade (all trades today, incl. squared-off rows)."""
        _, day, _ = await self.fetch_broker_snapshot()
        return day

    async def fetch_trade_book(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Today's executed fills from Flattrade (source of truth for live order history)."""
        now = time.monotonic()
        if (
            not force
            and self._trade_book_cache is not None
            and now - self._trade_book_cache[0] < self._trade_book_cache_ttl_sec
        ):
            return list(self._trade_book_cache[1])
        if not self._connected:
            await self.connect()

        raw = await self._call_api(self._api.get_trade_book, label="trade_book")
        if isinstance(raw, dict) and raw.get("stat") == "Not_Ok":
            raise ConnectionError(str(raw.get("emsg", "trade book unavailable")))
        if not isinstance(raw, list):
            rows: list[dict[str, Any]] = []
        else:
            rows = [row for row in raw if isinstance(row, dict)]
        self._trade_book_cache = (now, rows)
        return list(rows)

    async def fetch_open_positions(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Live open holdings from broker (cached with margin snapshot)."""
        await self.fetch_broker_snapshot(force=force)
        if self._snapshot_cache is not None:
            return list(self._snapshot_cache[3])
        return []

    async def fetch_broker_snapshot(
        self,
        *,
        force: bool = False,
    ) -> tuple[AccountLimits, BrokerDayPnl, list[dict[str, Any]]]:
        """Margin + day P&L + open positions in one broker round-trip (cached briefly)."""
        now = time.monotonic()
        if (
            not force
            and self._snapshot_cache is not None
            and now - self._snapshot_cache[0] < self._snapshot_cache_ttl_sec
        ):
            _, limits, day, open_pos = self._snapshot_cache
            return limits, day, list(open_pos)

        if not self._connected:
            await self.connect()

        limits_raw = await self._call_api(self._api.get_limits, label="limits")
        positions_raw = await self._call_api(self._api.get_positions, label="positions")
        if not isinstance(limits_raw, dict) or limits_raw.get("stat") == "Not_Ok":
            emsg = (
                limits_raw.get("emsg", "limits unavailable")
                if isinstance(limits_raw, dict)
                else "limits unavailable"
            )
            raise ConnectionError(str(emsg))

        limits = _parse_account_limits(limits_raw)
        day = _parse_day_pnl_from_limits(limits_raw) or _parse_day_pnl_from_positions(
            positions_raw
        )
        open_pos = parse_broker_open_positions(positions_raw)
        self._snapshot_cache = (now, limits, day, open_pos)
        return limits, day, open_pos
