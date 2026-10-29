from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from typing import Any

import structlog
import uvicorn

from algoflat.api.health import app as health_app
from algoflat.api.health import get_engine_state, set_engine_app, set_engine_state
from algoflat.broker.factory import create_live_broker
from algoflat.broker.labels import (
    broker_api_key_missing_message,
    broker_api_key_missing_title,
    broker_data_source_label,
    broker_display_name,
)
from algoflat.broker.upstox_auth import resolve_session as resolve_upstox_session
from algoflat.broker.upstox_credentials import load_upstox_config
from algoflat.broker.paper import PaperBrokerAdapter
from algoflat.broker.routed import RoutedBrokerAdapter
from algoflat.bus.event_bus import EventBus
from algoflat.config import get_config
from algoflat.contract_selector.selector import ContractSelector, ContractUniverse
from algoflat.db.connection import close_pool, get_pool, init_pool
from algoflat.db.migrate import apply_migrations
from algoflat.db.paper_account import ensure_paper_account
from algoflat.journal.writer import JournalWriter
from algoflat.logging_setup import setup_logging
from algoflat.market_data.engine import (
    FRESH_SETUP_LOOKBACK_DAYS,
    MarketDataEngine,
    needs_fresh_setup_backfill,
)
from algoflat.market_data.poller import RestQuotePoller, quote_from_rest
from algoflat.models.events import Candle, CandleInterval, QuoteUpdate, SystemEvent
from algoflat.option_data.layer import OptionDataLayer
from algoflat.trading.orchestrator import TradingOrchestrator
from algoflat.time_util import age_seconds, ensure_ist, now_ist
from algoflat.runtime.trading_mode import (
    get_execution_mode,
    init_execution_mode,
    is_live_execution,
    load_execution_mode_from_db,
    set_execution_mode,
)
from algoflat.symbols_util import (
    SENSEX_LOT_SIZE,
    apply_active_underlying,
    capital_for,
    fallback_spot,
    fut_expiry_label_from_tsym,
    index_spot_context,
    index_spot_exchange,
    is_sane_nifty_spot,
    is_ticker_only,
    list_index_tickers,
    list_mi_index_symbols,
    list_underlyings,
    price_context,
    price_exchange_for,
    resolve_all_spot_tokens,
    resolve_index_future_token,
    resolve_index_ticker_tokens,
    risk_underlying_key,
    strike_band_points,
    symbols_for,
    total_account_capital,
    uses_dual_index_spot,
    uses_futures_price,
    uses_pooled_capital,
)

logger = structlog.get_logger(__name__)


class TradingEngineApp:
    def __init__(self) -> None:
        self.config = get_config()
        self.bus = EventBus(max_size=self.config.runtime.get("event_queue_max_size", 10_000))
        self.journal = JournalWriter()
        self._tasks: list[asyncio.Task] = []
        self._universe: ContractUniverse | None = None
        self._universes: dict[str, ContractUniverse] = {}
        self._active_underlying = str(self.config.symbols.get("underlying", "SENSEX")).upper()

        live_broker = create_live_broker(self.config)
        paper = PaperBrokerAdapter(self.config, live_broker)
        init_execution_mode(self.config.env.trading_mode)
        self._live_broker = live_broker
        self.broker = RoutedBrokerAdapter(live_broker, paper)
        self.market_data = MarketDataEngine(self.config, self.broker, self.bus)
        self.option_data = OptionDataLayer(self.config)
        self.contract_selector = ContractSelector(self.config, self.broker)
        self.orchestrator = TradingOrchestrator(
            self.config,
            self.broker,
            self.journal,
            self.market_data,
            self.option_data,
        )
        self.orchestrator.positions.set_trade_open_hook(self._on_position_opened)
        self._quote_poller = RestQuotePoller(
            self.broker,
            self.market_data,
            self.option_data,
            self.config.symbols["exchange_spot"],
            self.config.symbols["spot_token"],
        )
        self._feed_mode = "offline"
        self._ws_started = False
        self._last_ws_quote_ts: datetime | None = None
        self._ws_retry_after: datetime | None = None
        self._last_universe_refresh_date: date | None = None
        self._last_log_maintenance_date: date | None = None
        self._last_ws_keys: list[str] = []
        self._index_spot_ltps: dict[str, Decimal] = {}
        self._futures_spot_ltps: dict[str, Decimal] = {}
        self._sticky_index_spots: dict[str, Decimal] = {}
        self._fut_contract_meta: dict[str, dict[str, str]] = {}
        self._token_to_underlying: dict[str, str] = {}
        self._token_spot_role: dict[str, str] = {}
        self._session_open_by_symbol: dict[str, dict[date, Decimal]] = {}
        # Broker day-change baseline (Flattrade quote field ``c`` = previous close).
        self._prev_close_by_symbol: dict[str, Decimal] = {}
        self._ticker_only_symbols = {
            str(row.get("symbol", "")).upper()
            for row in list_mi_index_symbols(self.config)
            if row.get("symbol")
        }
        self._chain_item_cache: dict[str, list[dict[str, Any]]] = {}
        self._last_chain_options_poll: datetime | None = None
        self._index_quote_tokens: set[str] = set()
        self._last_option_ws_ts: datetime | None = None
        self._watchlist_tick = asyncio.Event()
        self._position_tick = asyncio.Event()
        self._broker_open_positions: list[dict[str, Any]] = []
        self._universe_rebuild_locks: dict[str, asyncio.Lock] = {}
        self._last_spot_drift_rebuild_mono: dict[str, float] = {}
        self._spot_drift_rebuild_min_sec = 60.0
        self._broker_trades_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._broker_trades_cache_ttl_sec = 30.0

    @property
    def watchlist_tick(self) -> asyncio.Event:
        return self._watchlist_tick

    @property
    def position_tick(self) -> asyncio.Event:
        return self._position_tick

    def _position_trail_fields(self, pos: Any, ltp: Decimal) -> dict[str, Any]:
        from algoflat.position.exit_rules import _momentum_trail_floor

        exit_cfg = self.config.position_exit
        qty = int(pos.quantity)
        entry = Decimal(str(pos.entry_price))
        mfe = Decimal(str(pos.mfe))
        mae = Decimal(str(pos.mae))
        trail = _momentum_trail_floor(entry, mfe, exit_cfg)
        pnl_pts = ltp - entry
        state = self.option_data.get(pos.instrument_token)
        return {
            "mfe_points": float(mfe),
            "mfe_inr": float(mfe * qty),
            "mfe_pct": float((mfe / entry * 100) if entry > 0 else 0),
            "mae_points": float(mae),
            "mae_inr": float(mae * qty),
            "pnl_points": float(pnl_pts),
            "trail_floor": float(trail) if trail is not None else None,
            "last_tick_ts": (
                state.last_update_ts.isoformat()
                if state and state.last_update_ts is not None
                else None
            ),
        }

    def _position_row_from_pm(self, pos: Any, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        lot_size = self._lot_size_for_position(pos.instrument_token, pos.tsym)
        state = self.option_data.get(pos.instrument_token)
        ltp = state.ltp if state and state.ltp is not None else pos.entry_price
        if getattr(pos, "last_good_ltp", None) and pos.last_good_ltp > 0:
            ltp = pos.last_good_ltp
        qty = int(pos.quantity)
        row: dict[str, Any] = {
            "position_id": str(pos.position_id),
            "instrument_token": pos.instrument_token,
            "tsym": pos.tsym,
            "side": pos.option_side,
            "quantity": qty,
            "lot_size": lot_size,
            "lots": qty // max(lot_size, 1),
            "entry_price": float(pos.entry_price),
            "entry_price_source": "order_fill",
            "order_id": str(pos.order_id),
            "entry_ts": pos.entry_ts.isoformat(),
            "current_ltp": float(ltp),
            "unrealized_pnl": float((ltp - pos.entry_price) * qty),
            "premium_deployed": float(pos.premium_deployed),
            "setup_type": pos.setup_type,
            "mode": pos.mode,
            "managed": True,
        }
        row.update(self._position_trail_fields(pos, ltp))
        if extra:
            row.update(extra)
        return row

    def _on_position_opened(self, _pos: Any) -> None:
        """Ensure the new holding is on the WebSocket feed for tick trails."""
        self._position_tick.set()
        self._watchlist_tick.set()
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._ensure_holdings_on_websocket())
            if is_live_execution():
                loop.create_task(self._refresh_broker_open_positions())
        except RuntimeError:
            pass

    async def _has_api_credentials(self) -> bool:
        provider = str(self.config.broker.get("provider", "upstox")).lower()
        if provider == "flattrade":
            from algoflat.broker.credentials import load_flattrade_config

            cfg = await load_flattrade_config()
            return cfg.has_api_credentials()
        cfg = await load_upstox_config()
        return cfg.has_api_credentials()

    async def _has_valid_session(self) -> bool:
        provider = str(self.config.broker.get("provider", "upstox")).lower()
        if provider == "flattrade":
            from algoflat.broker.auth import resolve_session as resolve_ft_session
            from algoflat.broker.credentials import load_flattrade_config

            cfg = await load_flattrade_config()
            session = resolve_ft_session(cfg)
            return session is not None and session.is_valid
        cfg = await load_upstox_config()
        session = resolve_upstox_session(cfg)
        return session is not None and session.is_valid

    async def _handle_quote(self, quote: QuoteUpdate) -> None:
        ul = self._token_to_underlying.get(quote.instrument_token)
        q = quote
        if ul and quote.ltp is not None:
            role = self._token_spot_role.get(quote.instrument_token, "cash")
            if role == "futures":
                if self._is_market_open():
                    self._futures_spot_ltps[ul] = quote.ltp
                    self._capture_session_open(f"{ul}_FUT", quote.ltp)
                else:
                    known = self._futures_spot_ltps.get(ul)
                    if known is None or not self._spots_diverge(quote.ltp, known, 0.015):
                        self._futures_spot_ltps[ul] = quote.ltp
                    else:
                        # After-hours WS/REST junk — keep coalesced futures LTP.
                        q = quote.model_copy(update={"ltp": known})
                self._watchlist_tick.set()
            else:
                if self._is_market_open():
                    await self._apply_index_spot(ul, quote.ltp)
                else:
                    known = self._index_spot_ltps.get(ul) or self._sticky_index_spots.get(
                        ul
                    )
                    if known is None or not self._spots_diverge(quote.ltp, known, 0.015):
                        await self._apply_index_spot(ul, quote.ltp)
                    else:
                        logger.warning(
                            "index_spot_rejected_after_hours",
                            symbol=ul,
                            rest=str(quote.ltp),
                            kept=str(known),
                        )
        await self.market_data.on_quote(q)
        self.option_data.update_from_quote(quote)
        await self.orchestrator.on_quote(quote)
        if quote.instrument_token and self.orchestrator.positions.has_open_for_token(
            quote.instrument_token
        ):
            self._position_tick.set()
        if quote.source == "websocket":
            self._feed_mode = "websocket"
            self._last_ws_quote_ts = quote.ts
            if quote.instrument_token in self._index_quote_tokens:
                self._watchlist_tick.set()
            else:
                self._last_option_ws_ts = quote.ts
                self._watchlist_tick.set()
        elif quote.source == "rest":
            if self._ws_stale():
                self._feed_mode = "rest"
            self._watchlist_tick.set()
        set_engine_state(
            {
                "spot_ltp": str(self.market_data.spot_ltp) if self.market_data.spot_ltp else None,
                "last_quote_ts": ensure_ist(quote.ts).isoformat(),
                "feed_mode": self._feed_mode,
                "ws_open": bool(getattr(self.broker, "websocket_open", False)),
            }
        )

    def _quote_callback(self, quote: QuoteUpdate) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._handle_quote(quote))
        except RuntimeError:
            loop = asyncio.get_event_loop()
            asyncio.run_coroutine_threadsafe(self._handle_quote(quote), loop)

    async def _stop_subscription(self) -> None:
        stop = getattr(self.broker, "stop_websocket", None)
        if callable(stop):
            await stop()
        else:
            await self.broker.disconnect()
        self._ws_started = False
        self._last_ws_keys = []
        if self._feed_mode == "websocket":
            self._feed_mode = "offline"

    def _ws_subscription_keys(self) -> list[str]:
        """Universe ATM band + every open holding (trail exits need tick LTP).

        After ATM retarget / expiry roll, a held strike can fall outside the
        band and drop off WS — then MFE only updates on the ~2s REST poll and
        peaks are missed. Always keep holdings subscribed.
        """
        from algoflat.broker.base import BrokerAdapter

        keys: list[str] = []
        seen: set[str] = set()

        def _add(key: str) -> None:
            if key and key not in seen:
                seen.add(key)
                keys.append(key)

        if self._universe:
            for k in self._universe.subscription_keys:
                _add(k)

        for uni in self._universes.values():
            for k in uni.subscription_keys:
                _add(k)

        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            fut_token = str(row.get("spot_token", "")).strip()
            if sym and fut_token:
                exchange = price_exchange_for(row)
                _add(BrokerAdapter.format_instrument(exchange, fut_token))
            if uses_dual_index_spot(row):
                cash_token = str(row.get("index_spot_token", "")).strip()
                if sym and cash_token:
                    idx_exchange = index_spot_exchange(row)
                    _add(BrokerAdapter.format_instrument(idx_exchange, cash_token))

        for row in list_index_tickers(self.config):
            sym = str(row.get("symbol", "")).upper()
            token = str(row.get("spot_token", "")).strip()
            if not sym or not token:
                continue
            exchange = str(row.get("exchange", "NSE"))
            _add(BrokerAdapter.format_instrument(exchange, token))

        for pos in self.orchestrator.positions.open_positions:
            pos_exchange = str(self.config.symbols.get("exchange_options", "BFO"))
            for uni in self._universes.values():
                match = next(
                    (i for i in uni.instruments if i.token == pos.instrument_token),
                    None,
                )
                if match:
                    pos_exchange = match.exchange
                    break
            _add(BrokerAdapter.format_instrument(pos_exchange, pos.instrument_token))

        for bp in self._broker_open_positions:
            token = str(bp.get("instrument_token") or "").strip()
            if not token:
                continue
            exch = str(bp.get("exchange") or self.config.symbols.get("exchange_options", "BFO"))
            _add(BrokerAdapter.format_instrument(exch, token))

        return list(keys)

    async def _start_subscription(self) -> None:
        if not self._universe:
            return
        now = datetime.now(tz=timezone.utc)
        if self._ws_retry_after and now < self._ws_retry_after:
            return
        keys = self._ws_subscription_keys()
        if not keys:
            return
        try:
            await self.broker.subscribe(keys, self._quote_callback)
            self._ws_started = True
            self._last_ws_keys = list(keys)
            provider = str(self.config.broker.get("provider", "upstox")).lower()
            if getattr(self.broker, "websocket_open", False):
                self._feed_mode = "websocket"
                self._ws_retry_after = None
            elif provider == "upstox" or getattr(self.broker, "uses_rest_market_feed", False):
                self._feed_mode = "rest"
                self._ws_retry_after = now + timedelta(seconds=30)
            else:
                self._ws_retry_after = now + timedelta(seconds=60)
            logger.info(
                "websocket_subscription_ready",
                keys=len(keys),
                open_holdings=self.orchestrator.positions.open_count,
            )
        except Exception:
            logger.exception("websocket_subscribe_failed")
            self._ws_started = False
            self._ws_retry_after = now + timedelta(seconds=60)

    async def _ensure_holdings_on_websocket(self) -> None:
        """Resubscribe if open holdings are missing from the current WS set."""
        if not self._ws_started or not self._is_market_open():
            return
        has_holdings = (
            self.orchestrator.positions.open_count > 0 or len(self._broker_open_positions) > 0
        )
        if not has_holdings:
            return
        needed = self._ws_subscription_keys()
        if not needed:
            return
        if needed == self._last_ws_keys:
            return
        try:
            await self.broker.subscribe(needed, self._quote_callback)
            added = [k for k in needed if k not in self._last_ws_keys]
            self._last_ws_keys = list(needed)
            logger.info(
                "websocket_holdings_resubscribed",
                keys=len(needed),
                added=added,
                open_holdings=self.orchestrator.positions.open_count,
            )
        except Exception:
            logger.exception("websocket_holdings_resubscribe_failed")

    def _is_market_open(self) -> bool:
        from algoflat.market_session import is_market_open

        return is_market_open(self.config.market_session)

    def _register_index_spot_tokens(self) -> None:
        """Map futures + cash index tokens → underlying for live header/chain quotes."""
        self._token_to_underlying.clear()
        self._token_spot_role.clear()
        self._index_quote_tokens = set()
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            fut_token = str(row.get("spot_token", "")).strip()
            if sym and fut_token:
                self._token_to_underlying[fut_token] = sym
                self._token_spot_role[fut_token] = "futures"
                self._index_quote_tokens.add(fut_token)
            if uses_dual_index_spot(row):
                cash_token = str(row.get("index_spot_token", "")).strip()
                if sym and cash_token:
                    self._token_to_underlying[cash_token] = sym
                    self._token_spot_role[cash_token] = "cash"
                    self._index_quote_tokens.add(cash_token)
            elif sym and fut_token and not uses_dual_index_spot(row):
                self._token_spot_role[fut_token] = "cash"
        for row in list_index_tickers(self.config):
            sym = str(row.get("symbol", "")).upper()
            token = str(row.get("spot_token", "")).strip()
            if sym and token:
                self._token_to_underlying[token] = sym
                self._token_spot_role[token] = "cash"
                self._index_quote_tokens.add(token)

    def _register_sector_tokens(self) -> None:
        """Map sector index tokens for REST polling — not subscribed on WS."""
        from algoflat.symbols_util import list_sector_indices

        for row in list_sector_indices(self.config):
            sym = str(row.get("symbol", "")).upper()
            token = str(row.get("spot_token", "")).strip()
            if sym and token:
                self._token_to_underlying[token] = sym
                self._token_spot_role[token] = "sector"

    @staticmethod
    def _spots_diverge(a: Decimal, b: Decimal, pct: float = 0.008) -> bool:
        if a <= 0 or b <= 0:
            return True
        return abs(a - b) / a > Decimal(str(pct))

    def _trading_spot(self, sym: str, uni: ContractUniverse | None = None) -> Decimal | None:
        """Futures LTP for candles, VWAP, strategy, and NIFTY FUT header."""
        ul = sym.upper()

        fut = self._futures_spot_ltps.get(ul)
        if fut is not None and not is_sane_nifty_spot(fut):
            fut = None

        md: Decimal | None = None
        if ul == self._active_underlying and self.market_data.spot_ltp is not None:
            px = self.market_data.spot_ltp
            if is_sane_nifty_spot(px):
                md = px

        # After-hours Flattrade `lp` can be junk; prefer coalesced futures cache
        # whenever market-data LTP diverges from it.
        if fut is not None and (md is None or self._spots_diverge(md, fut, 0.015)):
            return fut
        if md is not None:
            return md
        return fut

    def _cash_index_spot(self, sym: str, uni: ContractUniverse | None = None) -> Decimal | None:
        """NSE cash index for ATM strike and option chain."""
        ul = sym.upper()
        cfg = symbols_for(self.config, ul)

        live = self._index_spot_ltps.get(ul)
        if live is not None and is_sane_nifty_spot(live):
            return live

        sticky = self._sticky_index_spots.get(ul)
        if sticky is not None and is_sane_nifty_spot(sticky):
            return sticky

        if uses_dual_index_spot(cfg):
            return None

        uni = uni or self._universes.get(ul)
        if uni and uni.spot is not None and is_sane_nifty_spot(uni.spot):
            return uni.spot

        return None

    async def _last_cash_index_close(self, sym: str) -> Decimal | None:
        cfg = symbols_for(self.config, sym.upper())
        if not uses_dual_index_spot(cfg):
            return None
        exchange, token = index_spot_context(cfg)
        if not token:
            return None
        now = datetime.now(tz=timezone.utc)
        try:
            bars = await self.broker.get_candles(
                exchange,
                token,
                CandleInterval.M1,
                now - timedelta(days=10),
                now,
            )
            if bars:
                return bars[-1].close
        except Exception:
            logger.exception("cash_index_candle_fallback_failed", symbol=sym)
        db_close = await self._last_close_from_db(sym, token_kind="cash")
        if db_close is not None:
            return db_close
        return None

    def _header_spot(self, sym: str, uni: ContractUniverse | None = None) -> Decimal | None:
        """Top-bar LTP — cash index for NIFTY, futures for NIFTY_FUT."""
        ul = sym.upper()
        if ul in self._ticker_only_symbols:
            live = self._index_spot_ltps.get(ul)
            if live is not None:
                return live
            sticky = self._sticky_index_spots.get(ul)
            if sticky is not None:
                return sticky
            return None
        if ul.endswith("_FUT"):
            base = ul.removesuffix("_FUT")
            return self._trading_spot(base, self._universes.get(base))
        return self._cash_index_spot(ul, uni)

    def _atm_spot(self, sym: str, uni: ContractUniverse | None = None) -> Decimal | None:
        """Spot for ATM / option chain — cash index when dual-chart ATM is enabled."""
        from algoflat.symbols_util import symbols_for, uses_dual_index_spot

        cfg = symbols_for(self.config, sym)
        prefer_cash = bool(
            cfg.get("atm_from_cash_index", self.config.symbols.get("atm_from_cash_index", True))
        )
        if prefer_cash and uses_dual_index_spot(cfg):
            cash = self._cash_index_spot(sym, uni)
            if cash is not None:
                return cash
        return self._trading_spot(sym, uni)

    def _spot_for_symbol(self, sym: str, uni: ContractUniverse | None) -> Decimal | None:
        return self._trading_spot(sym, uni)

    def _sync_engine_spot_state(self, spot: Decimal | None) -> None:
        if spot is None or self._active_underlying is None:
            return
        set_engine_state({"spot_ltp": str(spot)})

    def _retarget_universe_spot(self, sym: str, spot: Decimal) -> None:
        uni = self._universes.get(sym.upper())
        if uni is None or not uni.instruments:
            return
        cfg = symbols_for(self.config, sym)
        step = float(cfg.get("strike_step", 50))
        step_d = Decimal(str(step))
        old_atm = uni.atm_strike
        updated = self.contract_selector.retarget_atm(uni, spot, strike_step=step)
        if old_atm is not None and abs(updated.atm_strike - old_atm) >= step_d:
            self._chain_item_cache.pop(sym.upper(), None)
        self._universes[sym.upper()] = updated
        if sym.upper() == self._active_underlying:
            self._universe = updated
            self.orchestrator.set_universe(updated)
        if sym.upper() == self._active_underlying:
            self._universe = updated
            self.orchestrator.set_universe(updated)

    def _strike_coverage_ok(
        self,
        uni: ContractUniverse,
        atm: Decimal,
        sym: str,
    ) -> bool:
        """True when universe has ATM±atm_strike_steps on the strike grid."""
        if not uni.instruments:
            return False
        cfg = symbols_for(self.config, sym)
        steps = int(cfg.get("atm_strike_steps", 5))
        step = Decimal(str(cfg.get("strike_step", 50)))
        strikes = {i.strike for i in uni.instruments}
        for offset in range(-steps, steps + 1):
            target = atm + (step * offset)
            if not any(abs(s - target) <= Decimal("0.01") for s in strikes):
                return False
        return True

    async def _rebuild_universe_for_symbol(
        self,
        ul: str,
        spot: Decimal,
        *,
        reason: str,
    ) -> bool:
        primary = self._active_underlying
        apply_active_underlying(self.config, ul)
        try:
            rebuilt = await self.contract_selector.build_universe(spot)
            rebuilt.spot = spot
            self._universes[ul] = rebuilt
            if ul == self._active_underlying:
                self._universe = rebuilt
                self.orchestrator.set_universe(rebuilt)
            if rebuilt.instruments:
                pool = get_pool()
                await self.contract_selector.persist_instruments(pool, rebuilt)
                px_exchange, px_token = price_context(symbols_for(self.config, ul))
                self._quote_poller.set_spot(px_exchange, px_token)
                await self._quote_poller.poll_universe(rebuilt)
            logger.info(
                "universe_rebuilt",
                symbol=ul,
                reason=reason,
                spot=str(spot),
                atm=str(rebuilt.atm_strike),
                instruments=len(rebuilt.instruments),
            )
            if self._is_market_open() and self._ws_started:
                await self._ensure_holdings_on_websocket()
            return bool(rebuilt.instruments)
        except Exception:
            logger.exception("universe_rebuild_failed", symbol=ul, reason=reason)
            return False
        finally:
            apply_active_underlying(self.config, primary)

    def _sync_orchestrator_index_quotes(self) -> None:
        for sym, ltp in self._index_spot_ltps.items():
            open_px = self._session_open_for_symbol(sym)
            self.orchestrator.update_index_quote(sym, ltp, session_open=open_px)

    async def _apply_index_spot(self, sym: str, spot: Decimal) -> None:
        ul = sym.upper()
        if ul not in self._ticker_only_symbols and not is_sane_nifty_spot(spot):
            logger.warning("index_spot_rejected_out_of_band", symbol=ul, spot=str(spot))
            return
        self._index_spot_ltps[ul] = spot
        self._sticky_index_spots[ul] = spot
        session_open = self._session_open_for_symbol(ul)
        self.orchestrator.update_index_quote(ul, spot, session_open=session_open)
        if ul not in self._ticker_only_symbols:
            self.market_data.set_cash_index_ltp(spot)
        self._capture_session_open(ul, spot)
        self._watchlist_tick.set()
        if ul in self._ticker_only_symbols:
            return
        uni = self._universes.get(ul)
        atm_ref = self._atm_spot(ul, uni) or spot
        if uni is not None:
            uni.spot = atm_ref
        if ul == self._active_underlying:
            self._sync_engine_spot_state(self._trading_spot(ul) or atm_ref)
        if uni is None:
            return
        cfg = symbols_for(self.config, ul)
        step = float(cfg.get("strike_step", 50))
        step_d = Decimal(str(step))
        new_atm = self.contract_selector.atm_strike_for_spot(atm_ref, strike_step=step)
        atm_crossed = (
            uni.atm_strike is not None and abs(new_atm - uni.atm_strike) >= step_d
        )
        if not uni.instruments or atm_crossed:
            await self._maybe_rebuild_universe_spot_drift(ul, atm_ref)
        else:
            self._retarget_universe_spot(ul, atm_ref)

    async def _maybe_rebuild_universe_spot_drift(self, ul: str, spot: Decimal) -> None:
        lock = self._universe_rebuild_locks.setdefault(ul, asyncio.Lock())
        if lock.locked():
            self._retarget_universe_spot(ul, spot)
            return
        now = time.monotonic()
        last = self._last_spot_drift_rebuild_mono.get(ul, 0.0)
        uni = self._universes.get(ul)
        if uni and uni.instruments and now - last < self._spot_drift_rebuild_min_sec:
            self._retarget_universe_spot(ul, spot)
            return
        async with lock:
            now = time.monotonic()
            last = self._last_spot_drift_rebuild_mono.get(ul, 0.0)
            uni = self._universes.get(ul)
            if uni and uni.instruments and now - last < self._spot_drift_rebuild_min_sec:
                self._retarget_universe_spot(ul, spot)
                return
            ok = await self._rebuild_universe_for_symbol(ul, spot, reason="spot_drift")
            if ok:
                self._last_spot_drift_rebuild_mono[ul] = time.monotonic()
            else:
                self._retarget_universe_spot(ul, spot)

    async def _ensure_header_quote_tokens(self) -> None:
        """Resolve futures / index tokens if missing (e.g. after config reload)."""
        try:
            await resolve_all_spot_tokens(self.config, self.broker)
            await resolve_index_ticker_tokens(self.config, self.broker)
            self._register_index_spot_tokens()
            for row in list_underlyings(self.config):
                sym = str(row.get("symbol", "")).upper()
                if sym:
                    self._sync_fut_contract_meta(sym)
        except Exception:
            logger.exception("header_token_resolution_failed")

    async def _last_close_from_db(
        self,
        sym: str,
        *,
        token_kind: str = "auto",
    ) -> Decimal | None:
        """Last 1m candle close — REST fallback when broker quote is empty after hours."""
        ul = sym.upper()
        merged = symbols_for(self.config, ul)
        if is_ticker_only(self.config, ul):
            token = str(merged.get("spot_token", "")).strip()
        elif token_kind == "cash" or (
            token_kind == "auto" and uses_dual_index_spot(merged)
        ):
            _, token = index_spot_context(merged)
            if not token:
                _, token = price_context(merged)
        else:
            _, token = price_context(merged)
        if not token:
            return None
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT close FROM candles_1m
                    WHERE instrument_token = $1
                      AND ts >= NOW() - INTERVAL '10 days'
                    ORDER BY ts DESC
                    LIMIT 1
                    """,
                    token,
                )
            if row and row["close"] is not None:
                return Decimal(str(row["close"]))
        except Exception:
            logger.exception("header_spot_db_fallback_failed", symbol=ul)
        return None

    async def _hydrate_header_spots_from_db(self) -> None:
        """Fill missing header LTPs from last stored candle when live quotes are absent."""
        symbols: list[str] = []
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            if sym:
                symbols.append(sym)
        for row in list_index_tickers(self.config):
            sym = str(row.get("symbol", "")).upper()
            if sym:
                symbols.append(sym)
        for sym in symbols:
            if sym in self._ticker_only_symbols:
                if self._header_spot(sym) is not None:
                    continue
            else:
                cfg = symbols_for(self.config, sym)
                if uses_dual_index_spot(cfg):
                    if self._cash_index_spot(sym) is None:
                        db_cash = await self._last_close_from_db(sym, token_kind="cash")
                        if db_cash is not None:
                            await self._apply_index_spot(sym, db_cash)
                    if self._trading_spot(sym) is None:
                        db_fut = await self._last_close_from_db(sym, token_kind="futures")
                        if db_fut is not None:
                            self._futures_spot_ltps[sym] = db_fut
                    if self._cash_index_spot(sym) is not None or self._trading_spot(sym) is not None:
                        continue
                elif self._trading_spot(sym) is not None:
                    continue
            db_close = await self._last_close_from_db(sym)
            if db_close is not None:
                await self._apply_index_spot(sym, db_close)

    async def _reference_spot_close(
        self,
        sym: str,
        *,
        token_kind: str = "auto",
        prev_close: Decimal | None = None,
    ) -> Decimal | None:
        """Best last-session close for sanitizing after-hours / bad REST LTPs."""
        refs: list[Decimal] = []
        if token_kind == "cash":
            hist = await self._last_cash_index_close(sym)
            if hist is not None and is_sane_nifty_spot(hist):
                refs.append(hist)
        db_close = await self._last_close_from_db(sym, token_kind=token_kind)
        if db_close is not None and is_sane_nifty_spot(db_close):
            refs.append(db_close)
        if (
            token_kind in ("futures", "auto")
            and not refs
        ):
            # Futures candle history from broker when DB is empty.
            merged = symbols_for(self.config, sym.upper())
            exchange, token = price_context(merged)
            if token:
                now = datetime.now(tz=timezone.utc)
                try:
                    bars = await self.broker.get_candles(
                        exchange,
                        token,
                        CandleInterval.M1,
                        now - timedelta(days=10),
                        now,
                    )
                    if bars and is_sane_nifty_spot(bars[-1].close):
                        refs.append(bars[-1].close)
                except Exception:
                    logger.exception("futures_candle_fallback_failed", symbol=sym)
        if prev_close is not None and is_sane_nifty_spot(prev_close):
            refs.append(prev_close)
        return refs[0] if refs else None

    async def _coalesce_spot_with_db(
        self,
        sym: str,
        quote_ltp: Decimal,
        *,
        token: str,
        exchange: str,
        token_kind: str = "auto",
        prev_close: Decimal | None = None,
    ) -> Decimal:
        """Prefer last candle/prev-close when REST `lp` diverges (common after hours)."""
        ref = await self._reference_spot_close(
            sym, token_kind=token_kind, prev_close=prev_close
        )
        if ref is None or not is_sane_nifty_spot(ref):
            return quote_ltp
        # After hours Flattrade index/fut `lp` is often fantasy; trust candles.
        # In session, still reject >2.5% spikes (wrong scrip / bad tick).
        threshold = 0.012 if not self._is_market_open() else 0.025
        if self._spots_diverge(quote_ltp, ref, threshold):
            logger.warning(
                "rest_spot_diverges_from_db",
                symbol=sym,
                exchange=exchange,
                token=token,
                rest=str(quote_ltp),
                db_close=str(ref),
                market_open=self._is_market_open(),
            )
            return ref
        return quote_ltp

    async def _refresh_sector_spots_rest(self) -> bool:
        """Throttled REST poll for sector indices — avoids Upstox 429 on startup."""
        from algoflat.symbols_util import list_sector_indices, resolve_sector_index_tokens

        await resolve_sector_index_tokens(self.config, self.broker)
        self._register_sector_tokens()
        updated = False
        for row in list_sector_indices(self.config):
            sym = str(row.get("symbol", "")).upper()
            token = str(row.get("spot_token", "")).strip()
            if not sym or not token:
                continue
            exchange = str(row.get("exchange", "NSE"))
            try:
                raw = await self.broker.get_quotes(exchange, token)
                quote = quote_from_rest(exchange, token, raw)
                self._lock_quote_day_open(sym, raw)
                if quote is not None and quote.ltp is not None:
                    await self._apply_index_spot(sym, quote.ltp)
                    updated = True
            except Exception:
                logger.exception("sector_index_rest_failed", symbol=sym)
            await asyncio.sleep(0.35)
        return updated

    async def _refresh_index_ticker_spots(self) -> bool:
        """REST poll for display-only tickers (e.g. INDIAVIX) — runs even when market is closed."""
        updated = False
        for row in list_index_tickers(self.config):
            sym = str(row.get("symbol", "")).upper()
            token = str(row.get("spot_token", "")).strip()
            if not sym or not token:
                continue
            exchange = str(row.get("exchange", "NSE"))
            try:
                raw = await self.broker.get_quotes(exchange, token)
                quote = quote_from_rest(exchange, token, raw)
                self._lock_quote_day_open(sym, raw)
                if quote is None or quote.ltp is None:
                    db_close = await self._last_close_from_db(sym)
                    if db_close is not None:
                        await self._apply_index_spot(sym, db_close)
                        updated = True
                    continue
                self._token_to_underlying[token] = sym
                await self._apply_index_spot(sym, quote.ltp)
                updated = True
            except Exception:
                logger.exception("index_ticker_rest_failed", symbol=sym)
        return updated

    async def _refresh_futures_spot_rest(self, sym: str, row: dict[str, Any]) -> bool:
        token = str(row.get("spot_token", "")).strip()
        if not token:
            return False
        exchange = price_exchange_for(row)
        try:
            raw = await self.broker.get_quotes(exchange, token)
            quote = quote_from_rest(exchange, token, raw)
            if quote is None or quote.ltp is None:
                db_close = await self._last_close_from_db(sym, token_kind="futures")
                if db_close is not None:
                    self._futures_spot_ltps[sym.upper()] = db_close
                    return True
                return False
            self._token_to_underlying[token] = sym.upper()
            from algoflat.broker.base import BrokerAdapter

            prev_close = BrokerAdapter.parse_decimal(
                raw.get("c") if isinstance(raw, dict) else None
            )
            spot = await self._coalesce_spot_with_db(
                sym,
                quote.ltp,
                token=token,
                exchange=exchange,
                token_kind="futures",
                prev_close=prev_close,
            )
            self._futures_spot_ltps[sym.upper()] = spot
            if sym.upper() == self._active_underlying:
                md = self.market_data.spot_ltp
                needs_sync = md is None or self._spots_diverge(md, spot, 0.015)
                if needs_sync or self.market_data._spot_quote_trusted(spot):
                    await self.market_data.on_quote(
                        quote.model_copy(update={"ltp": spot})
                    )
                    # Force-correct when MD still holds after-hours junk.
                    if (
                        self.market_data.spot_ltp is not None
                        and self._spots_diverge(self.market_data.spot_ltp, spot, 0.015)
                    ):
                        self.market_data.set_spot_ltp(
                            spot, ts=quote.ts, update_forming=True
                        )
            return True
        except Exception:
            logger.exception("futures_spot_rest_failed", symbol=sym)
            return False

    async def _refresh_cash_index_spot_rest(self, sym: str, row: dict[str, Any]) -> bool:
        if not uses_dual_index_spot(row):
            return False
        merged = symbols_for(self.config, sym.upper())
        token = str(merged.get("index_spot_token", "")).strip()
        if not token:
            return False
        exchange = index_spot_exchange(merged)
        try:
            raw = await self.broker.get_quotes(exchange, token)
            quote = quote_from_rest(exchange, token, raw)
            if quote is None or quote.ltp is None:
                db_close = await self._last_cash_index_close(sym)
                if db_close is not None:
                    await self._apply_index_spot(sym, db_close)
                    logger.info(
                        "cash_index_spot_from_history",
                        symbol=sym,
                        token=token,
                        spot=str(db_close),
                    )
                    return True
                logger.warning(
                    "cash_index_spot_unavailable",
                    symbol=sym,
                    token=token,
                    exchange=exchange,
                    stat=raw.get("stat") if isinstance(raw, dict) else None,
                    keys=sorted(raw.keys()) if isinstance(raw, dict) else None,
                )
                return False
            self._token_to_underlying[token] = sym.upper()
            self._token_spot_role[token] = "cash"
            from algoflat.broker.base import BrokerAdapter

            prev_close = BrokerAdapter.parse_decimal(
                raw.get("c") if isinstance(raw, dict) else None
            )
            spot = await self._coalesce_spot_with_db(
                sym,
                quote.ltp,
                token=token,
                exchange=exchange,
                token_kind="cash",
                prev_close=prev_close,
            )
            await self._apply_index_spot(sym, spot)
            logger.info(
                "cash_index_spot_updated",
                symbol=sym,
                token=token,
                spot=str(spot),
                rest_lp=str(quote.ltp),
            )
            return True
        except Exception:
            logger.exception("cash_index_spot_rest_failed", symbol=sym)
            return False

    async def _refresh_index_spots_rest(self) -> None:
        """Poll futures + cash index LTPs for live top-bar and option chain."""
        await self._ensure_header_quote_tokens()
        updated = False
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            if uses_dual_index_spot(row):
                if await self._refresh_cash_index_spot_rest(sym, row):
                    updated = True
                if await self._refresh_futures_spot_rest(sym, row):
                    updated = True
                continue
            token = str(row.get("spot_token", "")).strip()
            if not token:
                continue
            exchange = price_exchange_for(row)
            try:
                raw = await self.broker.get_quotes(exchange, token)
                quote = quote_from_rest(exchange, token, raw)
                self._lock_quote_day_open(sym, raw)
                if quote is None or quote.ltp is None:
                    db_close = await self._last_close_from_db(sym)
                    if db_close is not None:
                        await self._apply_index_spot(sym, db_close)
                        updated = True
                    continue
                self._token_to_underlying[token] = sym
                spot = await self._coalesce_spot_with_db(
                    sym, quote.ltp, token=token, exchange=exchange
                )
                await self._apply_index_spot(sym, spot)
                updated = True
            except Exception:
                logger.exception("index_spot_rest_failed", symbol=sym)
        if await self._refresh_index_ticker_spots():
            updated = True
        if not updated:
            await self._hydrate_header_spots_from_db()
        self._watchlist_tick.set()

    def _sync_fut_contract_meta(self, sym: str) -> None:
        ul = sym.upper()
        cfg = symbols_for(self.config, ul)
        token = str(cfg.get("spot_token", "")).strip()
        tsym = str(cfg.get("fut_tsym", "")).strip()
        if not token or not tsym:
            self._fut_contract_meta.pop(ul, None)
            return
        label = fut_expiry_label_from_tsym(tsym) or tsym
        self._fut_contract_meta[ul] = {
            "token": token,
            "tsym": tsym,
            "expiry_label": label,
        }

    def _fut_header_snapshot_row(self, sym: str) -> dict[str, Any] | None:
        ul = sym.upper()
        if not uses_futures_price(symbols_for(self.config, ul)):
            return None
        if ul not in self._fut_contract_meta:
            self._sync_fut_contract_meta(ul)
        meta = self._fut_contract_meta.get(ul)
        if not meta:
            return None
        spot_val = self._trading_spot(ul)
        if spot_val is None and ul == self._active_underlying:
            spot_val = self.market_data.spot_ltp
        session_open = self._session_open_for_symbol(f"{ul}_FUT")
        prev_close = self._prev_close_for_symbol(f"{ul}_FUT") or self._prev_close_for_symbol(ul)
        change, change_pct = self._change_vs_baseline(spot_val, prev_close)
        return {
            "underlying": f"{ul}_FUT",
            "display_name": "NIFTY FUT",
            "card_type": "fut",
            "expiry_label": meta.get("expiry_label"),
            "fut_tsym": meta.get("tsym"),
            "spot_ltp": float(spot_val) if spot_val is not None else None,
            "session_open": float(session_open) if session_open is not None else None,
            "prev_close": float(prev_close) if prev_close is not None else None,
            "change": change,
            "change_pct": change_pct,
            "atm_strike": None,
            "expiry_symbol": meta.get("expiry_label"),
            "instrument_count": 0,
            "strike_band_points": 0,
            "strike_step": 0,
            "atm_strike_steps": 0,
            "items": [],
            "strike_count": 0,
            "ticker_only": True,
        }

    def _build_header_commodity_rows(
        self,
        *,
        default_step: float,
        default_band: float,
        default_atm_steps: int,
        include_chain: bool,
    ) -> list[dict[str, Any]]:
        commodities: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            seen.add(sym)
            uni = self._universes.get(sym)
            commodities.append(
                self._commodity_snapshot_row(
                    sym,
                    uni,
                    default_step=default_step,
                    default_band=default_band,
                    default_atm_steps=default_atm_steps,
                )
                if include_chain
                else self._commodity_summary_row(sym, uni)
            )
            fut_row = self._fut_header_snapshot_row(sym)
            if fut_row:
                commodities.append(fut_row)
        for row in list_index_tickers(self.config):
            sym = str(row.get("symbol", "")).upper()
            if not sym or sym in seen:
                continue
            commodities.append(self._ticker_snapshot_row(row))
        return commodities

    def _commodity_summary_row(
        self, sym: str, uni: ContractUniverse | None
    ) -> dict[str, Any]:
        """Lightweight commodity row for market summary (no option chain)."""
        full = self._commodity_snapshot_row(
            sym,
            uni,
            default_step=float(symbols_for(self.config, sym).get("strike_step", 50)),
            default_band=float(
                strike_band_points(symbols_for(self.config, sym))
            ),
            default_atm_steps=int(
                symbols_for(self.config, sym).get("atm_strike_steps", 5)
            ),
        )
        return {
            k: v
            for k, v in full.items()
            if k
            not in (
                "items",
                "strike_count",
                "instrument_count",
                "strike_band_points",
                "strike_step",
                "atm_strike_steps",
            )
        }

    def _ws_stale(self, max_age_sec: int = 8) -> bool:
        last = self._last_ws_quote_ts
        if last is None:
            return True
        age = age_seconds(last)
        return age is None or age > max_age_sec

    async def _poll_rest_quotes_once(self) -> None:
        updated = 0
        primary = self._active_underlying
        for sym, uni in self._universes.items():
            apply_active_underlying(self.config, sym)
            sym_cfg = symbols_for(self.config, sym)
            px_exchange, px_token = price_context(sym_cfg)
            self._quote_poller.set_spot(px_exchange, px_token)
            updated += await self._quote_poller.poll_universe(uni)
        apply_active_underlying(self.config, primary)
        if self._universe:
            sym_cfg = symbols_for(self.config, primary)
            px_exchange, px_token = price_context(sym_cfg)
            self._quote_poller.set_spot(px_exchange, px_token)
        if updated:
            if self._ws_stale():
                self._feed_mode = "rest"
            set_engine_state(
                {
                    "spot_ltp": str(self.market_data.spot_ltp)
                    if self.market_data.spot_ltp
                    else None,
                    "last_quote_ts": datetime.now(tz=timezone.utc).isoformat(),
                    "feed_mode": self._feed_mode,
                    "ws_open": bool(getattr(self.broker, "websocket_open", False)),
                }
            )
        await self._refresh_index_ticker_spots()
        await self._evaluate_open_exits_from_option_data(source="rest")
        self._watchlist_tick.set()

    async def _poll_universe_options(
        self,
        sym: str,
        uni: ContractUniverse | None,
        *,
        max_concurrency: int = 10,
    ) -> int:
        if uni is None or not uni.instruments:
            return 0
        sem = asyncio.Semaphore(max(1, max_concurrency))

        async def _one(inst) -> bool:
            async with sem:
                try:
                    raw = await self.broker.get_quotes(inst.exchange, inst.token)
                    quote = quote_from_rest(inst.exchange, inst.token, raw)
                    if quote is None:
                        return False
                    self.option_data.update_from_quote(quote)
                    return True
                except Exception:
                    logger.exception(
                        "option_quote_poll_failed",
                        symbol=sym,
                        token=inst.token,
                        exchange=inst.exchange,
                    )
                    return False

        results = await asyncio.gather(
            *[_one(inst) for inst in uni.instruments],
            return_exceptions=True,
        )
        return sum(1 for r in results if r is True)

    async def _poll_all_universe_option_quotes(self) -> None:
        """REST refresh for option chain when WebSocket is thin."""
        primary = self._active_underlying
        updated = 0
        for sym, uni in self._universes.items():
            apply_active_underlying(self.config, sym)
            updated += await self._poll_universe_options(sym, uni, max_concurrency=12)
        apply_active_underlying(self.config, primary)
        if updated:
            logger.debug("chain_options_rest_polled", updated=updated)

    def _option_ws_stale(self, max_age_sec: float = 3.0) -> bool:
        last = self._last_option_ws_ts
        if last is None:
            return True
        age = age_seconds(last)
        return age is None or age > max_age_sec

    async def _poll_stale_chain_options(self) -> int:
        """REST refresh only for option tokens missing a recent WebSocket tick."""
        if not self._is_market_open():
            return 0
        stale_sec = float(self.config.runtime.get("option_ws_stale_seconds", 1.5))
        max_batch = int(self.config.runtime.get("option_stale_poll_batch", 12))
        now = datetime.now(tz=timezone.utc)
        candidates: list[tuple[float, str, Any]] = []

        for sym, uni in self._universes.items():
            if uni is None or not uni.instruments:
                continue
            for inst in uni.instruments:
                state = self.option_data.get(inst.token)
                age = stale_sec + 1.0
                if state is not None and state.last_update_ts is not None:
                    age = (now - state.last_update_ts).total_seconds()
                if state is None or state.ltp is None or age >= stale_sec:
                    candidates.append((age, sym, inst))

        if not candidates:
            return 0

        candidates.sort(key=lambda row: row[0], reverse=True)
        batch = candidates[:max_batch]
        updated = 0
        for _, sym, inst in batch:
            try:
                raw = await self.broker.get_quotes(inst.exchange, inst.token)
                quote = quote_from_rest(inst.exchange, inst.token, raw)
                if quote is None:
                    continue
                self.option_data.update_from_quote(quote)
                updated += 1
            except Exception:
                logger.exception(
                    "stale_option_quote_poll_failed",
                    symbol=sym,
                    token=inst.token,
                )
        if updated:
            self._watchlist_tick.set()
            logger.debug("stale_chain_options_polled", updated=updated)
        return updated

    async def _maybe_poll_chain_options(self) -> None:
        if not self._is_market_open():
            return
        now = datetime.now(tz=timezone.utc)
        interval = int(self.config.runtime.get("chain_rest_poll_seconds", 5))
        if self._last_chain_options_poll is not None:
            if (now - self._last_chain_options_poll).total_seconds() < interval:
                return
        self._last_chain_options_poll = now
        await self._poll_all_universe_option_quotes()

    async def _poll_open_position_quotes(self) -> None:
        """Refresh LTP for open holdings — prioritise tokens with stale/missing WS ticks."""
        holdings: dict[str, tuple[str, str]] = {}
        default_exchange = self.config.symbols.get("exchange_options", "BFO")

        for pos in self.orchestrator.positions.open_positions:
            if is_live_execution() and pos.mode != "live":
                continue
            pos_exchange = default_exchange
            for uni in self._universes.values():
                match = next(
                    (i for i in uni.instruments if i.token == pos.instrument_token),
                    None,
                )
                if match:
                    pos_exchange = match.exchange
                    break
            holdings[pos.instrument_token] = (pos_exchange, pos.tsym)

        for bp in self._broker_open_positions:
            token = str(bp.get("instrument_token") or "").strip()
            if token and token not in holdings:
                holdings[token] = (
                    str(bp.get("exchange") or default_exchange),
                    str(bp.get("tsym") or ""),
                )

        if not holdings:
            return

        stale_sec = float(
            self.config.runtime.get("holding_ws_stale_seconds", 3)
        )
        stale_tokens = set(
            self.orchestrator.positions.stale_holding_tokens(stale_sec)
        )
        tokens = sorted(
            holdings.keys(),
            key=lambda t: (0 if t in stale_tokens else 1, t),
        )

        for token in tokens:
            pos_exchange, tsym = holdings[token]
            try:
                raw = await self.broker.get_quotes(pos_exchange, token)
            except Exception:
                logger.exception("open_position_quote_failed", token=token)
                continue
            quote = quote_from_rest(pos_exchange, token, raw)
            if quote is None or quote.ltp is None:
                continue
            if tsym:
                quote.tsym = tsym
            self.option_data.update_from_quote(quote)
            await self.orchestrator.on_quote(quote)

        if self.orchestrator.positions.open_count > 0:
            await self._refresh_open_option_charts()
            await self._evaluate_open_exits_from_option_data(source="holding_poll")
            await self._flatten_stale_holdings()
            await self._flatten_if_daily_loss()

    async def _evaluate_open_exits_from_option_data(self, *, source: str) -> None:
        """Push latest option LTPs into PositionManager for trail / reverse exits."""
        for pos in list(self.orchestrator.positions.open_positions):
            state = self.option_data.get(pos.instrument_token)
            if state is None or state.ltp is None:
                continue
            quote = QuoteUpdate(
                ts=datetime.now(tz=timezone.utc),
                exchange=self.config.symbols.get("exchange_options", "BFO"),
                instrument_token=pos.instrument_token,
                tsym=pos.tsym,
                ltp=state.ltp,
                bid=state.bid,
                ask=state.ask,
                source=source,
            )
            await self.orchestrator.on_quote(quote)

    async def _refresh_open_option_charts(self) -> None:
        """Keep 1m bars for open CE/PE so the holding chart can invalidate the trade."""
        now = datetime.now(tz=timezone.utc)
        last_map: dict[str, datetime] = getattr(self, "_option_chart_refresh_at", {})
        self._option_chart_refresh_at = last_map
        exchange = self.config.symbols.get("exchange_options", "BFO")
        min_bars = int(self.config.runtime.get("min_option_m1_bars_for_chart", 5))
        for pos in list(self.orchestrator.positions.open_positions):
            last = last_map.get(pos.instrument_token)
            if last is not None and (now - last).total_seconds() < 60:
                continue
            try:
                await self.orchestrator._market_data.ensure_option_token_m1(
                    pos.instrument_token,
                    exchange=exchange,
                    min_bars=min_bars,
                    force=True,
                )
            except Exception:
                logger.exception("open_option_chart_refresh_failed", token=pos.instrument_token)
            last_map[pos.instrument_token] = now

    async def _flatten_stale_holdings(self) -> None:
        limit = float(self.config.runtime.get("holding_stale_flatten_seconds", 20))
        if limit <= 0:
            return
        stale = self.orchestrator.positions.stale_holding_tokens(limit)
        for token in stale:
            logger.warning("holding_stale_flatten", token=token, max_age_seconds=limit)
            try:
                await self.orchestrator.positions.manual_exit_by_token(
                    token,
                    exit_reason="stale_quote",
                )
            except Exception:
                logger.exception("holding_stale_flatten_failed", token=token)

    async def _flatten_if_daily_loss(self) -> None:
        if self.orchestrator.positions.open_count <= 0:
            return
        if not bool(self.config.risk.get("block_entries_on_daily_loss", False)):
            return
        snap = await self.orchestrator.risk.ensure_daily_state()
        limit = self.orchestrator.risk._max_daily_loss_limit(snap.starting_capital)
        if limit <= 0:
            return
        unreal = self.orchestrator.positions.unrealized_pnl(self.option_data)
        total = snap.realized_pnl + unreal
        if total <= -limit:
            logger.warning(
                "daily_loss_flatten",
                pnl=str(total),
                limit=str(limit),
                starting_capital=str(snap.starting_capital),
            )
            await self.orchestrator.positions.flatten("max_daily_loss")

    async def _run_rest_poll_loop(self) -> None:
        while True:
            try:
                if is_live_execution():
                    await self._refresh_broker_open_positions()
                # Keep open holdings on WS so trail MFE sees ticks, not only 2s REST.
                await self._ensure_holdings_on_websocket()
                await self._refresh_index_spots_rest()
                # Full chain when WS is down; always keep open holdings ticking for trails.
                if not self._is_market_open() or self._ws_stale():
                    await self._poll_rest_quotes_once()
                else:
                    stale_updated = await self._poll_stale_chain_options()
                    if stale_updated == 0 and self._option_ws_stale(
                        float(self.config.runtime.get("option_ws_stale_seconds", 1.5)) * 2
                    ):
                        await self._maybe_poll_chain_options()
                    if self.orchestrator.positions.open_count > 0 or (
                        is_live_execution() and self._broker_open_positions
                    ):
                        await self._poll_open_position_quotes()
                    self._watchlist_tick.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("rest_poll_failed")
            interval = int(self.config.runtime.get("rest_quote_poll_interval_seconds", 30))
            if not self._is_market_open():
                interval = int(
                    self.config.runtime.get("rest_index_poll_interval_seconds", 10)
                )
            if self._is_market_open():
                interval = int(
                    self.config.runtime.get("rest_quote_poll_interval_market_seconds", 2)
                )
                if getattr(self.broker, "websocket_quotes_flowing", False):
                    interval = int(
                        self.config.runtime.get(
                            "rest_quote_poll_interval_ws_backup_seconds", 15
                        )
                    )
                if self.orchestrator.positions.open_count > 0 or (
                    is_live_execution() and self._broker_open_positions
                ):
                    stale = self.orchestrator.positions.stale_holding_tokens(
                        float(self.config.runtime.get("holding_ws_stale_seconds", 3))
                    )
                    if stale:
                        interval = int(
                            self.config.runtime.get("holding_rest_poll_seconds", 1)
                        )
            await asyncio.sleep(interval)

    async def _run_live_reconcile_loop(self) -> None:
        """Periodic Flattrade holdings + margin sync for live mode."""
        interval = int(self.config.runtime.get("reconciliation_interval_seconds", 60))
        while True:
            try:
                await self._run_live_broker_reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("live_reconcile_failed")
            await asyncio.sleep(max(10, interval))

    async def _resolve_atm_spot(self, sym: str, row: dict[str, Any] | None = None) -> Decimal:
        """Futures (or single spot) price for ATM strike and option universe."""
        ul = sym.upper()
        cfg_row = row or symbols_for(self.config, ul)
        fut_spot = await self._refresh_spot_from_rest()
        if fut_spot is not None and is_sane_nifty_spot(fut_spot):
            self._futures_spot_ltps[ul] = fut_spot

        if uses_dual_index_spot(cfg_row):
            await self._refresh_cash_index_spot_rest(ul, cfg_row)

        atm_spot: Decimal | None = self._trading_spot(ul)
        if atm_spot is None:
            atm_spot = fut_spot
        if atm_spot is None and self.market_data.spot_ltp is not None:
            atm_spot = self.market_data.spot_ltp
            if is_sane_nifty_spot(atm_spot):
                self._futures_spot_ltps[ul] = atm_spot
        if atm_spot is None:
            db_close = await self._last_close_from_db(ul, token_kind="auto")
            if db_close is not None:
                atm_spot = db_close
                self._futures_spot_ltps[ul] = db_close
        if atm_spot is None or not is_sane_nifty_spot(atm_spot):
            return fallback_spot(self.config, ul)
        return atm_spot

    async def _resolve_spot(self) -> Decimal:
        """Trading spot for universe build — futures when configured."""
        trading = self._trading_spot(self._active_underlying)
        if trading is not None:
            return trading
        spot = await self._refresh_spot_from_rest()
        if spot is not None:
            return spot
        if self.market_data.spot_ltp is not None:
            return self.market_data.spot_ltp
        uni = self._universes.get(self._active_underlying)
        if uni and uni.spot is not None:
            return uni.spot
        db_close = await self._last_close_from_db(self._active_underlying)
        if db_close is not None:
            return db_close
        return fallback_spot(self.config, self._active_underlying)

    @staticmethod
    def _expected_instrument_count(sym_cfg: dict[str, Any]) -> int:
        steps = int(sym_cfg.get("atm_strike_steps", 5))
        return (steps * 2 + 1) * 2

    def _any_universe_undersized(self) -> bool:
        for sym, uni in self._universes.items():
            cfg = symbols_for(self.config, sym)
            if len(uni.instruments) < self._expected_instrument_count(cfg):
                return True
        return False

    async def _rebuild_all_universes(self, *, reason: str) -> None:
        """Rebuild option universes for every configured underlying."""
        primary = self._active_underlying
        pool = get_pool()
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            merged = apply_active_underlying(self.config, sym)
            px_exchange, px_token = price_context(merged)
            self.market_data.set_spot_context(exchange=px_exchange, spot_token=px_token)
            self._quote_poller.set_spot(px_exchange, px_token)
            try:
                await self.market_data.backfill_today()
            except Exception:
                logger.exception("candle_backfill_failed", symbol=sym)
            ul_spot = await self._resolve_atm_spot(sym, row)
            universe = await self.contract_selector.build_universe(ul_spot)
            universe.spot = ul_spot
            self._universes[sym] = universe
            if universe.instruments:
                await self.contract_selector.persist_instruments(pool, universe)
                await self._quote_poller.poll_universe(universe)

        apply_active_underlying(self.config, primary)
        self._universe = self._universes.get(primary)
        if self._universe is not None:
            self.orchestrator.set_universe(self._universe)
        if self._ws_started and self._is_market_open():
            await self._stop_subscription()
            await self._start_subscription()
        await self._poll_rest_quotes_once()
        logger.info("all_universes_rebuilt", reason=reason, symbols=list(self._universes.keys()))

    async def _switch_trading_underlying(self, symbol: str) -> None:
        sym = symbol.upper()
        merged = apply_active_underlying(self.config, sym)
        self._active_underlying = sym
        exchange, spot_token = price_context(merged)
        self.market_data.set_spot_context(exchange=exchange, spot_token=spot_token)
        self._quote_poller.set_spot(exchange, spot_token)
        refreshed = await self._refresh_spot_from_rest()
        if refreshed is not None:
            await self._apply_index_spot(sym, refreshed)

        universe = self._universes.get(sym)
        if universe is None or not universe.instruments:
            spot = await self._resolve_spot()
            universe = await self.contract_selector.build_universe(spot)
            universe.spot = spot
            self._universes[sym] = universe
            if universe.instruments:
                pool = get_pool()
                await self.contract_selector.persist_instruments(pool, universe)

        self._universe = universe
        self.orchestrator.set_universe(universe)
        if universe.instruments:
            self._universes[sym] = universe

    async def _refresh_universe(self, *, reason: str) -> bool:
        """Rebuild weekly option chain (ATM band) for current / next expiry."""
        from zoneinfo import ZoneInfo

        spot = await self._resolve_spot()

        previous = self._universe.expiry_symbol if self._universe else None
        had_instruments = bool(self._universe and self._universe.instruments)

        universe = await self.contract_selector.build_universe(spot)
        self._universe = universe
        self._universes[self._active_underlying] = universe
        self.orchestrator.set_universe(universe)
        self._last_universe_refresh_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()

        if universe.instruments:
            pool = get_pool()
            await self.contract_selector.persist_instruments(pool, universe)
            # Resubscribe WS if symbols changed or we previously had none.
            if (
                previous != universe.expiry_symbol
                or not had_instruments
                or self._ws_started
            ):
                if self._ws_started:
                    await self._stop_subscription()
                if self._is_market_open():
                    await self._start_subscription()
            await self._poll_rest_quotes_once()

        set_engine_state(
            {
                "instrument_count": len(universe.instruments),
                "atm_strike": str(universe.atm_strike),
                "expiry_symbol": universe.expiry_symbol,
                "spot_ltp": str(self.market_data.spot_ltp or spot),
            }
        )
        logger.info(
            "universe_refreshed",
            reason=reason,
            expiry=universe.expiry_symbol,
            instruments=len(universe.instruments),
            previous_expiry=previous,
            spot=str(spot),
        )
        if universe.instruments and previous != universe.expiry_symbol:
            await self.journal.write_notification(
                "system",
                "info",
                "Weekly expiry rolled",
                f"Now trading {universe.expiry_symbol} · {len(universe.instruments)} contracts",
            )
        return bool(universe.instruments)

    async def _maybe_roll_futures(self) -> None:
        """Re-resolve current-month FUT when contract rolls."""
        for row in list_underlyings(self.config):
            if not uses_futures_price(row):
                continue
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            exchange = str(
                row.get("exchange_futures")
                or row.get("exchange_options")
                or row.get("exchange_spot", "NFO")
            )
            result = await resolve_index_future_token(self.broker, sym, exchange)
            if not result:
                continue
            token, tsym = result
            old = str(row.get("spot_token", ""))
            if token == old:
                continue
            row["spot_token"] = token
            row["fut_tsym"] = tsym
            row["exchange_spot"] = exchange
            self._sync_fut_contract_meta(sym)
            logger.info(
                "futures_rolled",
                symbol=sym,
                old_token=old,
                new_token=token,
                tsym=tsym,
            )
            if sym == self._active_underlying:
                apply_active_underlying(self.config, sym)
                self.market_data.set_spot_context(exchange=exchange, spot_token=token)
                self._quote_poller.set_spot(exchange, token)
                await self.market_data.backfill_today()
                if self._ws_started:
                    await self._stop_subscription()
                    await self._start_subscription()

    async def _maybe_roll_universe(self) -> None:
        """Auto-roll to next weekly after expiry day / empty chain / new IST day."""
        from zoneinfo import ZoneInfo

        from algoflat.contract_selector.expiry import parse_expiry_tag

        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        need = False
        reason = "periodic"

        if self._universe is None or not self._universe.instruments:
            need = True
            reason = "empty_universe"
        elif self._last_universe_refresh_date != today:
            need = True
            reason = "new_trading_day"
        else:
            exp = parse_expiry_tag(self._universe.expiry_symbol or "")
            if exp is not None and exp < today:
                need = True
                reason = "expiry_passed"
            elif exp == today and not self._is_market_open() and datetime.now(
                ZoneInfo("Asia/Kolkata")
            ).time().hour >= 15:
                # After close on expiry day, move to next weekly early.
                need = True
                reason = "expiry_day_closed"

        if need:
            await self._refresh_universe(reason=reason)
            if reason == "new_trading_day":
                await self._maybe_run_log_retention(trigger="new_trading_day")

    async def _maybe_run_log_retention(self, *, trigger: str = "scheduled") -> None:
        """Archive yesterday's logs to disk and purge from Postgres (once per IST day)."""
        from zoneinfo import ZoneInfo

        cfg = self.config.runtime.get("log_retention") or {}
        if not cfg.get("enabled", True):
            return

        ist = ZoneInfo("Asia/Kolkata")
        now = datetime.now(ist)
        today = now.date()
        if self._last_log_maintenance_date == today:
            return

        run_at_raw = str(cfg.get("run_at_ist", "15:35"))
        try:
            hour_s, minute_s = run_at_raw.split(":", 1)
            run_at = datetime.combine(today, dt_time(int(hour_s), int(minute_s)), tzinfo=ist)
        except ValueError:
            run_at = datetime.combine(today, dt_time(15, 35), tzinfo=ist)

        if trigger != "new_trading_day" and now < run_at:
            return

        from algoflat.journal.retention import run_log_retention

        try:
            stats = await run_log_retention(cfg)
            self._last_log_maintenance_date = today
            logger.info("daily_log_retention_done", trigger=trigger, stats=stats)
        except Exception:
            logger.exception("daily_log_retention_failed", trigger=trigger)

    async def _run_daily_log_maintenance_loop(self) -> None:
        """Check after market close whether today's log purge has run."""
        while True:
            try:
                await self._maybe_run_log_retention(trigger="scheduled")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("log_maintenance_loop_failed")
            await asyncio.sleep(300)

    async def _run_market_feed_loop(self) -> None:
        """Keep broker market feed alive during market hours (WS or REST poll)."""
        while True:
            try:
                await self._maybe_roll_futures()
                await self._maybe_roll_universe()
                if not self._is_market_open() or not self._universe:
                    if not self._is_market_open() and self._ws_started:
                        logger.info("market_closed_stopping_websocket")
                        await self._stop_subscription()
                elif getattr(self.broker, "uses_rest_market_feed", False):
                    ws_open = bool(getattr(self.broker, "websocket_open", False))
                    ws_stale = bool(getattr(self.broker, "websocket_stale", True))
                    if not getattr(self.broker, "rest_poll_active", False) or (ws_open and ws_stale):
                        logger.info(
                            "market_open_starting_rest_poll",
                            ws_open=ws_open,
                            ws_stale=ws_stale,
                        )
                        await self._start_subscription()
                    elif ws_open and not ws_stale:
                        self._feed_mode = "websocket"
                elif not self._universe.instruments:
                    await self._refresh_universe(reason="market_open_retry")
                elif not self._ws_started:
                    logger.info("market_open_starting_websocket")
                    await self._start_subscription()
                elif (
                    getattr(self.broker, "websocket_open", False)
                    and getattr(self.broker, "websocket_stale", False)
                ):
                    # UpstoxWebSocketFeed reconnects internally — avoid stop/start storms.
                    self._feed_mode = "rest"
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("market_feed_loop_failed")
            await asyncio.sleep(15)

    async def _refresh_spot_from_rest(self) -> Decimal | None:
        sym = self._active_underlying
        merged = symbols_for(self.config, sym)
        exchange, token = price_context(merged)
        try:
            raw = await self.broker.get_quotes(exchange, token)
        except ConnectionError:
            logger.warning("spot_rest_quote_failed", symbol=sym, exchange=exchange, token=token)
            return None
        quote = quote_from_rest(exchange, token, raw)
        if quote and quote.ltp is not None:
            spot = await self._coalesce_spot_with_db(sym, quote.ltp, token=token, exchange=exchange)
            trusted = self.market_data._spot_quote_trusted(spot)
            if trusted:
                await self.market_data.on_quote(
                    quote.model_copy(update={"ltp": spot})
                )
                return spot
            ref = self.market_data._reference_spot_from_candles()
            if ref is not None:
                return ref
            return spot
        return None

    async def _setup_market_data(self) -> None:
        for field, available in self.option_data.probe_greek_availability().items():
            await self.journal.log_field_availability(field, "websocket", available)

        try:
            await resolve_all_spot_tokens(self.config, self.broker)
            await resolve_index_ticker_tokens(self.config, self.broker)
        except Exception:
            logger.exception("spot_token_resolution_failed")
        self._register_index_spot_tokens()
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            if sym:
                self._sync_fut_contract_meta(sym)

        primary = str(self.config.symbols.get("underlying", "SENSEX")).upper()
        merged = apply_active_underlying(self.config, primary)
        px_exchange, px_token = price_context(merged)
        self.market_data.set_spot_context(exchange=px_exchange, spot_token=px_token)
        self._quote_poller.set_spot(px_exchange, px_token)

        await self._maybe_auto_fetch_fresh_setup_data()

        await self.market_data.backfill_today()
        for interval_candles in self.market_data._candles.values():
            await self.journal.write_candles(interval_candles)

        pool = get_pool()
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            merged = apply_active_underlying(self.config, sym)
            px_exchange, px_token = price_context(merged)
            self.market_data.set_spot_context(exchange=px_exchange, spot_token=px_token)
            self._quote_poller.set_spot(px_exchange, px_token)
            try:
                await self.market_data.backfill_today()
            except Exception:
                logger.exception("candle_backfill_failed", symbol=sym)
            self.market_data.seed_spot_from_candles()
            ul_spot = await self._resolve_atm_spot(sym, row)
            universe = await self.contract_selector.build_universe(ul_spot)
            universe.spot = ul_spot
            self._universes[sym] = universe
            await self.contract_selector.persist_instruments(pool, universe)
            await self._quote_poller.poll_universe(universe)

        apply_active_underlying(self.config, primary)
        self._universe = self._universes.get(primary)
        if self._universe is None:
            spot = await self._resolve_atm_spot(primary)
            self._universe = await self.contract_selector.build_universe(spot)
            self._universes[primary] = self._universe
        self.orchestrator.set_universe(self._universe)
        self.orchestrator.set_underlying_switcher(self._switch_trading_underlying)
        self.orchestrator.set_sector_refresher(self._refresh_sector_spots_rest)
        from zoneinfo import ZoneInfo

        self._last_universe_refresh_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        await self._poll_rest_quotes_once()
        await self._refresh_index_spots_rest()
        await self._refresh_sector_spots_rest()
        self._sync_orchestrator_index_quotes()

        set_engine_state(
            {
                "status": "running",
                "instrument_count": len(self._universe.instruments),
                "spot_ltp": str(self.market_data.spot_ltp or self._universe.spot),
                "atm_strike": str(self._universe.atm_strike),
                "expiry_symbol": self._universe.expiry_symbol,
                "feed_mode": self._feed_mode,
            }
        )

        await self.orchestrator.initialize()
        scan_task = asyncio.create_task(self.orchestrator.run_periodic_scan())
        poll_task = asyncio.create_task(self._run_rest_poll_loop())
        feed_task = asyncio.create_task(self._run_market_feed_loop())
        reconcile_task = asyncio.create_task(self._run_live_reconcile_loop())
        maintenance_task = asyncio.create_task(self._run_daily_log_maintenance_loop())
        self._tasks.extend([scan_task, poll_task, feed_task, reconcile_task, maintenance_task])

        logger.info("trading_engine_ready", instruments=len(self._universe.instruments))

    async def _ensure_session(self) -> bool:
        provider = str(self.config.broker.get("provider", "upstox")).lower()
        if not await self._has_valid_session():
            if provider == "flattrade":
                from algoflat.broker.auth import login_and_save
                from algoflat.broker.credentials import load_flattrade_config

                cfg = await load_flattrade_config()
                if cfg.password and cfg.totp_secret:
                    try:
                        await login_and_save(cfg)
                        logger.info("flattrade_auto_login_on_startup")
                    except Exception:
                        logger.exception("flattrade_auto_login_failed")

            if not await self._has_valid_session():
                broker_label = "Upstox" if provider == "upstox" else "Flattrade"
                logger.warning(
                    "broker_login_required",
                    provider=provider,
                    hint=f"Run scripts/{provider}_login.py or set access token in Settings",
                )
                await self.journal.write_notification(
                    "system",
                    "warning",
                    f"{broker_label} login required",
                    f"Open Settings → Broker and complete OAuth, or run scripts/{provider}_login.py",
                )
                set_engine_state({"status": "standby"})
                return False
        return True

    async def reauthenticate(self, *, force: bool = True) -> dict[str, Any]:
        provider = str(self.config.broker.get("provider", "upstox")).lower()
        if provider == "flattrade":
            from algoflat.broker.auth import login_and_save
            from algoflat.broker.credentials import (
                invalidate_flattrade_config_cache,
                load_flattrade_config,
            )

            invalidate_flattrade_config_cache()
            cfg = await load_flattrade_config(force=True)
            if not cfg.has_api_credentials():
                raise RuntimeError(
                    "Flattrade API credentials are not configured. "
                    "Open Settings → Broker and save your API key and secret."
                )
            if not cfg.has_auto_login():
                raise RuntimeError(
                    "Stored credentials are missing password or TOTP for auto login. "
                    "Open Settings, save password + TOTP secret, then Re-authenticate."
                )
            session = await login_and_save(cfg, force=force)
            user_msg = f"User {session.user_id} · valid until {session.expires_at.astimezone().strftime('%d %b %H:%M %Z')}"
            title = "Flattrade session refreshed"
        else:
            from algoflat.broker.upstox_auth import ensure_session
            from algoflat.broker.upstox_credentials import (
                invalidate_upstox_config_cache,
                load_upstox_config,
            )

            invalidate_upstox_config_cache()
            cfg = await load_upstox_config(force=True)
            if not cfg.has_api_credentials() and not cfg.has_access_token():
                raise RuntimeError(
                    "Upstox credentials are not configured. "
                    "Open Settings → Broker or run scripts/upstox_login.py"
                )
            session = await ensure_session(cfg, force=force)
            user_msg = f"Token valid until {session.expires_at.astimezone().strftime('%d %b %H:%M %Z')}"
            title = "Upstox session refreshed"

        was_subscribed = self._ws_started
        if was_subscribed:
            await self._stop_subscription()

        await self.broker.connect()
        set_engine_state({"broker_connected": True, "status": "running"})

        if self._universe is None:
            await self._setup_market_data()
        else:
            await self._refresh_index_spots_rest()
            if self._is_market_open():
                await self._start_subscription()

        await self.journal.write_notification(
            "system",
            "info",
            title,
            user_msg,
        )

        return {
            "ok": True,
            "user_id": session.user_id,
            "expires_at": session.expires_at.isoformat(),
            "valid": session.is_valid,
            "broker_connected": True,
        }

    def _build_chain_items(
        self,
        universe: ContractUniverse,
        *,
        step: float,
        band: float,
        spot: Decimal | None,
        with_greeks: bool = True,
        atm_steps: int = 5,
    ) -> list[dict[str, Any]]:
        from algoflat.contract_selector.expiry import parse_expiry_tag
        from algoflat.contract_selector.strike_picker import effective_quote_band_steps
        from algoflat.option_data.greeks import compute_greeks

        items: list[dict[str, Any]] = []
        if not universe.instruments or universe.atm_strike is None:
            return items

        atm_f = float(universe.atm_strike)
        rate = float(self.config.data_availability.get("risk_free_rate", 0.065))
        expiry_date = None
        if universe.expiry_symbol:
            expiry_date = parse_expiry_tag(universe.expiry_symbol)

        spot_f = float(spot) if spot is not None else float(universe.spot or 0)
        use_greeks = with_greeks and spot is not None and spot_f > 0

        atm_d = Decimal(str(atm_f))
        step_d = Decimal(str(step))
        max_span = step_d * Decimal(str(int(atm_steps)))
        tradable_steps = effective_quote_band_steps(
            self.config.strategy.get("strike_selection") or {}
        )
        tradable_span = step_d * Decimal(str(tradable_steps))

        for inst in sorted(
            universe.instruments,
            key=lambda i: (i.strike, 0 if i.option_type == "CE" else 1),
        ):
            strike_d = inst.strike
            diff = abs(strike_d - atm_d)
            if step_d > 0 and (diff % step_d) != 0:
                continue
            if diff > max_span:
                continue
            strike_f = float(strike_d)
            state = self.option_data.get(inst.token)
            lot_size = int(inst.lot_size)
            ltp = float(state.ltp) if state and state.ltp is not None else None
            greeks = None
            if use_greeks and ltp is not None:
                greeks = compute_greeks(
                    spot=spot_f,
                    strike=strike_f,
                    premium=ltp,
                    option_type=inst.option_type,
                    expiry=expiry_date,
                    rate=rate,
                )
            items.append(
                {
                    "exchange": inst.exchange,
                    "token": inst.token,
                    "tsym": inst.tsym,
                    "strike": strike_f,
                    "option_type": inst.option_type,
                    "is_atm": abs(strike_f - atm_f) < 1e-9,
                    "tradable": diff <= tradable_span + Decimal("1e-9"),
                    "lot_size": lot_size,
                    "ltp": ltp,
                    "bid": float(state.bid) if state and state.bid is not None else None,
                    "ask": float(state.ask) if state and state.ask is not None else None,
                    "volume": state.volume if state else None,
                    "oi": state.oi if state else None,
                    "iv": round(greeks.iv * 100, 2)
                    if greeks and greeks.iv is not None
                    else None,
                    "delta": round(greeks.delta, 4)
                    if greeks and greeks.delta is not None
                    else None,
                    "gamma": round(greeks.gamma, 6)
                    if greeks and greeks.gamma is not None
                    else None,
                    "theta": round(greeks.theta, 2)
                    if greeks and greeks.theta is not None
                    else None,
                    "vega": round(greeks.vega, 2)
                    if greeks and greeks.vega is not None
                    else None,
                    "greeks_source": "black_scholes" if use_greeks else None,
                    "last_update_ts": state.last_update_ts.isoformat()
                    if state and state.last_update_ts
                    else None,
                }
            )
        return items

    def _hydrate_chain_quotes(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge latest option_data LTP/OI into chain rows."""
        if not items:
            return items
        out: list[dict[str, Any]] = []
        for row in items:
            token = str(row.get("token", ""))
            state = self.option_data.get(token)
            if state is None:
                out.append(row)
                continue
            next_row = dict(row)
            if state.ltp is not None:
                next_row["ltp"] = float(state.ltp)
            if state.bid is not None:
                next_row["bid"] = float(state.bid)
            if state.ask is not None:
                next_row["ask"] = float(state.ask)
            if state.volume is not None:
                next_row["volume"] = state.volume
            if state.oi is not None:
                next_row["oi"] = state.oi
            if state.last_update_ts is not None:
                next_row["last_update_ts"] = state.last_update_ts.isoformat()
            out.append(next_row)
        return out

    @staticmethod
    def _chain_strike_count(items: list[dict[str, Any]]) -> int:
        return len({i.get("strike") for i in items if i.get("strike") is not None})

    def _merge_chain_items(
        self,
        base: list[dict[str, Any]],
        fresh: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Keep full strike structure; overlay latest quotes from a partial rebuild."""
        if not base:
            return list(fresh)
        if not fresh:
            return list(base)
        by_token: dict[str, dict[str, Any]] = {
            str(row["token"]): dict(row) for row in base if row.get("token")
        }
        for row in fresh:
            token = str(row.get("token", ""))
            if not token:
                continue
            if token in by_token:
                merged = dict(by_token[token])
                for key, value in row.items():
                    if value is not None:
                        merged[key] = value
                by_token[token] = merged
            else:
                by_token[token] = dict(row)
        return sorted(
            by_token.values(),
            key=lambda i: (float(i.get("strike", 0)), 0 if i.get("option_type") == "CE" else 1),
        )

    def _stabilize_chain_items(
        self,
        sym: str,
        built: list[dict[str, Any]],
        *,
        atm_steps: int,
    ) -> list[dict[str, Any]]:
        """Never replace a full ATM±band chain with a thin partial snapshot."""
        min_strikes = int(atm_steps) * 2 + 1
        cached = self._chain_item_cache.get(sym, [])
        built_strikes = self._chain_strike_count(built)
        cached_strikes = self._chain_strike_count(cached)

        if built_strikes >= min_strikes:
            self._chain_item_cache[sym] = built
            return built

        if built_strikes >= cached_strikes and built:
            self._chain_item_cache[sym] = built
            return built

        if cached:
            merged = self._merge_chain_items(cached, built)
            if self._chain_strike_count(merged) >= cached_strikes:
                self._chain_item_cache[sym] = merged
            return merged

        if built:
            self._chain_item_cache[sym] = built
        return built

    def _lot_size_for_position(self, instrument_token: str, tsym: str) -> int:
        for uni in self._universes.values():
            match = next((i for i in uni.instruments if i.token == instrument_token), None)
            if match:
                return int(match.lot_size)
        if self._universe:
            match = next(
                (i for i in self._universe.instruments if i.token == instrument_token),
                None,
            )
            if match:
                return int(match.lot_size)
        return SENSEX_LOT_SIZE

    def _capture_session_open(self, sym: str, spot: Decimal) -> None:
        # Weekend / after-hours REST ticks are unreliable — don't lock session open to them.
        if not self._is_market_open():
            return
        from zoneinfo import ZoneInfo

        ul = sym.upper()
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        by_day = self._session_open_by_symbol.setdefault(ul, {})
        if today not in by_day:
            by_day[today] = spot

    def _lock_quote_day_open(self, sym: str, raw: dict | None) -> None:
        if not isinstance(raw, dict):
            return
        from algoflat.broker.base import BrokerAdapter

        day_open = BrokerAdapter.parse_decimal(raw.get("o"))
        if day_open is not None and day_open > 0:
            self._lock_session_open(sym, day_open)
        # Noren/Flattrade: ``c`` is previous close (day-change reference), not LTP.
        prev_close = BrokerAdapter.parse_decimal(raw.get("c"))
        if prev_close is not None and prev_close > 0:
            self._prev_close_by_symbol[sym.upper()] = prev_close

    def _prev_close_for_symbol(self, sym: str) -> Decimal | None:
        ul = sym.upper()
        cached = self._prev_close_by_symbol.get(ul)
        if cached is not None and cached > 0:
            return cached
        # Futures cards share the base index previous close when the fut quote
        # has not arrived yet.
        if ul.endswith("_FUT"):
            base = self._prev_close_by_symbol.get(ul.removesuffix("_FUT"))
            if base is not None and base > 0:
                return base
        return None

    @staticmethod
    def _change_vs_baseline(
        spot: Decimal | None, baseline: Decimal | None
    ) -> tuple[float | None, float | None]:
        """Absolute and percent change vs previous close (broker day change)."""
        if spot is None or baseline is None or baseline == 0:
            return None, None
        chg = spot - baseline
        return float(chg), float((chg / baseline) * Decimal("100"))

    def _lock_session_open(self, sym: str, open_px: Decimal) -> None:
        """Store the exchange day open. Replaces a first-tick or wrong-token open."""
        if open_px is None or open_px <= 0:
            return
        from zoneinfo import ZoneInfo

        ul = sym.upper()
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        self._session_open_by_symbol.setdefault(ul, {})[today] = open_px

    def _session_open_context(self, sym: str) -> tuple[str | None, str, str]:
        """Exchange, token, and cache key for session-open 1m fallback."""
        from algoflat.symbols_util import index_quote_context

        ul = sym.upper()
        if ul.endswith("_FUT"):
            base = ul.removesuffix("_FUT")
            merged = symbols_for(self.config, base)
            exchange, token = price_context(merged)
            return exchange, token, ul
        quoted = index_quote_context(self.config, ul)
        if quoted is not None:
            exchange, token = quoted
            return exchange, token, ul
        merged = symbols_for(self.config, ul)
        if uses_dual_index_spot(merged):
            exchange, token = index_spot_context(merged)
            return exchange, token, ul
        exchange, token = price_context(merged)
        return exchange, token, ul

    def _session_open_for_symbol(self, sym: str) -> Decimal | None:
        """Today's session open from cached 1m bars (09:15 IST first bar) or first tick."""
        from zoneinfo import ZoneInfo

        ul = sym.upper()
        _, token, cache_key = self._session_open_context(sym)
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        cached = self._session_open_by_symbol.get(cache_key, {}).get(today)
        if cached is not None:
            return cached
        base_ul = ul.removesuffix("_FUT") if ul.endswith("_FUT") else ul
        merged = symbols_for(self.config, base_ul)
        if (
            ul == self._active_underlying
            and not ul.endswith("_FUT")
            and not uses_dual_index_spot(merged)
        ):
            m1 = self.market_data.candles(CandleInterval.M1)
        elif token:
            m1 = self.market_data.candles_for_token(token, CandleInterval.M1)
        else:
            m1 = []
        if m1:
            open_px = m1[0].open
            self._session_open_by_symbol.setdefault(cache_key, {})[today] = open_px
            return open_px
        return None

    def _commodity_snapshot_row(
        self,
        sym: str,
        uni: ContractUniverse | None,
        *,
        default_step: float,
        default_band: float,
        default_atm_steps: int,
    ) -> dict[str, Any]:
        cfg = symbols_for(self.config, sym)
        c_step = float(cfg.get("strike_step", default_step))
        c_band = float(strike_band_points(cfg))
        c_steps = int(cfg.get("atm_strike_steps", default_atm_steps))
        spot_val = self._header_spot(sym, uni)
        fut_spot = self._trading_spot(sym, uni)
        atm_spot = self._atm_spot(sym, uni)
        display_uni = uni
        if uni is not None and atm_spot is not None:
            display_uni = self.contract_selector.retarget_atm(
                uni, atm_spot, strike_step=c_step
            )
            if display_uni.atm_strike != uni.atm_strike:
                self._chain_item_cache.pop(sym.upper(), None)
        chain_items: list[dict[str, Any]] = []
        if display_uni and display_uni.instruments:
            built = self._build_chain_items(
                display_uni,
                step=c_step,
                band=c_band,
                spot=atm_spot,
                atm_steps=c_steps,
            )
            chain_items = self._stabilize_chain_items(sym, built, atm_steps=c_steps)
        elif sym in self._chain_item_cache:
            chain_items = list(self._chain_item_cache[sym])
        chain_items = self._hydrate_chain_quotes(chain_items)
        session_open = self._session_open_for_symbol(sym)
        prev_close = self._prev_close_for_symbol(sym)
        change, change_pct = self._change_vs_baseline(spot_val, prev_close)
        return {
            "underlying": sym,
            "display_name": str(cfg.get("display_name", sym)),
            "spot_ltp": float(spot_val) if spot_val is not None else None,
            "trading_spot_ltp": float(fut_spot) if fut_spot is not None else None,
            "session_open": float(session_open) if session_open is not None else None,
            "prev_close": float(prev_close) if prev_close is not None else None,
            "change": change,
            "change_pct": change_pct,
            "atm_strike": float(display_uni.atm_strike)
            if display_uni and display_uni.atm_strike is not None
            else None,
            "expiry_symbol": uni.expiry_symbol if uni else None,
            "instrument_count": len(uni.instruments) if uni else 0,
            "strike_band_points": c_band,
            "strike_step": c_step,
            "atm_strike_steps": c_steps,
            "items": chain_items,
            "strike_count": len({i["strike"] for i in chain_items}),
        }

    def _ticker_snapshot_row(self, row: dict[str, Any]) -> dict[str, Any]:
        sym = str(row.get("symbol", "")).upper()
        spot_val = self._header_spot(sym)
        session_open = self._session_open_for_symbol(sym)
        prev_close = self._prev_close_for_symbol(sym)
        change, change_pct = self._change_vs_baseline(spot_val, prev_close)
        return {
            "underlying": sym,
            "display_name": str(row.get("display_name", sym)),
            "spot_ltp": float(spot_val) if spot_val is not None else None,
            "session_open": float(session_open) if session_open is not None else None,
            "prev_close": float(prev_close) if prev_close is not None else None,
            "change": change,
            "change_pct": change_pct,
            "atm_strike": None,
            "expiry_symbol": None,
            "instrument_count": 0,
            "strike_band_points": 0,
            "strike_step": 0,
            "atm_strike_steps": 0,
            "items": [],
            "strike_count": 0,
            "ticker_only": True,
        }

    async def _refresh_broker_open_positions(
        self,
        *,
        positions: list[dict[str, Any]] | None = None,
        reconcile: bool = True,
        trade_book_rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, int] | None:
        """Sync open holdings from Flattrade (live mode only)."""
        if not is_live_execution():
            self._broker_open_positions = []
            return None
        try:
            if positions is None:
                positions = await self._live_broker.fetch_open_positions()
            prev = len(self._broker_open_positions)
            self._broker_open_positions = positions
            stats = None
            if reconcile:
                stats = await self.orchestrator.positions.reconcile_broker_holdings(
                    positions,
                    option_data=self.option_data,
                    trade_book_rows=trade_book_rows,
                )
            if len(positions) != prev or self.orchestrator.positions.open_count > 0:
                self._watchlist_tick.set()
                self._position_tick.set()
            return stats
        except Exception:
            logger.exception("broker_open_positions_refresh_failed")
            return None

    async def _run_live_broker_reconcile(self) -> dict[str, int]:
        """One broker round-trip: positions reconcile + account sync + entry fill correction."""
        if not is_live_execution() or not self._is_market_open():
            return {"matched": 0, "adopted": 0, "external_closed": 0}
        snapshot = await self._live_broker.fetch_broker_snapshot()
        trade_book = await self._live_broker.fetch_trade_book()
        stats = await self._refresh_broker_open_positions(
            positions=list(snapshot[2]),
            reconcile=True,
            trade_book_rows=trade_book,
        )
        await self.orchestrator.risk.sync_live_account_from_broker(
            self._live_broker,
            broker_snapshot=snapshot,
        )
        return stats or {"matched": 0, "adopted": 0, "external_closed": 0}

    async def reconcile_broker_positions_now(self) -> dict[str, int]:
        if not is_live_execution():
            return {"matched": 0, "adopted": 0, "external_closed": 0}
        return await self._run_live_broker_reconcile()

    def _enrich_open_positions(
        self,
        rows: list[dict[str, Any]],
        *,
        entry_price_override: Decimal | None = None,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in rows:
            token = str(row.get("instrument_token") or "")
            state = self.option_data.get(token) if token else None
            entry = entry_price_override if entry_price_override is not None else Decimal(
                str(row.get("entry_price", 0))
            )
            qty = int(row.get("quantity", 0))
            lot_size = int(row.get("lot_size") or self._lot_size_for_position(token, row.get("tsym", "")))
            broker_ltp = Decimal(str(row.get("current_ltp", 0)))
            ltp = state.ltp if state and state.ltp is not None else broker_ltp
            enriched = dict(row)
            enriched["lot_size"] = lot_size
            enriched["lots"] = qty // max(lot_size, 1) if qty else 0
            enriched["current_ltp"] = float(ltp)
            broker_ur = row.get("unrealized_pnl")
            if entry_price_override is not None:
                enriched["entry_price"] = float(entry)
                if entry > 0 and qty > 0:
                    enriched["unrealized_pnl"] = float((ltp - entry) * qty)
            elif broker_ur is not None and float(broker_ur) != 0:
                enriched["unrealized_pnl"] = float(broker_ur)
            elif entry > 0 and qty > 0:
                enriched["unrealized_pnl"] = float((ltp - entry) * qty)
            out.append(enriched)
        return out

    def _algo_open_positions_for_ui(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for p in self.orchestrator.positions.open_positions:
            if is_live_execution() and p.mode != "live":
                continue
            rows.append(self._position_row_from_pm(p, extra={"source": "algo"}))
        return rows

    def get_positions_snapshot(self) -> dict[str, Any]:
        positions = self.open_positions_for_ui()
        return {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            **self._feed_status_snapshot(),
            "open_position_count": len(positions),
            "open_positions": positions,
        }

    def open_positions_for_ui(self) -> list[dict[str, Any]]:
        """Open positions for UI / streams.

        Managed algo positions always use **order-fill entry LTP + algo qty** for
        P&L / lots / trail. Broker net avg / net qty are attached only as
        ``broker_entry_price`` / ``broker_quantity`` (same-token multi-fill).
        """
        algo = self._algo_open_positions_for_ui()
        if not is_live_execution():
            return algo

        if not self._broker_open_positions:
            return algo

        algo_by_token = {
            str(row.get("instrument_token") or ""): row for row in algo
        }
        pm_by_token = {
            p.instrument_token: p
            for p in self.orchestrator.positions.open_positions
            if p.mode == "live"
        }
        enriched: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()

        for broker_row in self._broker_open_positions:
            token = str(broker_row.get("instrument_token") or "")
            if not token:
                continue
            seen_tokens.add(token)
            algo_row = algo_by_token.get(token)
            pm = pm_by_token.get(token)

            if algo_row is not None or pm is not None:
                # Algo order fill is source of truth — never broker net avg.
                if algo_row is not None:
                    base = dict(algo_row)
                else:
                    assert pm is not None
                    base = self._position_row_from_pm(pm, extra={"source": "algo"})

                entry = Decimal(
                    str(
                        (pm.entry_price if pm is not None else None)
                        or base.get("entry_price")
                        or 0
                    )
                )
                qty = int(
                    (pm.quantity if pm is not None else None)
                    or base.get("quantity")
                    or 0
                )
                lot_size = int(
                    base.get("lot_size")
                    or self._lot_size_for_position(token, str(base.get("tsym") or ""))
                )
                state = self.option_data.get(token)
                broker_ltp = Decimal(str(broker_row.get("current_ltp") or 0))
                ltp = (
                    state.ltp
                    if state is not None and state.ltp is not None
                    else (
                        pm.last_good_ltp
                        if pm is not None and pm.last_good_ltp and pm.last_good_ltp > 0
                        else broker_ltp
                    )
                )
                if ltp is None or ltp <= 0:
                    ltp = entry

                base["entry_price"] = float(entry)
                base["quantity"] = qty
                base["lot_size"] = lot_size
                base["lots"] = qty // max(lot_size, 1) if qty else 0
                base["premium_deployed"] = float(
                    (pm.premium_deployed if pm is not None else None)
                    or base.get("premium_deployed")
                    or (entry * qty)
                )
                base["current_ltp"] = float(ltp)
                if entry > 0 and qty > 0:
                    base["unrealized_pnl"] = float((ltp - entry) * qty)
                if pm is not None:
                    base.update(self._position_trail_fields(pm, ltp))
                    base["position_id"] = str(pm.position_id)
                    base["order_id"] = str(pm.order_id)
                    base["setup_type"] = pm.setup_type
                    base["entry_ts"] = pm.entry_ts.isoformat()
                base["entry_price_source"] = "order_fill"
                base["managed"] = True
                base["source"] = "broker+algo"
                base["broker_entry_price"] = broker_row.get("broker_avg_price") or broker_row.get(
                    "entry_price"
                )
                base["broker_quantity"] = broker_row.get("quantity")
                broker_qty = int(broker_row.get("quantity") or 0)
                if broker_qty > 0 and qty > 0 and broker_qty != qty:
                    base["broker_qty_mismatch"] = True
                    logger.info(
                        "open_position_broker_qty_differs",
                        tsym=base.get("tsym"),
                        algo_qty=qty,
                        broker_qty=broker_qty,
                        algo_entry=str(entry),
                        broker_avg=base.get("broker_entry_price"),
                    )
                enriched.append(base)
                continue

            # Unmanaged broker-only holding (manual / external) — display only.
            merged = dict(self._enrich_open_positions([broker_row])[0])
            merged["managed"] = False
            merged["source"] = "broker"
            merged["broker_entry_price"] = broker_row.get("broker_avg_price") or broker_row.get(
                "entry_price"
            )
            merged["broker_quantity"] = broker_row.get("quantity")
            enriched.append(merged)

        for algo_row in algo:
            token = str(algo_row.get("instrument_token") or "")
            if token and token not in seen_tokens:
                enriched.append({**algo_row, "managed": True, "source": "algo"})
        return enriched

    def _side_confidence_snapshot(self) -> dict[str, Any]:
        """CE = bullish, PE = bearish MTF alignment scores (0–100)."""
        features = self.orchestrator.features.compute_cached(max_age_sec=2.0)
        extra = features.extra or {}
        decision = self.orchestrator.router.last_decision
        ce = extra.get("mtf_score_ce")
        pe = extra.get("mtf_score_pe")
        return {
            "bias_5m": features.bias_5m.value.upper(),
            "bullish_confidence": int(ce) if ce is not None else None,
            "bearish_confidence": int(pe) if pe is not None else None,
            "router_confidence": decision.confidence if decision else None,
            "bias_confidence_mismatch": bool(extra.get("bias_confidence_mismatch")),
            "mtf_confidence_gap": int(extra.get("mtf_confidence_gap") or 0),
        }

    def _feed_status_snapshot(self) -> dict[str, Any]:
        ws_open = bool(getattr(self.broker, "websocket_open", False))
        ws_age = age_seconds(self._last_ws_quote_ts)
        last_quote = get_engine_state().get("last_quote_ts")
        quote_age: float | None = None
        if last_quote:
            try:
                ts = ensure_ist(datetime.fromisoformat(str(last_quote).replace("Z", "+00:00")))
                quote_age = age_seconds(ts)
            except (TypeError, ValueError):
                quote_age = None
        mode = self._feed_mode
        if ws_open and ws_age is None and mode != "websocket":
            mode = "rest"
        return {
            "feed_mode": mode,
            "ws_open": ws_open,
            "ws_last_quote_ts": (
                ensure_ist(self._last_ws_quote_ts).isoformat()
                if self._last_ws_quote_ts
                else None
            ),
            "ws_quote_age_sec": round(ws_age, 1) if ws_age is not None else None,
            "last_quote_ts": last_quote,
            "quote_age_sec": round(quote_age, 1) if quote_age is not None else None,
            "ist_time": now_ist().isoformat(),
        }

    def get_watchlist_snapshot(self) -> dict[str, Any]:
        underlying = self._active_underlying
        sym_cfg = symbols_for(self.config, underlying)
        step = float(sym_cfg.get("strike_step", 50))
        band = float(strike_band_points(sym_cfg))
        atm_steps = int(sym_cfg.get("atm_strike_steps", 5))

        commodities = self._build_header_commodity_rows(
            default_step=step,
            default_band=band,
            default_atm_steps=atm_steps,
            include_chain=True,
        )
        seen = {c["underlying"] for c in commodities}
        for sym, uni in self._universes.items():
            if sym in seen:
                continue
            commodities.append(
                self._commodity_snapshot_row(
                    sym,
                    uni,
                    default_step=step,
                    default_band=band,
                    default_atm_steps=atm_steps,
                )
            )

        active = next((c for c in commodities if c["underlying"] == underlying), None)
        if active is None and commodities:
            active = commodities[0]
        items = active["items"] if active else []
        spot = active.get("spot_ltp") if active else None
        if spot is None and active:
            spot = active.get("trading_spot_ltp")
        engine_spot = (
            active.get("trading_spot_ltp") if active else None
        )
        if engine_spot is None and active:
            engine_spot = active.get("spot_ltp")
        atm_strike = active["atm_strike"] if active else None
        expiry_symbol = active["expiry_symbol"] if active else None

        open_positions = self.open_positions_for_ui()

        if engine_spot is not None:
            self._sync_engine_spot_state(Decimal(str(engine_spot)))
            set_engine_state(
                {
                    "instrument_count": len(items)
                    or len(self._universe.instruments)
                    if self._universe
                    else 0,
                    "atm_strike": str(atm_strike) if atm_strike is not None else None,
                }
            )

        return {
            "underlying": underlying,
            "active_underlying": self._active_underlying,
            "commodities": commodities,
            "spot_ltp": spot,
            "atm_strike": atm_strike,
            "expiry_symbol": expiry_symbol,
            "instrument_count": len(items),
            "strike_count": len({i["strike"] for i in items}),
            "strike_band_points": band,
            "strike_step": step,
            "atm_strike_steps": atm_steps,
            "last_quote_ts": get_engine_state().get("last_quote_ts"),
            **self._feed_status_snapshot(),
            "market_open": self._is_market_open(),
            "greeks_source": "black_scholes",
            "items": items,
            "open_positions": open_positions,
            **self._side_confidence_snapshot(),
        }

    async def get_underlying_chart_bars(
        self,
        underlying: str,
        *,
        minutes: int = 15,
        days: int = 30,
    ) -> dict[str, Any]:
        """OHLC bars for dashboard chart (1m / 3m / 5m / 15m) with DB history."""
        from algoflat.contract_selector.scripmaster import futures_tokens_for_underlying
        from algoflat.features.indicators import aggregate_from_m5
        from algoflat.market_data.engine import session_start_utc

        sym = underlying.upper()
        merged = symbols_for(self.config, sym)
        exchange, token = price_context(merged)
        if not token:
            return {"underlying": sym, "interval": f"{minutes}m", "bars": []}

        index_chart = not uses_futures_price(merged)
        if index_chart:
            chart_tokens = [token]
        else:
            chart_tokens = list(
                dict.fromkeys(
                    [
                        token,
                        *futures_tokens_for_underlying(underlying=sym, exchange=exchange),
                        *(
                            tok
                            for tok, ul in self._token_to_underlying.items()
                            if ul == sym
                        ),
                        *self.market_data._candles_by_token.keys(),
                    ]
                )
            )

        interval_map = {
            1: (CandleInterval.M1, "1m"),
            3: (CandleInterval.M3, "3m"),
            5: (CandleInterval.M5, "5m"),
        }
        if minutes == 15:
            source_interval = CandleInterval.M5
            interval_label = "15m"
        else:
            source_interval, interval_label = interval_map.get(
                minutes, (CandleInterval.M5, "15m")
            )

        now = datetime.now(tz=timezone.utc)
        history_start = now - timedelta(days=max(1, days))
        if not index_chart:
            try:
                pool = get_pool()
                async with pool.acquire() as conn:
                    chart_tokens = await self.market_data.chart_tokens_from_db(
                        conn,
                        seed_tokens=chart_tokens,
                        start=history_start,
                        ref_token=token,
                    )
            except Exception:
                logger.exception("chart_token_discovery_failed", underlying=sym)

        db_bars: list[Candle] = []
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                db_bars = await self.market_data.candles_from_db_for_tokens(
                    conn, chart_tokens, source_interval, history_start, now
                )
        except Exception:
            logger.exception("chart_candle_db_load_failed", underlying=sym)

        if db_bars:
            by_token: dict[str, list[Candle]] = {}
            for candle in db_bars:
                by_token.setdefault(candle.instrument_token, []).append(candle)
            db_bars = self.market_data.stitch_contract_candles(
                by_token, preferred_token=token
            )

        cached: list[Candle] = []
        if sym == self._active_underlying:
            cached = self.market_data.candles(source_interval)
        elif token:
            cached = self.market_data.candles_for_token(token, source_interval)

        bars_source = self.market_data.merge_candles(db_bars, cached)
        session_start = session_start_utc(now)
        has_today = bool(bars_source and bars_source[-1].ts >= session_start)
        # Prefer DB/session cache during market hours — broker fetch blocks the API for seconds.
        if (not bars_source or not has_today) and self._is_market_open():
            try:
                broker_bars = await self.broker.get_candles(
                    exchange,
                    token,
                    source_interval,
                    session_start,
                    now,
                )
                bars_source = self.market_data.merge_candles(bars_source, broker_bars)
            except Exception:
                logger.exception("chart_candle_fetch_failed", underlying=sym)

        if minutes == 15:
            m5 = bars_source if source_interval == CandleInterval.M5 else []
            if not m5:
                try:
                    pool = get_pool()
                    async with pool.acquire() as conn:
                        m5 = await self.market_data.candles_from_db_for_tokens(
                            conn, chart_tokens, CandleInterval.M5, history_start, now
                        )
                except Exception:
                    m5 = []
                if sym == self._active_underlying:
                    m5 = self.market_data.merge_candles(
                        m5, self.market_data.candles(CandleInterval.M5)
                    )
                elif token:
                    m5 = self.market_data.merge_candles(
                        m5,
                        self.market_data.candles_for_token(token, CandleInterval.M5),
                    )
                if not m5:
                    try:
                        m5 = await self.broker.get_candles(
                            exchange,
                            token,
                            CandleInterval.M5,
                            session_start_utc(now),
                            now,
                        )
                    except Exception:
                        m5 = []
            bars = aggregate_from_m5(m5, 15)
        else:
            bars = bars_source

        # Paint the forming bar with the live NIFTY 50 index (or futures, if that mode is on).
        live_spot = self._cash_index_spot(sym) if index_chart else self._trading_spot(sym)
        if live_spot is None:
            live_spot = self._trading_spot(sym)
        session_start = session_start_utc(now)
        if live_spot is not None and bars and self._is_market_open():
            last = bars[-1]
            if last.ts >= session_start:
                px = Decimal(str(live_spot))
                bars = [
                    *bars[:-1],
                    last.model_copy(
                        update={
                            "high": max(last.high, px),
                            "low": min(last.low, px),
                            "close": px,
                        }
                    ),
                ]

        fut_tsym = None if index_chart else str(merged.get("fut_tsym") or "")
        return {
            "underlying": sym,
            "interval": interval_label,
            "price_source": "index" if index_chart else "futures",
            "instrument_token": token,
            "fut_tsym": fut_tsym or None,
            "bars": [
                {
                    "ts": c.ts.isoformat(),
                    "open": float(c.open),
                    "high": float(c.high),
                    "low": float(c.low),
                    "close": float(c.close),
                    "volume": c.volume,
                }
                for c in bars
            ],
        }

    def _resolve_chart_instrument(
        self,
        token: str,
        tsym: str | None,
        exchange: str,
    ) -> tuple[str, str, str | None]:
        """Map watchlist token/tsym to broker token + options exchange (NFO/BFO)."""
        tok = str(token).strip()
        display_tsym = (tsym or "").strip() or None
        exch = (exchange or "NFO").strip().upper() or "NFO"
        if self._universe:
            match = next(
                (
                    inst
                    for inst in self._universe.instruments
                    if inst.token == tok
                    or (display_tsym and inst.tsym == display_tsym)
                ),
                None,
            )
            if match is not None:
                tok = match.token
                exch = str(match.exchange or exch).upper()
                if not display_tsym:
                    display_tsym = match.tsym
        return tok, exch, display_tsym

    async def _load_option_interval_candles(
        self,
        tok: str,
        exch: str,
        interval: CandleInterval,
        *,
        history_start: datetime,
        session_start: datetime,
        now: datetime,
    ) -> list[Candle]:
        """Merge DB, in-memory cache, and broker candles for one option interval."""
        db_bars: list[Candle] = []
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                db_bars = await self.market_data.candles_from_db_for_tokens(
                    conn, [tok], interval, history_start, now
                )
        except Exception:
            logger.exception(
                "option_chart_db_load_failed",
                token=tok,
                interval=interval.value,
            )

        cached = self.market_data.candles_for_token(tok, interval)
        merged = self.market_data.merge_candles(db_bars, cached)
        has_today = bool(merged and merged[-1].ts >= session_start)
        if not merged or not has_today:
            try:
                broker_bars = await self.broker.get_candles(
                    exch,
                    tok,
                    interval,
                    history_start if not merged else session_start,
                    now,
                )
                merged = self.market_data.merge_candles(merged, broker_bars)
            except Exception:
                logger.exception(
                    "option_chart_candle_fetch_failed",
                    token=tok,
                    exchange=exch,
                    interval=interval.value,
                )
        if merged:
            self.market_data.store_candles_for_token(tok, interval, merged)
        return merged

    async def get_instrument_chart_bars(
        self,
        token: str,
        *,
        exchange: str = "NFO",
        tsym: str | None = None,
        underlying: str = "NIFTY",
        minutes: int = 15,
        days: int = 30,
    ) -> dict[str, Any]:
        """OHLC bars for a single option/futures token (1m / 3m / 5m / 15m)."""
        from algoflat.features.indicators import aggregate_from_m1, aggregate_from_m5
        from algoflat.market_data.engine import session_start_utc

        sym = (underlying or "NIFTY").upper()
        tok, exch, display_tsym = self._resolve_chart_instrument(token, tsym, exchange)
        interval_map = {
            1: (CandleInterval.M1, "1m"),
            3: (CandleInterval.M3, "3m"),
            5: (CandleInterval.M5, "5m"),
        }
        if minutes == 15:
            source_interval = CandleInterval.M5
            interval_label = "15m"
        else:
            source_interval, interval_label = interval_map.get(
                minutes, (CandleInterval.M5, "15m")
            )

        empty: dict[str, Any] = {
            "underlying": sym,
            "interval": interval_label,
            "price_source": "option",
            "instrument_token": tok,
            "fut_tsym": display_tsym,
            "bars": [],
        }
        if not tok:
            return empty

        now = datetime.now(tz=timezone.utc)
        history_start = now - timedelta(days=max(1, days))
        session_start = session_start_utc(now)

        bars_source = await self._load_option_interval_candles(
            tok,
            exch,
            source_interval,
            history_start=history_start,
            session_start=session_start,
            now=now,
        )

        # Option band refresh only stores M1 — synthesize 3m/5m when broker MTF is empty.
        if minutes in (3, 5) and not bars_source:
            m1 = await self._load_option_interval_candles(
                tok,
                exch,
                CandleInterval.M1,
                history_start=history_start,
                session_start=session_start,
                now=now,
            )
            if m1:
                bars_source = aggregate_from_m1(m1, minutes)

        if minutes == 15:
            m5 = bars_source if source_interval == CandleInterval.M5 else []
            if not m5:
                m5 = await self._load_option_interval_candles(
                    tok,
                    exch,
                    CandleInterval.M5,
                    history_start=history_start,
                    session_start=session_start,
                    now=now,
                )
            if not m5:
                m1 = await self._load_option_interval_candles(
                    tok,
                    exch,
                    CandleInterval.M1,
                    history_start=history_start,
                    session_start=session_start,
                    now=now,
                )
                if m1:
                    m5 = aggregate_from_m1(m1, 5)
            bars = aggregate_from_m5(m5, 15) if m5 else []
        else:
            bars = bars_source

        live_ltp = None
        state = self.option_data.get(tok)
        if state is not None and state.ltp is not None:
            live_ltp = float(state.ltp)
        if live_ltp is not None and bars and self._is_market_open():
            last = bars[-1]
            if last.ts >= session_start:
                px = Decimal(str(live_ltp))
                bars = [
                    *bars[:-1],
                    last.model_copy(
                        update={
                            "high": max(last.high, px),
                            "low": min(last.low, px),
                            "close": px,
                        }
                    ),
                ]

        return {
            "underlying": sym,
            "interval": interval_label,
            "price_source": "option",
            "instrument_token": tok,
            "fut_tsym": display_tsym,
            "bars": [
                {
                    "ts": c.ts.isoformat(),
                    "open": float(c.open),
                    "high": float(c.high),
                    "low": float(c.low),
                    "close": float(c.close),
                    "volume": c.volume,
                }
                for c in bars
            ],
        }

    async def get_broker_closed_trades(
        self,
        *,
        limit: int = 500,
        trade_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Live closed round-trips from Flattrade trade book (not algo DB)."""
        from datetime import date as date_cls
        from zoneinfo import ZoneInfo

        from algoflat.broker.broker_trades import round_trips_from_trade_book

        day = (
            date_cls.fromisoformat(trade_date)
            if trade_date
            else datetime.now(ZoneInfo("Asia/Kolkata")).date()
        )
        rows = await self._live_broker.fetch_trade_book()
        return round_trips_from_trade_book(rows, trade_date=day, limit=limit)

    async def get_broker_closed_trades_cached(
        self,
        *,
        limit: int = 500,
        trade_date: str | None = None,
        max_age_sec: float | None = None,
    ) -> list[dict[str, Any]]:
        ttl = max_age_sec if max_age_sec is not None else self._broker_trades_cache_ttl_sec
        now = time.monotonic()
        if (
            self._broker_trades_cache is not None
            and trade_date is None
            and now - self._broker_trades_cache[0] < ttl
        ):
            cached = self._broker_trades_cache[1]
            return cached[:limit] if limit else list(cached)
        trades = await self.get_broker_closed_trades(limit=limit, trade_date=trade_date)
        if trade_date is None:
            self._broker_trades_cache = (now, list(trades))
        return trades

    async def get_trade_blotter(self, limit: int = 200) -> list[dict[str, Any]]:
        """Today's closed trades with entry/exit time, LTP, lots, P&L."""
        from algoflat.runtime.trading_mode import get_execution_mode

        mode_filter = get_execution_mode()
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    ct.id,
                    ct.entry_ts,
                    ct.exit_ts,
                    ct.entry_price,
                    ct.exit_price,
                    ct.quantity,
                    ct.pnl,
                    ct.exit_reason,
                    ct.setup_type,
                    ct.hold_seconds,
                    ct.mode,
                    p.tsym,
                    p.side AS position_side
                FROM closed_trades ct
                JOIN positions p ON p.id = ct.position_id
                WHERE (ct.entry_ts AT TIME ZONE 'Asia/Kolkata')::date
                      = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                  AND ct.mode = $2
                ORDER BY ct.exit_ts DESC
                LIMIT $1
                """,
                limit,
                mode_filter,
            )
        if not rows and mode_filter == "live":
            return await self.get_broker_closed_trades(limit=limit)
        blotter = []
        lot_default = 65
        for r in rows:
            qty = int(r["quantity"])
            lot_size = lot_default
            if self._universe:
                match = next((i for i in self._universe.instruments if i.tsym == r["tsym"]), None)
                if match:
                    lot_size = match.lot_size
            blotter.append(
                {
                    "id": str(r["id"]),
                    "tsym": r["tsym"],
                    "side": r["position_side"],
                    "entry_ts": r["entry_ts"].isoformat(),
                    "exit_ts": r["exit_ts"].isoformat(),
                    "entry_price": float(r["entry_price"]),
                    "exit_price": float(r["exit_price"]),
                    "quantity": qty,
                    "lot_size": lot_size,
                    "lots": qty // max(lot_size, 1),
                    "pnl": float(r["pnl"]),
                    "exit_reason": r["exit_reason"],
                    "setup_type": r["setup_type"],
                    "hold_seconds": r["hold_seconds"],
                    "mode": r["mode"],
                }
            )
        return blotter

    async def _discover_history_tokens(self) -> tuple[str | None, list[str]]:
        merged = apply_active_underlying(self.config, self._active_underlying)
        exchange, active_token = price_context(merged)
        history_tokens = [active_token] if active_token else []
        if not uses_futures_price(merged):
            return exchange, history_tokens
        try:
            from algoflat.contract_selector.scripmaster import futures_tokens_for_underlying

            history_tokens = list(
                dict.fromkeys(
                    [
                        *history_tokens,
                        *futures_tokens_for_underlying(
                            underlying=self._active_underlying,
                            exchange=exchange,
                        ),
                    ]
                )
            )
            pool = get_pool()
            async with pool.acquire() as conn:
                history_tokens = await self.market_data.chart_tokens_from_db(
                    conn,
                    seed_tokens=history_tokens,
                    start=datetime.now(tz=timezone.utc) - timedelta(days=30),
                    ref_token=active_token or history_tokens[0],
                )
        except Exception:
            logger.exception("history_token_discovery_failed")
        return exchange, history_tokens

    async def _backfill_chart_history(self, *, days: int = 30) -> dict[str, Any]:
        exchange, history_tokens = await self._discover_history_tokens()
        result: dict[str, Any] = {
            "exchange": exchange,
            "tokens": history_tokens,
            "days": days,
            "added": {},
            "rows": 0,
        }
        if not history_tokens or not exchange:
            return result
        history_added, history_rows = await self.market_data.backfill_contract_history(
            exchange=exchange,
            tokens=history_tokens,
            days=days,
        )
        if history_rows:
            by_interval: dict[CandleInterval, list[Candle]] = {}
            for candle in history_rows:
                by_interval.setdefault(candle.interval, []).append(candle)
            for interval_rows in by_interval.values():
                await self.journal.write_candles(interval_rows)
        result["added"] = history_added
        result["rows"] = len(history_rows)
        return result

    async def _needs_fresh_setup_data(self) -> tuple[bool, str]:
        merged = apply_active_underlying(self.config, self._active_underlying)
        _, active_token = price_context(merged)
        if not active_token:
            return False, "no_price_token"
        pool = get_pool()
        async with pool.acquire() as conn:
            inst_count = int(
                await conn.fetchval("SELECT COUNT(*) FROM instruments WHERE in_band = TRUE")
                or 0
            )
            coverage = await self.market_data.historical_coverage_from_db(
                conn,
                active_token,
                lookback_days=FRESH_SETUP_LOOKBACK_DAYS,
            )
        if inst_count == 0:
            return True, "no_instruments"
        need, reason = needs_fresh_setup_backfill(coverage)
        if need:
            return True, reason
        return False, "ok"

    async def _maybe_auto_fetch_fresh_setup_data(self) -> dict[str, Any] | None:
        """On first run / empty DB, pull last week's candles without manual sync."""
        need, reason = await self._needs_fresh_setup_data()
        if not need:
            return None

        logger.info("fresh_setup_auto_fetch_start", reason=reason)
        await self.journal.write_notification(
            "system",
            "info",
            "Fresh setup — fetching market data",
            (
                f"Missing history ({reason}). "
                f"Pulling last {FRESH_SETUP_LOOKBACK_DAYS} days from {broker_data_source_label()}…"
            ),
        )
        history = await self._backfill_chart_history(days=FRESH_SETUP_LOOKBACK_DAYS)
        await self.market_data.refresh_session_candles(force=True)
        for interval_candles in self.market_data._candles.values():
            await self.journal.write_candles(interval_candles)

        report: dict[str, Any] = {
            "triggered": True,
            "reason": reason,
            "history": history,
        }
        await self.journal.write_system_event(
            SystemEvent(
                event_type="fresh_setup_auto_fetch",
                ts=datetime.now(tz=timezone.utc),
                severity="info",
                message="Automatic data fetch on fresh setup",
                metadata=report,
            )
        )
        logger.info("fresh_setup_auto_fetch_done", **report)
        return report

    async def paper_trial_roundtrip(self, side: str = "CE") -> dict[str, Any]:
        return await self._trial_roundtrip(side, required_mode="paper")

    async def live_trial_roundtrip(self, side: str = "CE") -> dict[str, Any]:
        return await self._trial_roundtrip(side, required_mode="live")

    async def _trial_roundtrip(self, side: str, *, required_mode: str) -> dict[str, Any]:
        """Buy 1 lot ATM option, Telegram notify, then square off (paper or live)."""
        from algoflat.models.events import CandidateSignal, ValidationResult
        from algoflat.risk.engine import EntrySizing

        if get_execution_mode() != required_mode:
            return {"ok": False, "error": f"{required_mode}_mode_only"}

        side = side.upper()
        if side not in ("CE", "PE"):
            return {"ok": False, "error": "side_must_be_CE_or_PE"}

        universe = self._universe
        if universe is None or not universe.instruments:
            return {"ok": False, "error": "no_universe"}

        inst = universe.atm_ce if side == "CE" else universe.atm_pe
        if inst is None:
            return {"ok": False, "error": f"no_atm_{side}"}

        opt = self.option_data.get(inst.token)
        if opt is None or opt.ltp is None:
            raw = await self.broker.get_quotes(inst.exchange, inst.token)
            quote = quote_from_rest(inst.exchange, inst.token, raw)
            if quote is not None:
                self.option_data.update_from_quote(quote)
                opt = self.option_data.get(inst.token)
        if opt is None or opt.ltp is None:
            return {"ok": False, "error": "no_option_ltp"}

        orch = self.orchestrator
        risk_snap = await orch.risk.ensure_daily_state()
        if risk_snap.kill_switch or risk_snap.entries_blocked:
            return {
                "ok": False,
                "error": "entries_blocked",
                "kill_switch": risk_snap.kill_switch,
                "block_reason": risk_snap.block_reason,
            }
        if orch.positions.has_open_for_token(inst.token):
            return {"ok": False, "error": "position_already_open_for_token"}

        setup_type = f"{required_mode}_trial"
        features = orch.features.compute()
        signal = CandidateSignal(
            ts=datetime.now(tz=timezone.utc),
            setup_type=setup_type,
            side=side,
            instrument_token=inst.token,
            tsym=inst.tsym,
            strategy_version=str(self.config.strategy.get("strategy_version", "trial")),
            feature_snapshot=features,
            confidence=99,
            scanner_metadata={
                "exchange": inst.exchange,
                "lot_size": inst.lot_size,
                "option_ltp": str(opt.ltp),
                "trial": True,
                "mode": required_mode,
            },
        )

        lot_size = int(inst.lot_size or SENSEX_LOT_SIZE)
        entry_ltp = Decimal(str(opt.ltp))
        sizing = EntrySizing(
            approved=True,
            quantity=lot_size,
            lot_size=lot_size,
            entry_ltp=entry_ltp,
            premium_required=entry_ltp * lot_size,
            lots=1,
            confidence=99,
        )

        logger.info(
            f"{required_mode}_trial_start",
            side=side,
            tsym=inst.tsym,
            quantity=lot_size,
            ltp=str(entry_ltp),
        )

        await orch._journal.write_candidate_signal(signal)
        validation = ValidationResult(
            candidate_signal_id=signal.id,
            ts=signal.ts,
            passed=True,
            rejection_reasons=[],
            validator_version=f"{required_mode}_trial_bypass",
        )
        await orch._journal.write_validation(validation)

        await orch.risk.reserve_capital(sizing.premium_required, self._active_underlying)
        try:
            position_id, order_id, update = await orch.execution.enter(signal, sizing)
            await orch.positions.register_open_from_order(
                position_id,
                order_id,
                signal.id,
                signal,
                sizing,
                update,
                mode=required_mode,
            )
        except Exception as exc:
            await orch.risk.release_capital(
                sizing.premium_required, Decimal("0"), self._active_underlying
            )
            logger.exception(f"{required_mode}_trial_entry_failed")
            return {"ok": False, "error": str(exc)}

        logger.info(
            f"{required_mode}_trial_entry_filled",
            tsym=inst.tsym,
            quantity=lot_size,
            fill=str(update.fill_price or entry_ltp),
            position_id=str(position_id),
            broker_order=str(update.broker_order_id),
        )

        await asyncio.sleep(3)

        exit_opt = self.option_data.get(inst.token)
        exit_ltp = exit_opt.ltp if exit_opt and exit_opt.ltp is not None else entry_ltp
        exit_report = await orch.positions.manual_exit(position_id, exit_ltp)

        logger.info(
            f"{required_mode}_trial_exit_done",
            tsym=inst.tsym,
            exit=exit_report,
        )

        await orch._journal.write_system_event(
            SystemEvent(
                event_type="manual_sync",
                ts=datetime.now(tz=timezone.utc),
                severity="info",
                message=f"{required_mode}_trial_roundtrip_complete",
                metadata={
                    "mode": required_mode,
                    "side": side,
                    "tsym": inst.tsym,
                    "lots": 1,
                    "quantity": lot_size,
                    "entry_fill": str(update.fill_price or entry_ltp),
                    "exit": exit_report,
                },
            )
        )

        return {
            "ok": True,
            "mode": required_mode,
            "side": side,
            "tsym": inst.tsym,
            "lots": 1,
            "quantity": lot_size,
            "entry_fill": str(update.fill_price or entry_ltp),
            "broker_order_id": update.broker_order_id,
            "exit_ltp": str(exit_ltp),
            "position_id": str(position_id),
            "order_id": str(order_id),
            "exit": exit_report,
        }

    async def sync_missing_data(self) -> dict[str, Any]:
        """Fetch from broker only what DB / in-memory state is missing."""
        from zoneinfo import ZoneInfo

        if not await self._has_valid_session():
            return {
                "ok": False,
                "error": "broker_not_connected",
                "message": f"{broker_display_name()} session invalid — use Re-authenticate first.",
            }

        await self._maybe_roll_futures()

        band = float(self.config.symbols.get("strike_band_points", 300))
        step = float(self.config.symbols.get("strike_step", 50))
        expected_strikes = int(round((2 * band) / step)) + 1
        expected_inst = expected_strikes * 2
        spot_token = self.config.symbols["spot_token"]
        pool = get_pool()
        market_open = self._is_market_open()

        report: dict[str, Any] = {
            "ok": True,
            "universe": {},
            "candles": {},
            "quotes": {},
        }

        async with pool.acquire() as conn:
            before_inst = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM instruments WHERE in_band = TRUE"
                )
                or 0
            )
            db_candle_stats = await self.market_data.session_candle_stats_from_db(
                conn, spot_token
            )
            m1_before = int(db_candle_stats["1m"]["count"])
            m3_before = int(db_candle_stats["3m"]["count"])
            m5_before = int(db_candle_stats["5m"]["count"])

        need_universe = (
            before_inst < expected_inst
            or self._universe is None
            or not self._universe.instruments
            or self._any_universe_undersized()
        )
        if need_universe:
            await self._rebuild_all_universes(reason="manual_sync_missing")
            universe_action = "refreshed"
        else:
            universe_action = "ok"

        after_inst = len(self._universe.instruments) if self._universe else 0
        report["universe"] = {
            "action": universe_action,
            "before": before_inst,
            "after": after_inst,
            "expected": expected_inst,
            "expiry_symbol": self._universe.expiry_symbol if self._universe else None,
        }

        hydrated: dict[str, int] = {}
        if not self.market_data.candles(CandleInterval.M1) and m1_before > 0:
            async with pool.acquire() as conn:
                hydrated = await self.market_data.hydrate_from_db(conn, spot_token)
            report["candles"]["hydrated_from_db"] = hydrated

        need_candles, candle_reason = self.market_data.needs_broker_candle_sync(
            db_candle_stats,
            market_open=market_open,
        )
        if need_candles:
            await self.market_data.refresh_session_candles(force=True)
            for interval_candles in self.market_data._candles.values():
                await self.journal.write_candles(interval_candles)
            candle_action = "refreshed"
        else:
            candle_action = "ok"

        need_history, hist_reason = await self._needs_fresh_setup_data()
        if need_history:
            history_result = await self._backfill_chart_history(days=FRESH_SETUP_LOOKBACK_DAYS)
        else:
            history_result = {"skipped": True, "reason": hist_reason}
        history_added = history_result.get("added") or {}
        if history_result.get("rows"):
            candle_action = "refreshed"

        async with pool.acquire() as conn:
            db_after = await self.market_data.session_candle_stats_from_db(conn, spot_token)
            m1_after = int(db_after["1m"]["count"])
            m3_after = int(db_after["3m"]["count"])
            m5_after = int(db_after["5m"]["count"])

        report["candles"] = {
            **report.get("candles", {}),
            "action": candle_action,
            "reason": candle_reason,
            "m1_in_memory": len(self.market_data.candles(CandleInterval.M1)),
            "m3_in_memory": len(self.market_data.candles(CandleInterval.M3)),
            "m5_in_memory": len(self.market_data.candles(CandleInterval.M5)),
            "m1_db_before": m1_before,
            "m3_db_before": m3_before,
            "m5_db_before": m5_before,
            "m1_added": max(0, m1_after - m1_before),
            "m3_added": max(0, m3_after - m3_before),
            "m5_added": max(0, m5_after - m5_before),
            "history_backfill": history_added,
        }

        missing_tokens: list[str] = []
        if self._universe:
            for inst in self._universe.instruments:
                state = self.option_data.get(inst.token)
                if state is None or state.ltp is None:
                    missing_tokens.append(inst.token)

        report["quotes"]["missing_ltp_before"] = len(missing_tokens)
        polled = 0
        quote_action = "ok"
        if missing_tokens and self._universe:
            by_token = {i.token: i for i in self._universe.instruments}

            async def _one(token: str):
                inst = by_token[token]
                raw = await self.broker.get_quotes(inst.exchange, inst.token)
                from algoflat.market_data.poller import quote_from_rest

                return quote_from_rest(inst.exchange, inst.token, raw)

            results = await asyncio.gather(
                *[_one(t) for t in missing_tokens],
                return_exceptions=True,
            )
            for q in results:
                if isinstance(q, Exception) or q is None:
                    continue
                await self.market_data.on_quote(q)
                self.option_data.update_from_quote(q)
                polled += 1
            quote_action = "polled_missing"
        elif not self._universe:
            quote_action = "skipped_no_universe"

        still_missing = 0
        if self._universe:
            for inst in self._universe.instruments:
                state = self.option_data.get(inst.token)
                if state is None or state.ltp is None:
                    still_missing += 1
        report["quotes"]["action"] = quote_action
        report["quotes"]["polled"] = polled
        report["quotes"]["missing_ltp_after"] = still_missing
        report["spot_ltp"] = (
            float(self.market_data.spot_ltp) if self.market_data.spot_ltp is not None else None
        )
        report["ist_time"] = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()

        await self.journal.write_system_event(
            SystemEvent(
                event_type="manual_sync",
                ts=datetime.now(tz=timezone.utc),
                severity="info",
                message="manual_sync_missing_data",
                metadata=report,
            )
        )
        return report

    async def reset_paper_account(self) -> dict[str, Any]:
        """Wipe mock/paper trades and restore capital for a clean session."""
        if is_live_execution():
            raise ValueError("Cannot reset paper account while live trading is enabled")
        if uses_pooled_capital(self.config):
            total_capital = total_account_capital(self.config)
            reset_rows: list[tuple[str, Decimal]] = [
                (risk_underlying_key(self.config), total_capital)
            ]
        else:
            total_capital = Decimal("0")
            reset_rows = []
            for row in list_underlyings(self.config):
                sym = str(row.get("symbol", "")).upper()
                if not sym:
                    continue
                cap = capital_for(self.config, sym)
                reset_rows.append((sym, cap))
                total_capital += cap
            if not reset_rows:
                cap = total_account_capital(self.config)
                reset_rows = [("NIFTY", cap)]
                total_capital = cap

        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM closed_trades")
                await conn.execute("DELETE FROM positions")
                await conn.execute("DELETE FROM orders")
                await conn.execute("DELETE FROM validation_results")
                await conn.execute("DELETE FROM ml_scores")
                await conn.execute("DELETE FROM candidate_signals")
                await conn.execute(
                    """
                    DELETE FROM daily_risk_state
                    WHERE trade_date <> (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                    """
                )
                if uses_pooled_capital(self.config):
                    await conn.execute(
                        """
                        DELETE FROM daily_risk_state
                        WHERE trade_date = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                          AND underlying <> $1
                        """,
                        risk_underlying_key(self.config),
                    )
                await conn.execute(
                    "DELETE FROM instruments WHERE in_band = FALSE OR expiry_date < CURRENT_DATE"
                )
                for sym, cap in reset_rows:
                    await conn.execute(
                        """
                        UPDATE daily_risk_state SET
                            starting_capital = $1,
                            available_capital = $1,
                            deployed_capital = 0,
                            realized_pnl = 0,
                            trade_count = 0,
                            consecutive_losses = 0,
                            kill_switch = FALSE,
                            entries_blocked = FALSE,
                            block_reason = NULL,
                            updated_at = now()
                        WHERE trade_date = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                          AND underlying = $2
                        """,
                        cap,
                        sym,
                    )
                    await conn.execute(
                        """
                        INSERT INTO daily_risk_state (
                            trade_date, underlying, starting_capital, available_capital,
                            deployed_capital, realized_pnl, trade_count, consecutive_losses,
                            kill_switch, entries_blocked
                        ) VALUES (
                            (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date,
                            $2, $1, $1, 0, 0, 0, 0, FALSE, FALSE
                        )
                        ON CONFLICT (trade_date, underlying) DO NOTHING
                        """,
                        cap,
                        sym,
                    )

        reset_msg = (
            f"Balance restored to ₹{total_capital:,.0f}"
            + (
                " (shared pool)"
                if uses_pooled_capital(self.config)
                else " across indices"
            )
            + " · old trades removed"
        )
        await self.journal.write_notification(
            "system",
            "info",
            "Paper account reset",
            reset_msg,
        )

        # Clear in-memory open positions + exit cooldowns so trading can resume.
        self.orchestrator.positions._open.clear()
        self.orchestrator.positions.clear_cooldowns()
        self.orchestrator.positions._pending_flips.clear()

        refresh_warnings: list[str] = []
        # Avoid rebuilding the full option chain on reset — that fans out dozens of
        # Upstox quote/candle calls and can trip UDAPI10005 while background polls run.
        if not (self._universe and self._universe.instruments):
            try:
                await self._refresh_universe(reason="paper_account_reset")
            except Exception as exc:
                logger.warning("paper_reset_universe_refresh_failed", error=str(exc))
                refresh_warnings.append(f"universe refresh: {exc}")

        refreshed = False
        try:
            refreshed = await self.market_data.refresh_session_candles(force=True)
            if refreshed:
                for interval_candles in self.market_data._candles.values():
                    await self.journal.write_candles(interval_candles)
        except Exception as exc:
            logger.warning("paper_reset_candle_refresh_failed", error=str(exc))
            refresh_warnings.append(f"candle refresh: {exc}")

        return {
            "ok": True,
            "starting_capital": float(total_capital),
            "available_capital": float(total_capital),
            "expiry_symbol": self._universe.expiry_symbol if self._universe else None,
            "instrument_count": sum(len(u.instruments) for u in self._universes.values()),
            "candles_refreshed": refreshed,
            "m1_count": len(self.market_data.candles(CandleInterval.M1)),
            "warnings": refresh_warnings,
        }

    def get_market_summary(self) -> dict[str, Any]:
        from zoneinfo import ZoneInfo

        from algoflat.market_session import session_label

        ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        ms = self.config.market_session
        session = session_label(ms)

        m5 = self.market_data.candles(CandleInterval.M5)
        features = self.orchestrator.features.compute_cached(max_age_sec=5.0)
        bias = features.bias_5m.value.upper()

        vwap = self.market_data.session_vwap_value
        spot = self.market_data.spot_ltp
        spot_vs_vwap: str | None = None
        if spot is not None and vwap is not None:
            if spot > vwap:
                spot_vs_vwap = "ABOVE"
            elif spot < vwap:
                spot_vs_vwap = "BELOW"
            else:
                spot_vs_vwap = "AT"

        decision = self.orchestrator.router.last_decision
        sym_cfg = symbols_for(self.config, self._active_underlying)
        commodity_rows: list[dict[str, Any]] = []
        if self._universes:
            commodity_rows = self._build_header_commodity_rows(
                default_step=float(sym_cfg.get("strike_step", 50)),
                default_band=float(strike_band_points(sym_cfg)),
                default_atm_steps=int(sym_cfg.get("atm_strike_steps", 5)),
                include_chain=False,
            )
        else:
            for row in list_underlyings(self.config):
                commodity_rows.append(
                    {
                        "underlying": str(row.get("symbol", "")).upper(),
                        "display_name": str(row.get("display_name", row.get("symbol", ""))),
                        "spot_ltp": None,
                        "atm_strike": None,
                        "expiry_symbol": None,
                    }
                )

        active_uni = self._universes.get(self._active_underlying)
        display_spot = self._header_spot(self._active_underlying, active_uni)
        is_expiry = False
        if active_uni and active_uni.expiry_symbol:
            from algoflat.contract_selector.expiry import parse_expiry_tag

            exp = parse_expiry_tag(active_uni.expiry_symbol)
            if exp is not None:
                is_expiry = exp == ist.date()
        extra = features.extra or {}
        fut_spot = extra.get("fut_spot")
        fut_vs_vwap: str | None = None
        if fut_spot is not None and vwap is not None:
            if float(fut_spot) > float(vwap):
                fut_vs_vwap = "ABOVE"
            elif float(fut_spot) < float(vwap):
                fut_vs_vwap = "BELOW"
            else:
                fut_vs_vwap = "AT"

        return {
            "underlying": self._active_underlying,
            "active_underlying": self._active_underlying,
            "commodities": commodity_rows,
            "spot_ltp": float(display_spot)
            if display_spot is not None
            else (float(spot) if spot is not None else None),
            "session_vwap": float(vwap) if vwap is not None else None,
            "spot_vs_vwap": spot_vs_vwap,
            "atm_strike": float(active_uni.atm_strike)
            if active_uni and active_uni.atm_strike is not None
            else None,
            "bias_5m": bias,
            "market_open": self._is_market_open(),
            "market_session": session,
            "strategy": (
                decision.selected_strategy
                if decision
                else self.config.strategy.get("active_scanner", "router")
            ),
            "regime": decision.regime.primary if decision and decision.regime else None,
            "confidence": decision.confidence if decision else None,
            **self._side_confidence_snapshot(),
            "trading_mode": get_execution_mode(),
            "ist_time": ist.isoformat(),
            **self._feed_status_snapshot(),
            "expiry_symbol": active_uni.expiry_symbol if active_uni else None,
            "is_expiry_day": is_expiry,
            "instrument_count": len(active_uni.instruments) if active_uni else 0,
            "m5_count": len(m5),
            "levels": getattr(self.orchestrator.features, "_last_levels", None),
            "cash_index_spot": extra.get("cash_index_spot"),
            "fut_spot": fut_spot,
            "basis_points": extra.get("basis_points"),
            "fut_vs_vwap": fut_vs_vwap,
            "spot_fut": extra.get("spot_fut"),
            "market_ticks": extra.get("market_ticks"),
            "market_intelligence": self.orchestrator.refresh_mi_snapshot(),
            "ml_shadow": self.orchestrator.last_ml_prediction,
        }

    async def live_broker_snapshot(self):
        await self._live_broker.connect()
        limits, day, open_pos = await self._live_broker.fetch_broker_snapshot()
        self._broker_open_positions = list(open_pos)
        return limits, day, open_pos

    async def live_account_limits(self):
        limits, _, _ = await self.live_broker_snapshot()
        return limits

    async def live_day_pnl(self):
        _, day, _ = await self.live_broker_snapshot()
        return day

    async def set_execution_mode_runtime(self, mode: str) -> dict[str, Any]:
        normalized = "live" if str(mode).lower() == "live" else "paper"
        if self.orchestrator.positions.open_count > 0:
            raise ValueError("Close all open positions before switching Paper / Live mode")
        if is_live_execution() or normalized == "paper":
            await self._refresh_broker_open_positions()
            await self.orchestrator.positions.reconcile_broker_holdings(
                self._broker_open_positions,
                option_data=self.option_data,
            )
        if normalized == "paper" and self._broker_open_positions:
            raise ValueError(
                f"Close all broker holdings on {broker_display_name()} before switching to Paper mode"
            )

        if normalized == "live":
            await self._live_broker.connect()
            await self._live_broker.get_account_limits()

        new_mode = await set_execution_mode(normalized)
        await self.journal.write_system_event(
            SystemEvent(
                event_type="execution_mode_changed",
                ts=datetime.now(tz=timezone.utc),
                severity="critical" if new_mode == "live" else "info",
                message=f"Execution mode set to {new_mode.upper()}",
                metadata={"trading_mode": new_mode},
            )
        )
        await self.journal.write_notification(
            "system",
            "critical" if new_mode == "live" else "info",
            f"{'LIVE' if new_mode == 'live' else 'Paper'} trading enabled",
            (
                "Real broker orders will be placed for new entries."
                if new_mode == "live"
                else "Orders are simulated against the paper ledger."
            ),
        )
        return {"ok": True, "trading_mode": new_mode}

    async def start(self) -> None:
        setup_logging(self.config.env.log_level, self.config.logging.get("format", "json"))
        await init_pool()
        await apply_migrations()
        await load_execution_mode_from_db(self.config.env.trading_mode)
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            if sym:
                await ensure_paper_account(
                    float(total_account_capital(self.config)),
                    underlying=risk_underlying_key(self.config),
                )
                break
        else:
            await ensure_paper_account(
                float(total_account_capital(self.config)),
                underlying=risk_underlying_key(self.config),
            )
        set_engine_state({"status": "running", "broker_connected": False})

        await self.journal.write_system_event(
            SystemEvent(
                event_type="SYSTEM_START",
                ts=datetime.now(tz=timezone.utc),
                severity="info",
                message="Trading engine started",
                metadata={"mode": get_execution_mode()},
            )
        )

        if not await self._has_api_credentials():
            provider = str(self.config.broker.get("provider", "upstox")).lower()
            logger.warning(
                "broker_api_key_missing",
                provider=provider,
                hint="Configure broker credentials in Settings",
            )
            await self.journal.write_notification(
                "system",
                "warning",
                broker_api_key_missing_title(provider),
                broker_api_key_missing_message(provider),
            )
            set_engine_state({"status": "standby"})
            return

        if not await self._ensure_session():
            return

        await self.broker.connect()
        set_engine_state({"broker_connected": True})
        from algoflat.notifications.telegram import bootstrap_telegram_from_env

        await bootstrap_telegram_from_env()
        await self._setup_market_data()
        if is_live_execution():
            await self._refresh_broker_open_positions()
        try:
            n = await self.orchestrator.positions.rehydrate_open_positions()
            if n:
                logger.info("open_positions_restored", count=n)
            if is_live_execution() and self._broker_open_positions:
                await self.orchestrator.positions.reconcile_broker_holdings(
                    self._broker_open_positions,
                    option_data=self.option_data,
                )
            if self.orchestrator.positions.open_count > 0:
                await self._ensure_holdings_on_websocket()
        except Exception:
            logger.exception("position_rehydrate_failed")

    async def stop(self) -> None:
        self.orchestrator.stop()
        await self._stop_subscription()
        await close_pool()
        set_engine_state({"status": "stopped", "broker_connected": False})


async def _run() -> None:
    engine = TradingEngineApp()
    set_engine_app(engine)
    config = uvicorn.Config(health_app, host="0.0.0.0", port=8001, log_level="info")
    server = uvicorn.Server(config)
    api_task = asyncio.create_task(server.serve())

    try:
        await engine.start()
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await engine.stop()
        server.should_exit = True
        await api_task


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("shutdown_signal_received")


if __name__ == "__main__":
    main()
