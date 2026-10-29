"""Flattrade credentials: DB storage with .env fallback."""

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
_cached: FlattradeConfig | None = None


@dataclass(frozen=True)
class FlattradeConfig:
    user_id: str | None
    api_key: str | None
    api_secret: str | None
    password: str | None
    totp_secret: str | None
    access_token: str | None
    token_expires_at: datetime | None
    redirect_url: str
    token_file: str

    def has_api_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def has_auto_login(self) -> bool:
        return bool(
            self.user_id
            and self.api_key
            and self.api_secret
            and self.password
            and self.totp_secret
        )

    def has_access_token(self) -> bool:
        if not self.access_token:
            return False
        if self.token_expires_at is None:
            return True
        exp = self.token_expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=IST)
        return datetime.now(tz=IST) < exp.astimezone(IST)

    @classmethod
    def from_env(cls, env: EnvSettings | None = None) -> FlattradeConfig:
        env = env or get_env()
        return cls(
            user_id=_clean(env.flattrade_user_id),
            api_key=_clean(env.flattrade_api_key),
            api_secret=_clean(env.flattrade_api_secret),
            password=_clean(env.flattrade_password),
            totp_secret=_clean(env.flattrade_totp_secret),
            access_token=_clean(env.flattrade_access_token),
            token_expires_at=None,
            redirect_url=env.flattrade_redirect_url or "http://127.0.0.1:8000/callback",
            token_file=env.flattrade_token_file or ".flattrade/session.json",
        )

    @classmethod
    def from_db_row(cls, row: dict[str, Any], *, env: FlattradeConfig | None = None) -> FlattradeConfig:
        """Build config from the saved database row (Settings → Broker)."""
        env = env or cls.from_env()
        expires = row.get("token_expires_at")
        if isinstance(expires, str):
            try:
                expires = datetime.fromisoformat(expires)
            except ValueError:
                expires = None
        return cls(
            user_id=_clean(row.get("user_id")),
            api_key=_clean(row.get("api_key")),
            api_secret=_clean(row.get("api_secret")),
            password=_clean(row.get("password")),
            totp_secret=_clean(row.get("totp_secret")),
            access_token=_clean(row.get("access_token")) or env.access_token,
            token_expires_at=expires if isinstance(expires, datetime) else None,
            redirect_url=_clean(row.get("redirect_url")) or env.redirect_url,
            token_file=env.token_file,
        )

    def merge_db(self, row: dict[str, Any] | None) -> FlattradeConfig:
        if not row:
            return self
        merged = self
        for field in (
            "user_id",
            "api_key",
            "api_secret",
            "password",
            "totp_secret",
            "access_token",
        ):
            value = _clean(row.get(field))
            if value:
                merged = replace(merged, **{field: value})
        redirect = _clean(row.get("redirect_url"))
        if redirect:
            merged = replace(merged, redirect_url=redirect)
        expires = row.get("token_expires_at")
        if isinstance(expires, datetime):
            merged = replace(merged, token_expires_at=expires)
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


def credentials_status(cfg: FlattradeConfig, *, source: str) -> dict[str, Any]:
    return {
        "source": source,
        "user_id": cfg.user_id,
        "api_key_masked": mask_secret(cfg.api_key),
        "api_secret_set": bool(cfg.api_secret),
        "password_set": bool(cfg.password),
        "totp_secret_set": bool(cfg.totp_secret),
        "access_token_set": bool(cfg.access_token),
        "has_access_token": cfg.has_access_token(),
        "token_expires_at": cfg.token_expires_at.isoformat() if cfg.token_expires_at else None,
        "redirect_url": cfg.redirect_url,
        "has_api_credentials": cfg.has_api_credentials(),
        "has_auto_login": cfg.has_auto_login(),
    }


async def _load_db_row() -> dict[str, Any] | None:
    try:
        pool = get_pool()
    except RuntimeError:
        # Standalone scripts may call load before pool init — fall back to env/file.
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT user_id, api_key, api_secret, password, totp_secret,
                       redirect_url, access_token, token_expires_at
                FROM flattrade_credentials
                WHERE id = $1
                """,
                _CREDENTIALS_ROW_ID,
            )
        return dict(row) if row else None
    except Exception:
        # Pre-migration DBs may lack access_token columns — fall back.
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT user_id, api_key, api_secret, password, totp_secret, redirect_url
                    FROM flattrade_credentials
                    WHERE id = $1
                    """,
                    _CREDENTIALS_ROW_ID,
                )
            return dict(row) if row else None
        except Exception:
            logger.exception("flattrade_credentials_load_failed")
            return None


def _detect_source(base: FlattradeConfig, merged: FlattradeConfig) -> str:
    db_fields = ("user_id", "api_key", "api_secret", "password", "totp_secret", "access_token")
    from_db = any(getattr(merged, f) != getattr(base, f) for f in db_fields)
    from_env = any(getattr(base, f) for f in db_fields if f != "access_token") or bool(base.access_token)
    if from_db and from_env:
        return "mixed"
    if from_db:
        return "database"
    if from_env:
        return "environment"
    return "none"


async def load_flattrade_config(*, force: bool = False) -> FlattradeConfig:
    global _cached
    if force:
        _cached = None
    elif _cached is not None:
        return _cached
    base = FlattradeConfig.from_env()
    try:
        get_pool()
    except RuntimeError:
        # A health probe before startup must not cache an env-only miss.
        return base
    row = await _load_db_row()
    if row and _clean(row.get("api_key")) and _clean(row.get("api_secret")):
        merged = FlattradeConfig.from_db_row(row, env=base)
    else:
        merged = base.merge_db(row)
    _cached = merged
    return merged


async def load_flattrade_credentials_status() -> dict[str, Any]:
    base = FlattradeConfig.from_env()
    row = await _load_db_row()
    if row and _clean(row.get("api_key")) and _clean(row.get("api_secret")):
        merged = FlattradeConfig.from_db_row(row, env=base)
    else:
        merged = base.merge_db(row)
    return credentials_status(merged, source=_detect_source(base, merged))


def invalidate_flattrade_config_cache() -> None:
    global _cached
    _cached = None


def _persist_access_token_session(
    token_file: str,
    *,
    user_id: str,
    access_token: str,
    expires_at: datetime,
    source: str = "settings",
) -> None:
    from algoflat.broker.auth import FlattradeSession, FlattradeSessionStore

    now = datetime.now(tz=IST)
    FlattradeSessionStore(Path(token_file)).save(
        FlattradeSession(
            user_id=user_id,
            access_token=access_token,
            obtained_at=now,
            expires_at=expires_at,
            source=source,
        )
    )


async def save_flattrade_session_token(
    *,
    user_id: str,
    access_token: str,
    expires_at: datetime,
    token_file: str | None = None,
) -> None:
    """Persist session to DB + file so engine can reuse without TOTP re-login."""
    cfg = await load_flattrade_config(force=True)
    path = token_file or cfg.token_file
    _persist_access_token_session(
        path,
        user_id=user_id,
        access_token=access_token,
        expires_at=expires_at,
        source="auto_oauth",
    )
    try:
        pool = get_pool()
    except RuntimeError:
        logger.debug("flattrade_session_db_skip_no_pool")
        invalidate_flattrade_config_cache()
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO flattrade_credentials (
                    id, user_id, access_token, token_expires_at, updated_at
                ) VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (id) DO UPDATE SET
                    user_id = COALESCE(EXCLUDED.user_id, flattrade_credentials.user_id),
                    access_token = EXCLUDED.access_token,
                    token_expires_at = EXCLUDED.token_expires_at,
                    updated_at = now()
                """,
                _CREDENTIALS_ROW_ID,
                user_id,
                access_token,
                expires_at,
            )
        # Also append to broker_sessions audit trail
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO broker_sessions (user_id, access_token, expires_at, api_version)
                VALUES ($1, $2, $3, 'flattrade')
                """,
                user_id,
                access_token,
                expires_at,
            )
    except Exception:
        logger.exception("flattrade_session_db_persist_failed")
    invalidate_flattrade_config_cache()
    logger.info(
        "flattrade_session_persisted",
        user_id=user_id,
        expires_at=expires_at.isoformat(),
        token_file=path,
    )


async def save_flattrade_credentials(updates: dict[str, Any]) -> dict[str, Any]:
    """Persist credentials; empty secret fields keep existing values."""
    current = await load_flattrade_config(force=True)
    pool = get_pool()

    user_id = _clean(updates.get("user_id")) or current.user_id
    api_key = _clean(updates.get("api_key")) or current.api_key
    api_secret = _clean(updates.get("api_secret")) or current.api_secret
    password = _clean(updates.get("password")) or current.password
    totp_secret = _clean(updates.get("totp_secret")) or current.totp_secret
    redirect_url = _clean(updates.get("redirect_url")) or current.redirect_url
    access_token = _clean(updates.get("access_token")) or current.access_token

    if not user_id or not api_key or not api_secret:
        raise ValueError("user_id, api_key, and api_secret are required")

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO flattrade_credentials (
                id, user_id, api_key, api_secret, password, totp_secret,
                redirect_url, access_token, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
            ON CONFLICT (id) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                api_key = EXCLUDED.api_key,
                api_secret = EXCLUDED.api_secret,
                password = EXCLUDED.password,
                totp_secret = EXCLUDED.totp_secret,
                redirect_url = EXCLUDED.redirect_url,
                access_token = COALESCE(EXCLUDED.access_token, flattrade_credentials.access_token),
                updated_at = now()
            """,
            _CREDENTIALS_ROW_ID,
            user_id,
            api_key,
            api_secret,
            password,
            totp_secret,
            redirect_url,
            access_token,
        )

    if _clean(updates.get("access_token")) and access_token:
        from algoflat.broker.auth import next_token_expiry

        _persist_access_token_session(
            current.token_file,
            user_id=user_id,
            access_token=access_token,
            expires_at=next_token_expiry(),
            source="settings",
        )

    invalidate_flattrade_config_cache()
    merged = await load_flattrade_config(force=True)
    base = FlattradeConfig.from_env()
    status = credentials_status(merged, source=_detect_source(base, merged))
    logger.info("flattrade_credentials_saved", user_id=user_id, source=status["source"])
    return status
