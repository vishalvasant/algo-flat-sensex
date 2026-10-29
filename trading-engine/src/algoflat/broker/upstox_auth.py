"""Upstox OAuth 2.0 session management."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import structlog

from algoflat.broker.upstox_credentials import UpstoxConfig

logger = structlog.get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
AUTH_DIALOG_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"


@dataclass(frozen=True)
class UpstoxSession:
    access_token: str
    obtained_at: datetime
    expires_at: datetime
    source: str
    user_id: str | None = None

    @property
    def is_valid(self) -> bool:
        return datetime.now(tz=IST) < self.expires_at.astimezone(IST)

    def to_dict(self) -> dict[str, str]:
        return {
            "access_token": self.access_token,
            "obtained_at": self.obtained_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "source": self.source,
            "user_id": self.user_id or "",
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UpstoxSession:
        return cls(
            access_token=str(data["access_token"]),
            obtained_at=datetime.fromisoformat(str(data["obtained_at"])),
            expires_at=datetime.fromisoformat(str(data["expires_at"])),
            source=str(data.get("source", "file")),
            user_id=str(data.get("user_id") or "") or None,
        )


def build_authorization_url(api_key: str, redirect_uri: str, *, state: str = "algoflat") -> str:
    from urllib.parse import urlencode

    params = urlencode(
        {
            "response_type": "code",
            "client_id": api_key,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{AUTH_DIALOG_URL}?{params}"


def parse_redirect_url(redirect_url: str) -> tuple[str, int, str]:
    parsed = urlparse(redirect_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/callback"
    return host, port, path


def next_token_expiry(now: datetime | None = None) -> datetime:
    """Upstox access tokens expire daily at 03:30 IST."""
    current = (now or datetime.now(tz=IST)).astimezone(IST)
    expiry = current.replace(hour=3, minute=30, second=0, microsecond=0)
    if current >= expiry:
        expiry += timedelta(days=1)
    return expiry


def jwt_expiry(access_token: str) -> datetime | None:
    """Decode JWT exp claim when present (Upstox access tokens are JWTs)."""
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        if exp is None:
            return None
        return datetime.fromtimestamp(int(exp), tz=IST)
    except Exception:
        return None


def _session_from_env_token(access_token: str) -> UpstoxSession | None:
    now = datetime.now(tz=IST)
    expires_at = jwt_expiry(access_token) or next_token_expiry(now)
    if now >= expires_at.astimezone(IST):
        logger.warning("upstox_env_token_expired", expires_at=expires_at.isoformat())
        return None
    return UpstoxSession(
        access_token=access_token,
        obtained_at=now,
        expires_at=expires_at,
        source="env",
    )


async def exchange_auth_code(
    *,
    api_key: str,
    api_secret: str,
    redirect_uri: str,
    code: str,
) -> UpstoxSession:
    payload = {
        "code": code,
        "client_id": api_key,
        "client_secret": api_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            TOKEN_URL,
            data=payload,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()

    token = data.get("access_token")
    if not token:
        errors = data.get("errors") or data.get("message") or data
        raise ConnectionError(f"Upstox token exchange failed: {errors}")

    now = datetime.now(tz=IST)
    return UpstoxSession(
        access_token=str(token),
        obtained_at=now,
        expires_at=next_token_expiry(now),
        source="oauth",
        user_id=None,
    )


class UpstoxSessionStore:
    def __init__(self, token_file: Path) -> None:
        self._token_file = token_file

    def load(self) -> UpstoxSession | None:
        if not self._token_file.exists():
            return None
        try:
            data = json.loads(self._token_file.read_text(encoding="utf-8"))
            session = UpstoxSession.from_dict(data)
            if session.is_valid:
                return session
            logger.warning("upstox_token_expired", expires_at=session.expires_at.isoformat())
        except Exception:
            logger.exception("upstox_token_load_failed", path=str(self._token_file))
        return None

    def save(self, session: UpstoxSession) -> None:
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        self._token_file.write_text(
            json.dumps(session.to_dict(), indent=2),
            encoding="utf-8",
        )
        try:
            self._token_file.chmod(0o600)
        except OSError:
            pass
        logger.info(
            "upstox_token_saved",
            path=str(self._token_file),
            expires_at=session.expires_at.isoformat(),
        )


def resolve_session(cfg: UpstoxConfig, token_file: Path | None = None) -> UpstoxSession | None:
    path = token_file or Path(cfg.token_file or ".upstox/session.json")
    if cfg.access_token:
        env_session = _session_from_env_token(cfg.access_token)
        if env_session is not None:
            return env_session
    return UpstoxSessionStore(path).load()


async def ensure_session(cfg: UpstoxConfig, *, force: bool = False) -> UpstoxSession:
    path = Path(cfg.token_file or ".upstox/session.json")
    if not force:
        existing = resolve_session(cfg, path)
        if existing is not None and existing.is_valid:
            return existing
    if cfg.access_token:
        env_session = _session_from_env_token(cfg.access_token)
        if env_session is not None:
            return env_session
    raise ConnectionError(
        "Upstox access token missing or expired — run scripts/upstox_login.py "
        "or set UPSTOX_ACCESS_TOKEN"
    )


def parse_code_from_redirect(url: str) -> str | None:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    codes = qs.get("code") or []
    return codes[0] if codes else None
