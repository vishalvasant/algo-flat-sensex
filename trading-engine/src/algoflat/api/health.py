from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from algoflat.broker.upstox_auth import resolve_session as resolve_upstox_session
from algoflat.broker.upstox_credentials import (
    load_upstox_config,
    load_upstox_credentials_status,
    save_upstox_credentials,
)
from algoflat.config import get_config
from algoflat.db.connection import get_pool
from algoflat.models.events import CandleInterval
from algoflat.db.paper_account import ensure_paper_account
from algoflat.runtime.trading_mode import get_execution_mode, is_live_execution
from algoflat.notifications.telegram import get_telegram_notifier, maybe_send_telegram_alert
from algoflat.symbols_util import list_underlyings

app = FastAPI(title="Algo-Flat Trading Engine", version="0.1.0")
_engine_state: dict[str, Any] = {"status": "starting"}


def set_engine_state(state: dict[str, Any]) -> None:
    _engine_state.update(state)


def get_engine_state() -> dict[str, Any]:
    return dict(_engine_state)


_engine_app: Any = None
_market_summary_cache: dict[str, Any] | None = None
_market_summary_cache_at: float = 0.0
_market_summary_lock = asyncio.Lock()
_MARKET_SUMMARY_TTL_SEC = 4.0


def set_engine_app(app: Any) -> None:
    global _engine_app
    _engine_app = app


@app.get("/health")
async def health() -> dict[str, Any]:
    db_ok = False
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False

    cfg = get_config()
    provider = str(cfg.broker.get("provider", "upstox")).lower()
    session_info = None
    credentials_info: dict[str, Any] = {}
    if provider == "flattrade":
        from algoflat.broker.auth import resolve_session as resolve_ft_session
        from algoflat.broker.credentials import load_flattrade_config

        ft_cfg = await load_flattrade_config()
        session = resolve_ft_session(ft_cfg)
        credentials_info = {
            "provider": "flattrade",
            "configured": ft_cfg.has_api_credentials(),
            "has_auto_login": ft_cfg.has_auto_login(),
        }
    else:
        ux_cfg = await load_upstox_config()
        session = resolve_upstox_session(ux_cfg)
        credentials_info = {
            "provider": "upstox",
            "configured": ux_cfg.has_api_credentials(),
            "has_access_token": ux_cfg.has_access_token(),
        }
    if session:
        session_info = {
            "user_id": getattr(session, "user_id", None),
            "source": session.source,
            "expires_at": session.expires_at.isoformat(),
            "valid": session.is_valid,
        }

    market_open = False
    market_session = "CLOSED"
    ist_time = datetime.now(IST).isoformat()
    if _engine_app is not None:
        from algoflat.market_session import session_label

        market_open = _engine_app._is_market_open()
        market_session = session_label(_engine_app.config.market_session)
        ist_time = datetime.now(IST).isoformat()

    return {
        "status": _engine_state.get("status", "unknown"),
        "trading_mode": get_execution_mode(),
        "db_ok": db_ok,
        "broker_connected": _engine_state.get("broker_connected", False),
        "market_open": market_open,
        "market_session": market_session,
        "ist_time": ist_time,
        "broker_session": session_info,
        "broker_credentials": credentials_info,
        # Back-compat for UI still reading flattrade_* keys
        "flattrade_session": session_info,
        "flattrade_credentials": credentials_info,
        "spot_ltp": _engine_state.get("spot_ltp"),
        "instrument_count": _engine_state.get("instrument_count", 0),
        "last_quote_ts": _engine_state.get("last_quote_ts"),
        "ts": datetime.now(tz=timezone.utc).isoformat(),
    }


@app.post("/control/kill-switch")
async def kill_switch(enabled: bool = True) -> dict[str, Any]:
    from zoneinfo import ZoneInfo

    pool = get_pool()
    await ensure_paper_account()
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO daily_risk_state (
                trade_date, kill_switch, entries_blocked, block_reason,
                starting_capital, available_capital, deployed_capital
            )
            VALUES ($1, $2, $3, $4, 50000, 50000, 0)
            ON CONFLICT (trade_date) DO UPDATE SET
                kill_switch = EXCLUDED.kill_switch,
                entries_blocked = EXCLUDED.entries_blocked,
                block_reason = EXCLUDED.block_reason,
                updated_at = now()
            """,
            today,
            enabled,
            enabled,
            "manual_kill_switch" if enabled else None,
        )
        await conn.execute(
            """
            INSERT INTO notifications (type, severity, title, message)
            VALUES ($1, $2, $3, $4)
            """,
            "kill_switch",
            "critical" if enabled else "info",
            "Kill switch updated",
            f"Kill switch {'enabled' if enabled else 'disabled'}",
        )
    await maybe_send_telegram_alert(
        type_="kill_switch",
        severity="critical" if enabled else "info",
        title="Kill switch updated",
        message=f"Kill switch {'enabled' if enabled else 'disabled'}",
    )
    _engine_state["kill_switch"] = enabled
    return {"kill_switch": enabled}


@app.post("/control/trading-mode")
async def trading_mode(mode: str) -> dict[str, Any]:
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    normalized = str(mode).lower()
    if normalized not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be paper or live")
    try:
        return await _engine_app.set_execution_mode_runtime(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/control/auto-trade")
async def auto_trade(enabled: bool = True) -> dict[str, Any]:
    """Pause/resume new entries (scans + decision logs continue)."""
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    snap = await _engine_app.orchestrator.risk.set_auto_trade(enabled)
    return {
        "auto_trade_enabled": snap.auto_trade_enabled,
        "kill_switch": snap.kill_switch,
        "entries_blocked": snap.entries_blocked,
        "block_reason": snap.block_reason,
    }


@app.post("/control/sync-missing")
async def sync_missing() -> dict[str, Any]:
    """Pull only missing universe/candles/quotes from Flattrade into DB."""
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    try:
        return await _engine_app.sync_missing_data()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/control/paper-trial")
async def paper_trial(side: str = "CE") -> dict[str, Any]:
    """Paper-only E2E: 1-lot ATM entry, Telegram alerts, immediate exit."""
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    result = await _engine_app.paper_trial_roundtrip(side=side)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/control/live-trial")
async def live_trial(side: str = "CE") -> dict[str, Any]:
    """Live E2E: 1-lot ATM entry via Flattrade, Telegram alerts, immediate exit."""
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    result = await _engine_app.live_trial_roundtrip(side=side)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


class FlattradeCredentialsBody(BaseModel):
    user_id: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    password: str | None = None
    totp_secret: str | None = None
    redirect_url: str | None = None
    access_token: str | None = None
    analytic_token: str | None = None


@app.get("/control/flattrade/credentials")
async def flattrade_credentials_get() -> dict[str, Any]:
    cfg = get_config()
    if str(cfg.broker.get("provider", "upstox")).lower() == "upstox":
        return await load_upstox_credentials_status()
    from algoflat.broker.credentials import load_flattrade_credentials_status

    return await load_flattrade_credentials_status()


@app.put("/control/flattrade/credentials")
async def flattrade_credentials_put(body: FlattradeCredentialsBody) -> dict[str, Any]:
    cfg = get_config()
    try:
        if str(cfg.broker.get("provider", "upstox")).lower() == "upstox":
            return await save_upstox_credentials(
                body.model_dump(exclude_none=True),
            )
        from algoflat.broker.credentials import save_flattrade_credentials

        return await save_flattrade_credentials(
            body.model_dump(exclude_none=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class UpstoxCredentialsBody(BaseModel):
    api_key: str | None = None
    api_secret: str | None = None
    redirect_url: str | None = None
    access_token: str | None = None
    analytic_token: str | None = None


@app.get("/control/upstox/credentials")
async def upstox_credentials_get() -> dict[str, Any]:
    return await load_upstox_credentials_status()


@app.put("/control/upstox/credentials")
async def upstox_credentials_put(body: UpstoxCredentialsBody) -> dict[str, Any]:
    try:
        return await save_upstox_credentials(body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/control/telegram/status")
async def telegram_status() -> dict[str, Any]:
    notifier = get_telegram_notifier()
    await notifier.load_chat_id_from_db()
    bot = await notifier.get_bot_info()
    bot_user = (bot.get("result") or {}) if bot.get("ok") else {}
    return {
        **notifier.status(),
        "bot": {
            "ok": bool(bot.get("ok")),
            "username": bot_user.get("username"),
            "name": bot_user.get("first_name"),
        },
    }


@app.post("/control/telegram/link")
async def telegram_link() -> dict[str, Any]:
    """Link Telegram chat after user taps Start on the bot."""
    notifier = get_telegram_notifier()
    if not notifier.configured:
        raise HTTPException(status_code=400, detail="TELEGRAM_BOT_TOKEN not configured")
    chat_id = await notifier.discover_chat_from_updates()
    if not chat_id:
        bot = await notifier.get_bot_info()
        username = (bot.get("result") or {}).get("username")
        hint = (
            f"Open Telegram, search @{username}, tap Start, then call this endpoint again."
            if username
            else "Open Telegram, start your bot, then call this endpoint again."
        )
        raise HTTPException(status_code=404, detail=hint)
    return {"ok": True, "chat_id": chat_id, **notifier.status()}


@app.post("/control/telegram/test")
async def telegram_test() -> dict[str, Any]:
    notifier = get_telegram_notifier()
    result = await notifier.send_test()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@app.get("/decision-logs")
async def decision_logs(
    limit: int = 25,
    offset: int = 0,
    event_type: str | None = None,
) -> dict[str, Any]:
    """Strategy decision + entry skip logs for the Decision Logs UI (paginated)."""
    allowed = {"strategy_decision", "entry_skipped", "entry_confirm_wait", "manual_sync"}
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    if event_type and event_type not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid event_type: {event_type}")
    types = [event_type] if event_type else sorted(allowed)
    pool = get_pool()
    async with pool.acquire() as conn:
        total = int(
            await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM system_events
                WHERE event_type = ANY($1::text[])
                """,
                types,
            )
            or 0
        )
        rows = await conn.fetch(
            """
            SELECT id, ts, event_type, severity, message, metadata
            FROM system_events
            WHERE event_type = ANY($1::text[])
            ORDER BY ts DESC
            LIMIT $2 OFFSET $3
            """,
            types,
            limit,
            offset,
        )
        count_today = int(
            await conn.fetchval(
                """
                SELECT COUNT(*) FROM system_events
                WHERE event_type = 'strategy_decision'
                  AND (ts AT TIME ZONE 'Asia/Kolkata')::date
                      = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                """
            )
            or 0
        )
    events = []
    for r in rows:
        meta = r["metadata"]
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        events.append(
            {
                "id": str(r["id"]),
                "ts": r["ts"].isoformat(),
                "event_type": r["event_type"],
                "severity": r["severity"],
                "message": r["message"],
                "metadata": meta or {},
            }
        )
    scan_interval = 10
    market_open = False
    market_session = "CLOSED"
    ist_time = datetime.now(IST).isoformat()
    engine_status = _engine_state.get("status", "unknown")
    last_scan_at: str | None = None
    if _engine_app is not None:
        from algoflat.market_session import session_label

        scan_interval = int(_engine_app.config.runtime.get("scan_interval_seconds", 10))
        market_open = _engine_app._is_market_open()
        market_session = session_label(_engine_app.config.market_session)
        engine_status = _engine_state.get("status", "unknown")
        completed = _engine_app.orchestrator.last_scan_completed_at
        if completed is not None:
            last_scan_at = completed.astimezone(IST).isoformat()
    return {
        "scan_interval_seconds": scan_interval,
        "decisions_today": count_today,
        "market_open": market_open,
        "market_session": market_session,
        "ist_time": ist_time,
        "engine_status": engine_status,
        "last_scan_at": last_scan_at,
        "total": total,
        "limit": limit,
        "offset": offset,
        "events": events,
    }


@app.get("/decision-logs/summary")
async def decision_logs_summary(minutes: int = 60) -> dict[str, Any]:
    """Aggregate NO_TRADE / regime / confidence reasons for debugging."""
    minutes = max(5, min(minutes, 480))
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
              COALESCE(
                metadata->>'selected_reason',
                metadata->>'selected_strategy',
                message
              ) AS reason,
              COALESCE(metadata->>'scan_underlying', '—') AS underlying,
              COALESCE(metadata->'regime'->>'primary', '—') AS regime,
              COUNT(*)::int AS count,
              MAX(ts) AS last_seen
            FROM system_events
            WHERE event_type = 'strategy_decision'
              AND ts > NOW() - ($1::int * INTERVAL '1 minute')
            GROUP BY 1, 2, 3
            ORDER BY count DESC, last_seen DESC
            LIMIT 40
            """,
            minutes,
        )
        latest = await conn.fetchrow(
            """
            SELECT ts, metadata, message
            FROM system_events
            WHERE event_type = 'strategy_decision'
            ORDER BY ts DESC
            LIMIT 1
            """
        )
        stale_count = int(
            await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM system_events
                WHERE event_type = 'strategy_decision'
                  AND ts > NOW() - ($1::int * INTERVAL '1 minute')
                  AND (
                    metadata->>'selected_reason' = 'stale_candle_feed'
                    OR message = 'NO_TRADE'
                       AND metadata->>'selected_reason' = 'stale_candle_feed'
                  )
                """,
                minutes,
            )
            or 0
        )
    latest_meta: dict[str, Any] = {}
    if latest:
        raw = latest["metadata"]
        if isinstance(raw, str):
            try:
                latest_meta = json.loads(raw)
            except Exception:
                latest_meta = {}
        elif isinstance(raw, dict):
            latest_meta = raw

    return {
        "window_minutes": minutes,
        "stale_feed_decisions": stale_count,
        "latest_scan": {
            "ts": latest["ts"].isoformat() if latest else None,
            "underlying": latest_meta.get("scan_underlying"),
            "strategy": latest_meta.get("selected_strategy"),
            "reason": latest_meta.get("selected_reason") or (latest["message"] if latest else None),
            "confidence": latest_meta.get("confidence"),
            "regime": (latest_meta.get("regime") or {}).get("primary")
            if isinstance(latest_meta.get("regime"), dict)
            else None,
            "trade_allowed": latest_meta.get("trade_allowed"),
            "candles_stale": latest_meta.get("candles_stale"),
        },
        "reasons": [
            {
                "reason": r["reason"],
                "underlying": r["underlying"],
                "regime": r["regime"],
                "count": r["count"],
                "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            }
            for r in rows
        ],
    }


@app.post("/control/reauth")
async def reauth(force: bool = True) -> dict[str, Any]:
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    try:
        return await _engine_app.reauthenticate(force=force)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/control/refresh-universe")
async def refresh_universe() -> dict[str, Any]:
    """Force reload of weekly option chain (Pi API or Flattrade scripmaster) into DB."""
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    try:
        ok = await _engine_app._refresh_universe(reason="manual_refresh")
        universe = _engine_app._universe
        return {
            "ok": ok,
            "expiry_symbol": universe.expiry_symbol if universe else None,
            "instrument_count": len(universe.instruments) if universe else 0,
            "atm_strike": float(universe.atm_strike) if universe else None,
            "atm_ce": universe.atm_ce.tsym if universe and universe.atm_ce else None,
            "atm_pe": universe.atm_pe.tsym if universe and universe.atm_pe else None,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/broker/trades")
async def broker_trades(
    limit: int = 500,
    trade_date: str | None = None,
) -> list[dict[str, Any]]:
    """Live mode: closed round-trips from Flattrade trade book."""
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    if not is_live_execution():
        raise HTTPException(status_code=400, detail="broker_trades_only_in_live_mode")
    try:
        return await _engine_app.get_broker_closed_trades_cached(
            limit=limit,
            trade_date=trade_date,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/trade-blotter")
async def trade_blotter(limit: int = 200) -> dict[str, Any]:
    if _engine_app is None:
        return {"open_positions": [], "closed_trades": []}
    snap = _engine_app.get_watchlist_snapshot()
    closed = await _engine_app.get_trade_blotter(limit=limit)
    return {
        "open_positions": snap.get("open_positions", []),
        "closed_trades": closed,
    }


@app.post("/control/reset-paper-account")
async def reset_paper_account() -> dict[str, Any]:
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    try:
        return await _engine_app.reset_paper_account()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/control/exit-position")
async def exit_position(position_id: str) -> dict[str, Any]:
    """Manually square-off an open position at current option LTP."""
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    from decimal import Decimal
    from uuid import UUID

    pm = _engine_app.orchestrator.positions
    if position_id.startswith("broker-"):
        token = position_id.removeprefix("broker-")
        await _engine_app.reconcile_broker_positions_now()
        pos = pm.position_by_token(token)
        if pos is None:
            raise HTTPException(status_code=404, detail="Position not open")
        pid = pos.position_id
    else:
        try:
            pid = UUID(position_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid position_id") from exc

        pos = next(
            (p for p in pm.open_positions if p.position_id == pid),
            None,
        )
        if pos is None:
            raise HTTPException(status_code=404, detail="Position not open")

    state = _engine_app.option_data.get(pos.instrument_token)
    ltp = state.ltp if state and state.ltp is not None else None
    try:
        return await pm.manual_exit(
            pid, Decimal(str(ltp)) if ltp is not None else None
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Position not open") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/watchlist")
async def watchlist() -> dict[str, Any]:
    if _engine_app is None:
        return {
            "underlying": get_config().symbols.get("underlying", "SENSEX"),
            "spot_ltp": None,
            "atm_strike": None,
            "instrument_count": 0,
            "last_quote_ts": None,
            "items": [],
        }
    snapshot = _engine_app.get_watchlist_snapshot()
    snapshot["last_quote_ts"] = _engine_state.get("last_quote_ts")
    return snapshot


@app.get("/chart/candles")
async def chart_candles(
    underlying: str = "SENSEX",
    interval: str = "15m",
    days: int = 30,
    token: str | None = None,
    exchange: str | None = None,
    tsym: str | None = None,
) -> dict[str, Any]:
    """OHLC for dashboard chart (1m / 3m / 5m / 15m) with DB history.

    Pass ``token`` to load an option/futures contract chart instead of the
    underlying futures stitch used for the index dashboard default.
    """
    if _engine_app is None:
        return {"underlying": underlying.upper(), "interval": interval, "bars": []}
    iv = interval.lower().strip()
    if iv in ("1m", "1"):
        bar_minutes = 1
    elif iv in ("3m", "3"):
        bar_minutes = 3
    elif iv in ("5m", "5"):
        bar_minutes = 5
    else:
        bar_minutes = 15
    days_n = max(1, min(days, 365))
    tok = (token or "").strip()
    if tok:
        return await _engine_app.get_instrument_chart_bars(
            tok,
            exchange=(exchange or "NFO").strip().upper() or "NFO",
            tsym=(tsym or "").strip() or None,
            underlying=underlying,
            minutes=bar_minutes,
            days=days_n,
        )
    return await _engine_app.get_underlying_chart_bars(
        underlying,
        minutes=bar_minutes,
        days=days_n,
    )


@app.get("/positions/stream")
async def positions_stream() -> StreamingResponse:
    """SSE stream of open positions — tick-driven via Flattrade WebSocket."""

    async def generate():
        cfg = get_config()
        interval_ms = int(cfg.runtime.get("position_stream_interval_ms", 150))
        interval = max(0.05, interval_ms / 1000.0)
        while True:
            if _engine_app is None:
                payload = {"open_positions": [], "feed_mode": "offline"}
            else:
                payload = _engine_app.get_positions_snapshot()
            yield f"data: {json.dumps(payload, default=str)}\n\n"
            if _engine_app is None:
                await asyncio.sleep(interval)
                continue
            tick = _engine_app.position_tick
            tick.clear()
            try:
                await asyncio.wait_for(tick.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/quotes/stream")
async def quotes_stream() -> StreamingResponse:
    """SSE stream of option-chain snapshots for the live ticker UI."""

    async def generate():
        cfg = get_config()
        interval_ms = int(cfg.runtime.get("watchlist_stream_interval_ms", 250))
        interval = max(0.1, interval_ms / 1000.0)
        while True:
            if _engine_app is None:
                payload = {"items": [], "feed_mode": "offline"}
            else:
                payload = _engine_app.get_watchlist_snapshot()
                payload["last_quote_ts"] = _engine_state.get("last_quote_ts")
            yield f"data: {json.dumps(payload, default=str)}\n\n"
            if _engine_app is None:
                await asyncio.sleep(interval)
                continue
            tick = _engine_app.watchlist_tick
            tick.clear()
            try:
                await asyncio.wait_for(tick.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/market-summary")
async def market_summary() -> dict[str, Any]:
    global _market_summary_cache, _market_summary_cache_at
    if _engine_app is None:
        return {
            "underlying": "SENSEX",
            "spot_ltp": None,
            "session_vwap": None,
            "spot_vs_vwap": None,
            "atm_strike": None,
            "bias_5m": "NEUTRAL",
            "market_session": "CLOSED",
            "market_open": False,
            "strategy": "vwap_reclaim",
            "trading_mode": "paper",
            "ist_time": datetime.now(IST).isoformat(),
            "starting_capital": 50000.0,
            "available_capital": 50000.0,
            "deployed_capital": 0.0,
            "today_pnl": 0.0,
            "trade_count": 0,
            "has_open_position": False,
        }
    now = time.monotonic()
    if (
        _market_summary_cache is not None
        and now - _market_summary_cache_at < _MARKET_SUMMARY_TTL_SEC
    ):
        cached = dict(_market_summary_cache)
        cached["trading_mode"] = get_execution_mode()
        return cached
    async with _market_summary_lock:
        now = time.monotonic()
        if (
            _market_summary_cache is not None
            and now - _market_summary_cache_at < _MARKET_SUMMARY_TTL_SEC
        ):
            cached = dict(_market_summary_cache)
            cached["trading_mode"] = get_execution_mode()
            return cached
        summary = await _build_market_summary()
        _market_summary_cache = summary
        _market_summary_cache_at = time.monotonic()
        return summary


async def _build_market_summary() -> dict[str, Any]:
    summary = _engine_app.get_market_summary()
    summary["trading_mode"] = get_execution_mode()
    try:
        from algoflat.symbols_util import uses_pooled_capital

        if is_live_execution():
            limits, broker_day, _ = await _engine_app.live_broker_snapshot()
            summary["starting_capital"] = float(limits.cash)
            summary["available_capital"] = float(limits.available)
            summary["deployed_capital"] = float(limits.margin_used)
            summary["used_margin"] = float(limits.margin_used)
            summary["broker_cash"] = float(limits.cash)
            summary["broker_collateral"] = float(limits.collateral)
            from algoflat.risk.session_stats import fetch_session_trade_stats

            algo_stats = await fetch_session_trade_stats("live")
            snaps = [await _engine_app.orchestrator.risk.ensure_daily_state()]
            snap = snaps[0]
            summary["today_realized_pnl"] = float(broker_day.realized_pnl)
            summary["today_pnl"] = float(broker_day.total_pnl)
            summary["broker_day_pnl"] = float(broker_day.total_pnl)
            try:
                broker_trades = await _engine_app.get_broker_closed_trades_cached(limit=500)
                summary["trade_count"] = len(broker_trades)
                summary["broker_trade_count"] = len(broker_trades)
            except Exception:
                summary["trade_count"] = algo_stats.trade_count
                summary["broker_trade_count"] = algo_stats.trade_count
            summary["algo_today_pnl"] = float(algo_stats.realized_pnl)
            summary["consecutive_losses"] = algo_stats.consecutive_losses
            summary["kill_switch"] = any(s.kill_switch for s in snaps)
            summary["entries_blocked"] = any(s.entries_blocked for s in snaps)
            block_reasons = [s.block_reason for s in snaps if s.block_reason]
            summary["block_reason"] = block_reasons[0] if block_reasons else None
            summary["auto_trade_enabled"] = not summary["entries_blocked"]
        else:
            if uses_pooled_capital(_engine_app.config):
                snaps = [await _engine_app.orchestrator.risk.ensure_daily_state()]
            else:
                snaps = []
                for row in list_underlyings(_engine_app.config):
                    sym = str(row.get("symbol", "")).upper()
                    if sym:
                        snaps.append(await _engine_app.orchestrator.risk.ensure_daily_state(sym))
                if not snaps:
                    snaps = [await _engine_app.orchestrator.risk.ensure_daily_state()]

            snap = snaps[0]
            summary["starting_capital"] = float(sum(s.starting_capital for s in snaps))
            summary["available_capital"] = float(sum(s.available_capital for s in snaps))
            summary["deployed_capital"] = float(sum(s.deployed_capital for s in snaps))
            summary["used_margin"] = summary["deployed_capital"]
            summary["today_pnl"] = float(sum(s.realized_pnl for s in snaps))
            summary["trade_count"] = sum(s.trade_count for s in snaps)
            summary["consecutive_losses"] = max((s.consecutive_losses for s in snaps), default=0)
            summary["kill_switch"] = any(s.kill_switch for s in snaps)
            summary["entries_blocked"] = any(s.entries_blocked for s in snaps)
            block_reasons = [s.block_reason for s in snaps if s.block_reason]
            summary["block_reason"] = block_reasons[0] if block_reasons else None
            summary["auto_trade_enabled"] = not summary["entries_blocked"]

        summary["scan_interval_seconds"] = int(
            _engine_app.config.runtime.get("scan_interval_seconds", 10)
        )
        summary["entry_scan_on_quote"] = bool(
            _engine_app.config.runtime.get("entry_scan_on_quote", False)
        )
        summary["exit_eval_interval_seconds"] = float(
            _engine_app.config.runtime.get("exit_eval_interval_seconds", 2)
        )
        summary["reconciliation_interval_seconds"] = int(
            _engine_app.config.runtime.get("reconciliation_interval_seconds", 60)
        )
        risk_cfg = _engine_app.config.risk
        sizing = risk_cfg.get("confidence_lot_sizing") or {}
        start_cap = float(summary.get("starting_capital") or risk_cfg.get("account_capital_inr") or 0)
        pct = float(risk_cfg.get("max_daily_loss_pct_of_starting") or 0)
        if pct > 0 and start_cap > 0:
            max_daily_loss_inr = round(start_cap * pct / 100.0, 2)
        else:
            max_daily_loss_inr = float(risk_cfg.get("max_daily_loss", 0))
        summary["max_daily_loss"] = max_daily_loss_inr
        summary["max_daily_loss_pct_of_starting"] = pct
        summary["max_deployed_pct_of_equity"] = float(
            risk_cfg.get("max_deployed_pct_of_equity", 85)
        )
        summary["trading_limits"] = {
            "max_trades_per_day": int(risk_cfg.get("max_trades_per_day", 0)),
            "max_trades_per_day_label": "unlimited"
            if int(risk_cfg.get("max_trades_per_day", 0)) <= 0
            else str(risk_cfg.get("max_trades_per_day")),
            "cooldown_after_exit_minutes": int(
                risk_cfg.get("cooldown_after_exit_minutes", 3)
            ),
            "max_concurrent_positions_per_index": int(
                risk_cfg.get("max_concurrent_positions_per_index", 0)
            ),
            "max_daily_loss_inr": max_daily_loss_inr,
            "max_daily_loss_pct_of_starting": pct,
            "max_consecutive_losses": int(risk_cfg.get("max_consecutive_losses", 0)),
            "min_router_confidence": int(
                _engine_app.config.strategy.get("router", {}).get("min_confidence", 80)
            ),
            "use_pooled_capital": bool(risk_cfg.get("use_pooled_capital", True)),
            "account_capital_inr": float(risk_cfg.get("account_capital_inr", 50000)),
            "confidence_lot_sizing": {
                "enabled": bool(sizing.get("enabled", False)),
                "mode": str(sizing.get("mode", "dynamic")),
                "min_capital_pct": float(sizing.get("min_capital_pct", 30)),
                "max_capital_pct": float(sizing.get("max_capital_pct", 90)),
                "aggressive_deploy_min_confidence": int(
                    sizing.get("aggressive_deploy_min_confidence", 90)
                ),
                "max_lots": int(sizing.get("max_lots", 0)),
            },
        }
        open_positions = _engine_app.open_positions_for_ui()
        summary["open_positions"] = open_positions
        if open_positions:
            summary["open_position"] = open_positions[0]
        summary["has_open_position"] = len(open_positions) > 0
        summary["open_position_count"] = len(open_positions)
        if is_live_execution():
            pos_unrealized = sum(float(p.get("unrealized_pnl", 0)) for p in open_positions)
            unrealized = (
                pos_unrealized
                if open_positions
                else float(broker_day.unrealized_pnl)
            )
        else:
            unrealized = _engine_app.orchestrator.positions.unrealized_pnl(
                _engine_app.option_data,
            )
        summary["unrealized_pnl"] = float(unrealized)
        if is_live_execution():
            summary["equity"] = float(limits.cash + limits.collateral)
            summary["broker_unrealized_pnl"] = float(broker_day.unrealized_pnl)
            summary["broker_realized_pnl"] = float(broker_day.realized_pnl)
        else:
            summary["equity"] = float(summary["starting_capital"] + summary["today_pnl"] + unrealized)
        summary["auto_trading_active"] = (
            summary.get("market_open", False)
            and summary.get("auto_trade_enabled", False)
        )
        pool = get_pool()
        async with pool.acquire() as conn:
            summary["candidate_count"] = int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM candidate_signals
                    WHERE ts::date = CURRENT_DATE
                    """
                )
                or 0
            )
            summary["rejection_count"] = int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM validation_results vr
                    JOIN candidate_signals cs ON cs.id = vr.candidate_signal_id
                    WHERE cs.ts::date = CURRENT_DATE AND NOT vr.passed
                    """
                )
                or 0
            )
            rows = await conn.fetch(
                """
                SELECT vr.rejection_reasons, cs.tsym
                FROM validation_results vr
                JOIN candidate_signals cs ON cs.id = vr.candidate_signal_id
                WHERE cs.ts::date = CURRENT_DATE AND NOT vr.passed
                ORDER BY vr.ts DESC
                LIMIT 5
                """
            )
            summary["recent_rejections"] = [
                {
                    "tsym": r["tsym"],
                    "reasons": list(r["rejection_reasons"] or []),
                }
                for r in rows
            ]
    except Exception:
        pass

    realized = float(
        summary.get("today_realized_pnl")
        if is_live_execution()
        else summary.get("today_pnl") or 0
    )
    unrealized = float(summary.get("unrealized_pnl") or 0)
    if is_live_execution():
        total_pnl = float(
            summary.get("broker_day_pnl")
            or summary.get("today_pnl")
            or realized + unrealized
        )
        if summary.get("broker_realized_pnl") is not None:
            realized = float(summary["broker_realized_pnl"])
    else:
        total_pnl = realized + unrealized
    used_margin = float(summary.get("used_margin") or summary.get("deployed_capital") or 0)
    equity = float(summary.get("equity") or 0)
    max_loss = float(summary.get("max_daily_loss") or 0)
    deploy_cap_pct = float(summary.get("max_deployed_pct_of_equity") or 90)
    deploy_cap = equity * (deploy_cap_pct / 100) if equity > 0 else 0.0
    loss_budget_pct = (
        min(100.0, round((abs(total_pnl) / max_loss) * 100))
        if max_loss > 0 and total_pnl < 0
        else 0.0
    )
    margin_deploy_pct = (
        min(100.0, round((used_margin / deploy_cap) * 100))
        if deploy_cap > 0
        else (min(100.0, round((used_margin / equity) * 100)) if equity > 0 else 0.0)
    )
    risk_pct = max(loss_budget_pct, margin_deploy_pct)
    if risk_pct <= 0:
        risk_driver = "none"
    elif margin_deploy_pct >= loss_budget_pct:
        risk_driver = "margin"
    else:
        risk_driver = "loss_budget"
    has_open = bool(summary.get("has_open_position"))
    if has_open:
        entry_status = "in_position"
    elif summary.get("kill_switch"):
        entry_status = "kill_switch"
    elif summary.get("entries_blocked"):
        entry_status = "entries_blocked"
    else:
        entry_status = "flat_scanning"
    summary["risk_dashboard"] = {
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": total_pnl,
        "loss_budget_pct": loss_budget_pct,
        "margin_deploy_pct": margin_deploy_pct,
        "risk_pct": risk_pct,
        "risk_driver": risk_driver,
        "has_open_position": has_open,
        "entry_status": entry_status,
    }

    try:
        from algoflat.trading.decision_flow import build_decision_flow

        features = _engine_app.orchestrator.features.compute_cached(max_age_sec=5.0)
        mi = _engine_app.orchestrator.refresh_mi_snapshot()
        ml = _engine_app.orchestrator.last_ml_prediction
        summary["decision_flow"] = build_decision_flow(
            features,
            decision=_engine_app.orchestrator.router.last_decision,
            basis_cfg=_engine_app.config.validator.get("basis_gate") or {},
            premium_sample_count=_engine_app.orchestrator._premium_tracker.total_samples(),
            entries_blocked=bool(summary.get("entries_blocked")),
            block_reason=summary.get("block_reason"),
            has_open_position=has_open,
            mi_snapshot=mi,
            ml_prediction=ml,
            last_entry_block=_engine_app.orchestrator.last_entry_block,
            last_entry_confirm=_engine_app.orchestrator.last_entry_confirm,
            underlying=str(
                summary.get("active_underlying")
                or summary.get("underlying")
                or _engine_app.config.symbols.get("underlying")
                or "SENSEX"
            ),
        )
        summary["market_intelligence"] = mi
        summary["ml_shadow"] = ml
    except Exception:
        pass

    return summary


@app.get("/strategy-state")
async def strategy_state() -> dict[str, Any]:
    if _engine_app is None:
        return {"error": "engine_not_ready"}
    orch = _engine_app.orchestrator
    await _engine_app.market_data.refresh_session_candles(force=True)
    features = orch.features.compute()
    m1 = _engine_app.market_data.candles(CandleInterval.M1)
    m3 = _engine_app.market_data.candles(CandleInterval.M3)
    m5 = _engine_app.market_data.candles(CandleInterval.M5)
    is_expiry = orch._is_expiry_day()
    regime = orch.regime.classify(features, m1, m5, is_expiry_day=is_expiry)
    universe = _engine_app._universe
    inst_token = None
    if features.bias_5m.value == "bullish" and universe and universe.atm_ce:
        inst_token = universe.atm_ce.token
    elif features.bias_5m.value == "bearish" and universe and universe.atm_pe:
        inst_token = universe.atm_pe.token
    option = _engine_app.option_data.get(inst_token) if inst_token else None
    decision = None
    signal = None
    if universe:
        options = {"vwap_reclaim": option, "vwap_pullback": option, "vwap_trend": option}
        decision, signal = orch.router.route(features, regime, universe, options)
    return {
        "market_open": _engine_app._is_market_open(),
        "feed_mode": _engine_app._feed_mode,
        "ws_started": _engine_app._ws_started,
        "ws_open": bool(getattr(_engine_app.broker, "websocket_open", False)),
        "candle_counts": {"m1": len(m1), "m3": len(m3), "m5": len(m5)},
        "last_m1_close": float(m1[-1].close) if m1 else None,
        "last_m3_close": float(m3[-1].close) if m3 else None,
        "features": {
            "spot": float(features.nifty_spot) if features.nifty_spot else None,
            "vwap": float(features.session_vwap) if features.session_vwap else None,
            "bias": features.bias_5m.value,
            "setup_3m": features.setup_3m,
            "trigger_1m": features.trigger_1m,
            "setup_vwap_pullback": (features.extra or {}).get("setup_vwap_pullback"),
            "trigger_vwap_pullback": (features.extra or {}).get("trigger_vwap_pullback"),
            "distance_to_vwap_points": (features.extra or {}).get("distance_to_vwap_points"),
            "structure_5m": (features.extra or {}).get("structure_5m"),
        },
        "regime": regime.model_dump(mode="json") if regime else None,
        "decision": decision.model_dump(mode="json") if decision else None,
        "would_signal": signal is not None,
        "signal_tsym": signal.tsym if signal else None,
        "signal_setup": signal.setup_type if signal else None,
        "signal_confidence": signal.confidence if signal else None,
        "option_ltp": float(option.ltp) if option and option.ltp else None,
        "is_expiry_day": is_expiry,
    }
