"""Upstox credentials: DB storage with .env fallback."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from algoflat.config import EnvSettings, get_env
from algoflat.db.connection import get_pool

logger = structlog.get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
_CREDENTIALS_ROW_ID = 1
_cached: UpstoxConfig | None = None


@dataclass(frozen=True)
class UpstoxConfig:
    api_key: str | None
    api_secret: str | None
    access_token: str | None
    analytic_token: str | None
    redirect_url: str
    token_file: str

    def has_api_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def has_access_token(self) -> bool:
        return bool(self.access_token)

    def has_analytic_token(self) -> bool:
        return bool(self.analytic_token)

    @classmethod
    def from_env(cls, env: EnvSettings | None = None) -> UpstoxConfig:
        env = env or get_env()
        return cls(
            api_key=_clean(env.upstox_api_key),
            api_secret=_clean(env.upstox_api_secret),
            access_token=_clean(env.upstox_access_token),
            analytic_token=_clean(env.upstox_analytic_token),
            redirect_url=env.upstox_redirect_url or "http://127.0.0.1:8000/callback",
            token_file=env.upstox_token_file or ".upstox/session.json",
        )

    @classmethod
    def from_db_row(cls, row: dict[str, Any], *, env: UpstoxConfig | None = None) -> UpstoxConfig:
        env = env or cls.from_env()
        return cls(
            api_key=_clean(row.get("api_key")),
            api_secret=_clean(row.get("api_secret")),
            access_token=_clean(row.get("access_token")) or env.access_token,
            analytic_token=_clean(row.get("analytic_token")) or env.analytic_token,
            redirect_url=_clean(row.get("redirect_url")) or env.redirect_url,
            token_file=env.token_file,
        )

    def merge_db(self, row: dict[str, Any] | None) -> UpstoxConfig:
        if not row:
            return self
        merged = self
        for field in ("api_key", "api_secret", "access_token", "analytic_token"):
            value = _clean(row.get(field))
            if value:
                merged = replace(merged, **{field: value})
        redirect = _clean(row.get("redirect_url"))
        if redirect:
            merged = replace(merged, redirect_url=redirect)
        return merged


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def mask_secret(value: str | None, *, head: int = 4, tail: int = 4) -> str | None:
    if not value:
        return None
    if len(value) <= head + tail:
        return "•" * len(value)
    return f"{value[:head]}{'•' * (len(value) - head - tail)}{value[-tail:]}"


def credentials_status(cfg: UpstoxConfig, *, source: str) -> dict[str, Any]:
    from algoflat.broker.upstox_auth import resolve_session

    session = resolve_session(cfg)
    has_token = session is not None and session.is_valid
    return {
        "source": source,
        "provider": "upstox",
        "api_key_masked": mask_secret(cfg.api_key),
        "api_secret_set": bool(cfg.api_secret),
        "access_token_set": bool(cfg.access_token),
        "analytic_token_set": bool(cfg.analytic_token),
        "redirect_url": cfg.redirect_url,
        "has_api_credentials": cfg.has_api_credentials(),
        "has_access_token": has_token,
        # UI compat — Upstox uses OAuth token, not Flattrade TOTP auto-login.
        "has_auto_login": has_token,
    }


async def _load_db_row() -> dict[str, Any] | None:
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT api_key, api_secret, redirect_url, access_token, analytic_token
                FROM upstox_credentials
                WHERE id = $1
                """,
                _CREDENTIALS_ROW_ID,
            )
        return dict(row) if row else None
    except Exception:
        logger.exception("upstox_credentials_load_failed")
        return None


def _detect_source(base: UpstoxConfig, merged: UpstoxConfig) -> str:
    db_fields = ("api_key", "api_secret", "access_token", "analytic_token")
    from_db = any(getattr(merged, f) != getattr(base, f) for f in db_fields)
    from_env = any(getattr(base, f) for f in db_fields)
    if from_db and from_env:
        return "mixed"
    if from_db:
        return "database"
    if from_env:
        return "environment"
    return "none"


async def load_upstox_config(*, force: bool = False) -> UpstoxConfig:
    global _cached
    if force:
        _cached = None
    elif _cached is not None:
        return _cached
    base = UpstoxConfig.from_env()
    row = await _load_db_row()
    if row and _clean(row.get("api_key")) and _clean(row.get("api_secret")):
        merged = UpstoxConfig.from_db_row(row, env=base)
    else:
        merged = base.merge_db(row)
    _cached = merged
    return _cached


async def load_upstox_credentials_status() -> dict[str, Any]:
    base = UpstoxConfig.from_env()
    row = await _load_db_row()
    if row and _clean(row.get("api_key")) and _clean(row.get("api_secret")):
        merged = UpstoxConfig.from_db_row(row, env=base)
    else:
        merged = base.merge_db(row)
    return credentials_status(merged, source=_detect_source(base, merged))


def invalidate_upstox_config_cache() -> None:
    global _cached
    _cached = None


def _persist_access_token_session(token_file: str, access_token: str) -> None:
    from algoflat.broker.upstox_auth import UpstoxSession, UpstoxSessionStore, jwt_expiry, next_token_expiry

    now = datetime.now(tz=IST)
    expires_at = jwt_expiry(access_token) or next_token_expiry(now)
    UpstoxSessionStore(Path(token_file)).save(
        UpstoxSession(
            access_token=access_token,
            obtained_at=now,
            expires_at=expires_at,
            source="settings",
        )
    )


async def save_upstox_credentials(updates: dict[str, Any]) -> dict[str, Any]:
    current = await load_upstox_config(force=True)
    pool = get_pool()

    api_key = _clean(updates.get("api_key")) or current.api_key
    api_secret = _clean(updates.get("api_secret")) or current.api_secret
    redirect_url = _clean(updates.get("redirect_url")) or current.redirect_url
    access_token = _clean(updates.get("access_token")) or current.access_token
    analytic_token = _clean(updates.get("analytic_token")) or current.analytic_token

    if not api_key or not api_secret:
        if not access_token and not analytic_token:
            raise ValueError("API key and API secret are required")

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO upstox_credentials (
                id, api_key, api_secret, redirect_url, access_token, analytic_token, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, now())
            ON CONFLICT (id) DO UPDATE SET
                api_key = COALESCE(EXCLUDED.api_key, upstox_credentials.api_key),
                api_secret = COALESCE(EXCLUDED.api_secret, upstox_credentials.api_secret),
                redirect_url = COALESCE(EXCLUDED.redirect_url, upstox_credentials.redirect_url),
                access_token = COALESCE(EXCLUDED.access_token, upstox_credentials.access_token),
                analytic_token = COALESCE(EXCLUDED.analytic_token, upstox_credentials.analytic_token),
                updated_at = now()
            """,
            _CREDENTIALS_ROW_ID,
            api_key,
            api_secret,
            redirect_url,
            access_token,
            analytic_token,
        )

    if _clean(updates.get("access_token")) and access_token:
        _persist_access_token_session(current.token_file, access_token)

    invalidate_upstox_config_cache()
    merged = await load_upstox_config(force=True)
    base = UpstoxConfig.from_env()
    status = credentials_status(merged, source=_detect_source(base, merged))
    logger.info("upstox_credentials_saved", source=status["source"])
    return status
