from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from algoflat.broker.base import BrokerAdapter
from algoflat.bus.event_bus import EventBus
from algoflat.config import AppConfig
from algoflat.market_data.vwap import session_vwap
from algoflat.models.events import Candle, CandleInterval, QuoteUpdate
from algoflat.symbols_util import is_sane_nifty_spot

logger = structlog.get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")

_CANDLE_TABLE = {
    CandleInterval.M1: "candles_1m",
    CandleInterval.M3: "candles_3m",
    CandleInterval.M5: "candles_5m",
    CandleInterval.M15: "candles_15m",
}

# Max age of latest 1m bar before we treat feed as stale (market hours).
_STALE_M1_SECONDS = 120

# Fresh install: expect at least a week of history before skipping auto-fetch.
FRESH_SETUP_LOOKBACK_DAYS = 7
FRESH_SETUP_MIN_DISTINCT_DAYS = 3


def needs_fresh_setup_backfill(coverage: dict[str, Any]) -> tuple[bool, str]:
    """True when DB lacks enough multi-day candle history for chart/strategy."""
    if int(coverage.get("count", 0)) == 0:
        return True, "no_candles_in_lookback"
    if int(coverage.get("distinct_days", 0)) < FRESH_SETUP_MIN_DISTINCT_DAYS:
        return True, "sparse_history"
    return False, "ok"


def session_start_utc(now: datetime | None = None) -> datetime:
    """Today's NSE cash open 09:15 IST as UTC (matches day_backtest)."""
    now = now or datetime.now(tz=timezone.utc)
    return (
        now.astimezone(IST)
        .replace(hour=9, minute=15, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )


def _empty_candle_store() -> dict[CandleInterval, list[Candle]]:
    return {i: [] for i in _CANDLE_TABLE}


class MarketDataEngine:
    def __init__(self, config: AppConfig, broker: BrokerAdapter, bus: EventBus) -> None:
        self._config = config
        self._broker = broker
        self._bus = bus
        self._spot_token = config.symbols["spot_token"]
        self._exchange = config.symbols["exchange_spot"]
        self._candles: dict[CandleInterval, list[Candle]] = _empty_candle_store()
        # Per-token candle cache for futures rollovers.
        self._candles_by_token: dict[str, dict[CandleInterval, list[Candle]]] = {}
        self._refresh_minute_by_token: dict[str, tuple[int, int]] = {}
        self._spot_ltp: Decimal | None = None
        self._cash_index_ltp: Decimal | None = None
        self._last_quote_ts: datetime | None = None
        self._last_candle_refresh_minute: tuple[int, int] | None = None
        self._last_successful_refresh_ts: datetime | None = None
        self._last_refresh_ok: bool = False
        # Intra-minute spot bar built from quotes (aligns 5s scans with day_backtest forming M1).
        self._forming_m1_enabled = bool(
            (config.runtime or {}).get("forming_m1_from_quotes", True)
        )
        self._forming_m1_min_interval = float(
            (config.runtime or {}).get("forming_m1_min_interval_seconds", 5)
        )
        self._last_forming_m1_update: datetime | None = None

    def _persist_active_candles(self) -> None:
        if not self._spot_token:
            return
        self._candles_by_token[self._spot_token] = {
            interval: list(rows) for interval, rows in self._candles.items()
        }
        if self._last_candle_refresh_minute is not None:
            self._refresh_minute_by_token[self._spot_token] = self._last_candle_refresh_minute

    def _load_candles_for_token(self, spot_token: str) -> None:
        cached = self._candles_by_token.get(spot_token)
        if cached is None:
            self._candles = _empty_candle_store()
        else:
            self._candles = {interval: list(rows) for interval, rows in cached.items()}
        self._last_candle_refresh_minute = self._refresh_minute_by_token.get(spot_token)
        self._last_refresh_ok = bool(self._candles.get(CandleInterval.M1))

    async def hydrate_from_db(self, conn: Any, spot_token: str) -> dict[str, int]:
        """Load today's session candles from Postgres into memory."""
        start = session_start_utc()
        loaded: dict[str, int] = {}
        for interval in _CANDLE_TABLE:
            table = _CANDLE_TABLE[interval]
            rows = await conn.fetch(
                f"""
                SELECT ts, open, high, low, close, volume
                FROM {table}
                WHERE instrument_token = $1 AND ts >= $2
                ORDER BY ts
                """,
                spot_token,
                start,
            )
            candles = [
                Candle(
                    instrument_token=spot_token,
                    ts=r["ts"],
                    open=r["open"],
                    high=r["high"],
                    low=r["low"],
                    close=r["close"],
                    volume=r["volume"],
                    interval=interval,
                )
                for r in rows
            ]
            if candles:
                self._candles[interval] = candles
            loaded[interval.value] = len(candles)
        if any(loaded.values()):
            self._persist_active_candles()
            self._last_refresh_ok = bool(self._candles[CandleInterval.M1])
        return loaded

    async def candles_from_db(
        self,
        conn: Any,
        spot_token: str,
        interval: CandleInterval,
        start: datetime,
        end: datetime | None = None,
    ) -> list[Candle]:
        """Load candles for a token/interval from Postgres (multi-day chart history)."""
        return await self.candles_from_db_for_tokens(
            conn, [spot_token], interval, start, end
        )

    async def candles_from_db_for_tokens(
        self,
        conn: Any,
        tokens: list[str],
        interval: CandleInterval,
        start: datetime,
        end: datetime | None = None,
    ) -> list[Candle]:
        """Load candles for one or more tokens (stitched chart history across rollovers)."""
        token_list = [t for t in dict.fromkeys(tokens) if t]
        if not token_list:
            return []
        table = _CANDLE_TABLE[interval]
        if end is None:
            rows = await conn.fetch(
                f"""
                SELECT instrument_token, ts, open, high, low, close, volume
                FROM {table}
                WHERE instrument_token = ANY($1::text[]) AND ts >= $2
                ORDER BY ts
                """,
                token_list,
                start,
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT instrument_token, ts, open, high, low, close, volume
                FROM {table}
                WHERE instrument_token = ANY($1::text[]) AND ts >= $2 AND ts <= $3
                ORDER BY ts
                """,
                token_list,
                start,
                end,
            )
        return [
            Candle(
                instrument_token=r["instrument_token"],
                ts=r["ts"],
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
                interval=interval,
            )
            for r in rows
        ]

    @staticmethod
    def merge_candles(*sources: list[Candle]) -> list[Candle]:
        """Merge candle lists by timestamp; later sources override earlier ones."""
        by_ts: dict[datetime, Candle] = {}
        for rows in sources:
            for candle in rows:
                by_ts[candle.ts] = candle
        return sorted(by_ts.values(), key=lambda c: c.ts)

    @staticmethod
    def stitch_contract_candles(
        candles_by_token: dict[str, list[Candle]],
        *,
        preferred_token: str | None = None,
    ) -> list[Candle]:
        """Stitch rolled futures; preferred (front-month) token wins on overlap.

        Far months (Sep/Oct) trade alongside the front month and must not replace
        it. Expired contracts only fill timestamps the preferred series lacks.
        """
        if not candles_by_token:
            return []
        if len(candles_by_token) == 1:
            return sorted(next(iter(candles_by_token.values())), key=lambda c: c.ts)

        def _token_rank(token: str) -> tuple[int, datetime, str]:
            series = candles_by_token[token]
            last_ts = max(c.ts for c in series)
            # preferred sorts last so it overwrites far-month / expired overlap
            pref = 1 if preferred_token and token == preferred_token else 0
            return (pref, last_ts, token)

        by_ts: dict[datetime, Candle] = {}
        for token in sorted(candles_by_token.keys(), key=_token_rank):
            for candle in sorted(candles_by_token[token], key=lambda c: c.ts):
                by_ts[candle.ts] = candle
        return sorted(by_ts.values(), key=lambda c: c.ts)

    async def chart_tokens_from_db(
        self,
        conn: Any,
        *,
        seed_tokens: list[str],
        start: datetime,
        ref_token: str,
    ) -> list[str]:
        """Discover expired futures tokens in DB that match the reference price scale."""
        rows = await conn.fetch(
            """
            SELECT instrument_token, AVG(close)::float AS avg_close
            FROM candles_1m
            WHERE ts >= $1
            GROUP BY instrument_token
            """,
            start,
        )
        tokens = list(dict.fromkeys([t for t in seed_tokens if t]))
        ref_avg: float | None = None
        for row in rows:
            if row["instrument_token"] == ref_token:
                ref_avg = float(row["avg_close"] or 0)
                break
        if ref_avg is None or ref_avg <= 0:
            return tokens
        for row in rows:
            tok = str(row["instrument_token"])
            if tok in tokens:
                continue
            avg = float(row["avg_close"] or 0)
            if avg <= 0:
                continue
            if abs(avg - ref_avg) / ref_avg <= 0.15:
                tokens.append(tok)
        return tokens

    async def session_candle_stats_from_db(
        self, conn: Any, spot_token: str
    ) -> dict[str, dict[str, Any]]:
        """Per-interval row count and latest ts for today's session (IST open)."""
        start = session_start_utc()
        stats: dict[str, dict[str, Any]] = {}
        for interval in _CANDLE_TABLE:
            table = _CANDLE_TABLE[interval]
            row = await conn.fetchrow(
                f"""
                SELECT COUNT(*)::int AS cnt, MAX(ts) AS latest
                FROM {table}
                WHERE instrument_token = $1 AND ts >= $2
                """,
                spot_token,
                start,
            )
            stats[interval.value] = {
                "count": int(row["cnt"] or 0),
                "latest": row["latest"],
            }
        return stats

    async def historical_coverage_from_db(
        self,
        conn: Any,
        spot_token: str,
        *,
        lookback_days: int = FRESH_SETUP_LOOKBACK_DAYS,
    ) -> dict[str, Any]:
        """Row count and trading-day coverage for multi-day history checks."""
        start = datetime.now(tz=timezone.utc) - timedelta(days=max(1, lookback_days))
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)::int AS cnt,
                COUNT(DISTINCT (ts AT TIME ZONE 'Asia/Kolkata')::date)::int AS distinct_days,
                MIN(ts) AS earliest,
                MAX(ts) AS latest
            FROM candles_1m
            WHERE instrument_token = $1 AND ts >= $2
            """,
            spot_token,
            start,
        )
        return {
            "lookback_days": lookback_days,
            "count": int(row["cnt"] or 0),
            "distinct_days": int(row["distinct_days"] or 0),
            "earliest": row["earliest"],
            "latest": row["latest"],
        }

    def needs_broker_candle_sync(
        self,
        db_stats: dict[str, dict[str, Any]],
        *,
        market_open: bool,
    ) -> tuple[bool, str]:
        """Decide if Flattrade candle fetch is required (call after hydrate_from_db)."""
        if not self.candles(CandleInterval.M1):
            if db_stats.get("1m", {}).get("count", 0) > 0:
                return True, "db_hydrate_failed"
            return True, "empty"

        if self.candles_stale() and market_open:
            return True, "memory_stale"

        latest_mem = self.latest_candle_ts(CandleInterval.M1)
        latest_db = db_stats.get("1m", {}).get("latest")
        if latest_db and latest_mem and latest_db > latest_mem:
            return True, "db_ahead_of_memory"

        if market_open and latest_mem:
            age = (datetime.now(tz=timezone.utc) - latest_mem).total_seconds()
            if age > _STALE_M1_SECONDS:
                return True, "bar_stale"

        return False, "ok"

    def set_spot_context(self, *, exchange: str, spot_token: str) -> None:
        self._exchange = exchange
        if spot_token and spot_token != self._spot_token:
            self._persist_active_candles()
            self._spot_token = spot_token
            self._load_candles_for_token(spot_token)
            self._spot_ltp = None
            logger.info(
                "spot_context_switched",
                spot_token=spot_token,
                m1_cached=len(self._candles.get(CandleInterval.M1) or []),
            )

    @property
    def spot_ltp(self) -> Decimal | None:
        return self._spot_ltp

    @property
    def cash_index_ltp(self) -> Decimal | None:
        """NSE cash index LTP (NIFTY 50) when dual spot mode is active."""
        return self._cash_index_ltp

    def set_cash_index_ltp(self, ltp: Decimal | None) -> None:
        if ltp is None:
            self._cash_index_ltp = None
            return
        if is_sane_nifty_spot(ltp):
            self._cash_index_ltp = ltp

    @property
    def session_vwap_value(self) -> Decimal | None:
        return session_vwap(self._candles[CandleInterval.M1])

    @property
    def last_quote_ts(self) -> datetime | None:
        return self._last_quote_ts

    @property
    def last_refresh_ok(self) -> bool:
        return self._last_refresh_ok

    def candles(self, interval: CandleInterval) -> list[Candle]:
        return list(self._candles[interval])

    def candles_for_token(self, spot_token: str, interval: CandleInterval) -> list[Candle]:
        """Cached session bars for a futures/index/option token."""
        cached = self._candles_by_token.get(spot_token) or {}
        return list(cached.get(interval) or [])

    @staticmethod
    def _minute_open_utc(ts: datetime) -> datetime:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        return ts.replace(second=0, microsecond=0)

    def apply_spot_ltp_to_forming_m1(self, ltp: Decimal, ts: datetime) -> None:
        """Update/create the current-minute spot M1 bar from live LTP (backtest parity).

        REST refresh remains source of truth for closed bars; this only keeps the
        in-progress minute aligned with tick LTP between minute fetches.
        """
        if not self._forming_m1_enabled or ltp is None:
            return
        if self._forming_m1_min_interval > 0 and self._last_forming_m1_update is not None:
            age = (ts - self._last_forming_m1_update).total_seconds()
            # Always allow first tick of a new minute so the bar is created.
            rows = list(self._candles.get(CandleInterval.M1) or [])
            minute_open = self._minute_open_utc(ts)
            same_minute = bool(
                rows and self._minute_open_utc(rows[-1].ts) == minute_open
            )
            if same_minute and age < self._forming_m1_min_interval:
                return
        minute_open = self._minute_open_utc(ts)
        rows = list(self._candles.get(CandleInterval.M1) or [])
        if rows:
            last_open = self._minute_open_utc(rows[-1].ts)
            if last_open == minute_open:
                last = rows[-1]
                rows[-1] = last.model_copy(
                    update={
                        "high": max(last.high, ltp),
                        "low": min(last.low, ltp),
                        "close": ltp,
                    }
                )
                self._candles[CandleInterval.M1] = rows
                self._last_forming_m1_update = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
                return
            if last_open > minute_open:
                return
        open_px = rows[-1].close if rows else ltp
        forming = Candle(
            instrument_token=str(self._spot_token or ""),
            ts=minute_open,
            open=open_px,
            high=max(open_px, ltp),
            low=min(open_px, ltp),
            close=ltp,
            volume=0,
            interval=CandleInterval.M1,
        )
        self._candles[CandleInterval.M1] = rows + [forming]
        self._last_forming_m1_update = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    def store_candles_for_token(
        self,
        token: str,
        interval: CandleInterval,
        rows: list[Candle],
    ) -> None:
        """Merge session candles into the per-token cache (options, rolled FUT)."""
        if not token:
            return
        bucket = self._candles_by_token.setdefault(token, _empty_candle_store())
        bucket[interval] = self.merge_candles(bucket.get(interval) or [], rows)

    async def ensure_option_token_m1(
        self,
        token: str,
        *,
        exchange: str = "NFO",
        min_bars: int = 5,
        force: bool = False,
    ) -> list[Candle]:
        """Ensure the candidate option has enough 1m bars for chart confirm.

        Band refresh is throttled once/minute across all strikes; validation must
        not depend on an empty cache. Force-fetches this token when short.
        """
        tok = str(token or "")
        if not tok:
            return []
        existing = self.candles_for_token(tok, CandleInterval.M1)
        if not force and len(existing) >= max(1, min_bars):
            return existing
        now = datetime.now(tz=timezone.utc)
        start = session_start_utc(now)
        try:
            rows = await self._broker.get_candles(
                exchange,
                tok,
                CandleInterval.M1,
                start,
                now,
            )
        except Exception:
            logger.exception(
                "option_candidate_candle_fetch_failed",
                token=tok,
                exchange=exchange,
            )
            return existing
        rows = sorted(rows, key=lambda c: c.ts)
        if rows:
            self.store_candles_for_token(tok, CandleInterval.M1, rows)
            logger.info(
                "option_candidate_m1_ready",
                token=tok,
                bars=len(rows),
                min_bars=min_bars,
            )
        return self.candles_for_token(tok, CandleInterval.M1)

    async def refresh_option_band_candles(
        self,
        instruments: list[Any],
        *,
        exchange: str | None = None,
        force: bool = False,
    ) -> int:
        """Fetch today's 1m option bars for ATM band tokens (once per IST minute)."""
        ist = datetime.now(IST)
        minute_key = (ist.hour, ist.minute)
        if not force and self._refresh_minute_by_token.get("__options__") == minute_key:
            return 0

        now = datetime.now(tz=timezone.utc)
        start = session_start_utc(now)
        loaded = 0
        for inst in instruments:
            if getattr(inst, "option_type", None) not in ("CE", "PE"):
                continue
            tok = str(getattr(inst, "token", "") or "")
            if not tok:
                continue
            exch = exchange or getattr(inst, "exchange", None) or "NFO"
            try:
                rows = await self._broker.get_candles(
                    exch,
                    tok,
                    CandleInterval.M1,
                    start,
                    now,
                )
            except Exception:
                logger.exception("option_band_candle_fetch_failed", token=tok, exchange=exch)
                continue
            rows = sorted(rows, key=lambda c: c.ts)
            if rows:
                self.store_candles_for_token(tok, CandleInterval.M1, rows)
                loaded += 1
        if loaded:
            self._refresh_minute_by_token["__options__"] = minute_key
        return loaded

    def latest_candle_ts(self, interval: CandleInterval = CandleInterval.M1) -> datetime | None:
        rows = self._candles.get(interval) or []
        if not rows:
            return None
        return rows[-1].ts

    def candles_stale(self, *, max_age_seconds: int = _STALE_M1_SECONDS) -> bool:
        """True when latest 1m bar is too old relative to now (during session)."""
        latest = self.latest_candle_ts(CandleInterval.M1)
        if latest is None:
            return True
        # Broker bar timestamps are candle *open* time; treat bar as fresh until close.
        effective_end = latest
        if effective_end.tzinfo is None:
            effective_end = effective_end.replace(tzinfo=timezone.utc)
        else:
            effective_end = effective_end.astimezone(timezone.utc)
        effective_end += timedelta(minutes=1)
        age = (datetime.now(tz=timezone.utc) - effective_end).total_seconds()
        return age > max_age_seconds

    async def backfill_contract_history(
        self,
        *,
        exchange: str,
        tokens: list[str],
        days: int = 30,
    ) -> tuple[dict[str, int], list[Candle]]:
        """Fetch multi-day candles from broker for rolled contracts."""
        now = datetime.now(tz=timezone.utc)
        start = now - timedelta(days=max(1, days))
        added: dict[str, int] = {i.value: 0 for i in _CANDLE_TABLE}
        all_rows: list[Candle] = []
        for spot_token in dict.fromkeys(t for t in tokens if t):
            for interval in CandleInterval.broker_native():
                try:
                    rows = await self._broker.get_candles(
                        exchange, spot_token, interval, start, now
                    )
                except Exception:
                    logger.exception(
                        "history_backfill_failed",
                        token=spot_token,
                        interval=interval.value,
                    )
                    continue
                rows = sorted(rows, key=lambda c: c.ts)
                if not rows:
                    continue
                all_rows.extend(rows)
                added[interval.value] += len(rows)
                if spot_token == self._spot_token:
                    merged = self.merge_candles(self._candles.get(interval, []), rows)
                    self._candles[interval] = merged
                    self._persist_active_candles()
        if all_rows:
            self._last_refresh_ok = bool(self._candles.get(CandleInterval.M1))
        return added, all_rows

    async def backfill_today(self) -> None:
        now = datetime.now(tz=timezone.utc)
        start = session_start_utc(now)
        any_ok = False
        for interval in CandleInterval.broker_native():
            try:
                rows = await self._broker.get_candles(
                    self._exchange,
                    self._spot_token,
                    interval,
                    start,
                    now,
                )
            except Exception:
                logger.exception("candles_backfill_failed", interval=interval.value)
                continue
            rows = sorted(rows, key=lambda c: c.ts)
            if not rows:
                logger.warning(
                    "candles_backfill_empty",
                    interval=interval.value,
                    spot_token=self._spot_token,
                    cached=len(self._candles.get(interval) or []),
                )
                continue
            self._candles[interval] = rows
            any_ok = True
            logger.info(
                "candles_backfilled",
                interval=interval.value,
                count=len(rows),
                first=rows[0].ts.isoformat(),
                last=rows[-1].ts.isoformat(),
            )
        if any_ok:
            self._materialize_m15()
            self._persist_active_candles()
            self._last_refresh_ok = bool(self._candles.get(CandleInterval.M1))
            if self._last_refresh_ok:
                self._last_successful_refresh_ts = now

    async def refresh_session_candles(self, *, force: bool = False) -> bool:
        """Refresh intraday candles so setup/trigger logic sees new bars.

        Returns True when in-memory candles were updated from a successful fetch.
        On empty/failed fetch, keeps prior bars and does NOT mark the IST minute
        as done so the next scan retries within the same minute.
        """
        ist = datetime.now(IST)
        minute_key = (ist.hour, ist.minute)
        if not force and self._last_candle_refresh_minute == minute_key:
            return False

        now = datetime.now(tz=timezone.utc)
        start = session_start_utc(now)
        fetched: dict[CandleInterval, list[Candle]] = {}
        any_ok = False
        for interval in CandleInterval.broker_native():
            try:
                rows = await self._broker.get_candles(
                    self._exchange,
                    self._spot_token,
                    interval,
                    start,
                    now,
                )
            except Exception:
                logger.exception("candle_fetch_failed", interval=interval.value)
                rows = []
            rows = sorted(rows, key=lambda c: c.ts)
            fetched[interval] = rows
            if rows:
                any_ok = True

        if not any_ok:
            # Keep stale bars; allow retry this same minute.
            self._last_refresh_ok = False
            logger.warning(
                "candle_refresh_empty",
                force=force,
                m1_cached=len(self._candles[CandleInterval.M1]),
                stale=self.candles_stale(),
            )
            return False

        for interval, rows in fetched.items():
            if rows:
                self._candles[interval] = rows

        self._materialize_m15()
        self._last_candle_refresh_minute = minute_key
        self._last_successful_refresh_ts = now
        self._last_refresh_ok = True
        self._persist_active_candles()
        logger.info(
            "candles_refreshed",
            m1=len(self._candles[CandleInterval.M1]),
            m3=len(self._candles[CandleInterval.M3]),
            m5=len(self._candles[CandleInterval.M5]),
            m15=len(self._candles.get(CandleInterval.M15) or []),
            last_m1=self.latest_candle_ts().isoformat() if self.latest_candle_ts() else None,
            stale=self.candles_stale(),
        )
        return True

    def _materialize_m15(self) -> None:
        from algoflat.features.indicators import aggregate_from_m5

        m5 = self._candles.get(CandleInterval.M5) or []
        if m5:
            self._candles[CandleInterval.M15] = aggregate_from_m5(m5, 15)

    def _reference_spot_from_candles(self) -> Decimal | None:
        m1 = self._candles.get(CandleInterval.M1) or []
        if m1:
            return m1[-1].close
        return None

    def _spot_quote_trusted(self, ltp: Decimal) -> bool:
        if not is_sane_nifty_spot(ltp):
            return False
        ref = self._reference_spot_from_candles()
        if ref is None or ref <= 0:
            return True
        if abs(ltp - ref) / ref > Decimal("0.025"):
            logger.warning(
                "spot_quote_rejected_vs_candles",
                ltp=str(ltp),
                candle_close=str(ref),
            )
            return False
        return True

    def seed_spot_from_candles(self) -> Decimal | None:
        ref = self._reference_spot_from_candles()
        if ref is not None:
            self.set_spot_ltp(ref, ts=datetime.now(tz=timezone.utc), update_forming=False)
        return ref

    def set_spot_ltp(
        self,
        ltp: Decimal,
        *,
        ts: datetime | None = None,
        update_forming: bool = True,
    ) -> None:
        """Set strategy spot LTP; optionally keep forming M1 in sync (scan parity)."""
        self._spot_ltp = ltp
        if update_forming:
            self.apply_spot_ltp_to_forming_m1(
                ltp, ts or datetime.now(tz=timezone.utc)
            )

    async def on_quote(self, quote: QuoteUpdate) -> None:
        self._last_quote_ts = quote.ts
        if quote.instrument_token == self._spot_token and quote.ltp is not None:
            if self._spot_quote_trusted(quote.ltp):
                self.set_spot_ltp(quote.ltp, ts=quote.ts, update_forming=True)
        await self._bus.publish("quote_update", quote)

    def candle_table(self, interval: CandleInterval) -> str:
        return _CANDLE_TABLE[interval]
