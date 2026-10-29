#!/usr/bin/env python3
"""One-shot Flattrade credential validation (TOTP auto-login).

Reads /Users/vishal/Desktop/Algo/Flattrade.txt (or --creds-file).

Auth flow (matches algoflat.broker.auth.automated_login_with_credentials):
  1. POST authapi.flattrade.in/auth/session  → sid
  2. GET  auth.flattrade.in/?app_key=&sid=
  3. POST authapi.flattrade.in/ftauth        → password SHA256 + TOTP → RedirectURL?code=
  4. POST authapi.flattrade.in/trade/apitoken
       body: api_key, request_code, api_secret=SHA256(api_key+code+secret)
  5. Optional: PiConnect GetLimits with jKey=token (proves trading API works)

IMPORTANT: Do not re-run quickly on failure — Flattrade may lock the account.
This script never retries login. Default is a single attempt.

Usage:
  python scripts/validate_flattrade_creds.py
  python scripts/validate_flattrade_creds.py --creds-file /path/to/Flattrade.txt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pyotp

# Host repo root, or /app inside trading-engine container.
_SCRIPT_DIR = Path(__file__).resolve().parent
if (_SCRIPT_DIR.parent / "trading-engine" / "src" / "algoflat").is_dir():
    ROOT = _SCRIPT_DIR.parent
    _SRC = ROOT / "trading-engine" / "src"
elif Path("/app/src/algoflat").is_dir():
    ROOT = Path("/app")
    _SRC = ROOT / "src"
else:
    ROOT = Path(os.environ.get("ALGOFLAT_ROOT", "/app"))
    _SRC = ROOT / "src"

sys.path.insert(0, str(_SRC))
os.chdir(ROOT)
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from algoflat.broker.auth import (  # noqa: E402
    FlattradeSessionStore,
    automated_login_with_credentials,
    generate_totp_code,
    next_token_expiry,
)

DEFAULT_CREDS = Path("/Users/vishal/Desktop/Algo/Flattrade.txt")
TOKEN_OUT = ROOT / ".flattrade" / "session_validate.json"
API_BASE = "https://piconnect.flattrade.in/PiConnectAPI"


def _parse_creds(path: Path) -> dict[str, str]:
    """Parse Flattrade.txt lines: API- / TOTP- / Secret- / User- / Pass-."""
    text = path.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lower = line.lower()
        if lower.startswith("api-"):
            out["api_key"] = line.split("-", 1)[1].strip()
        elif lower.startswith("totp-"):
            out["totp_secret"] = line.split("-", 1)[1].strip()
        elif lower.startswith("secret-"):
            out["api_secret"] = line.split("-", 1)[1].strip()
        elif lower.startswith("user-"):
            out["user_id"] = line.split("-", 1)[1].strip()
        elif lower.startswith("pass-"):
            out["password"] = line.split("-", 1)[1].strip()
        elif "=" in line:
            k, v = line.split("=", 1)
            key = k.strip().lower()
            val = v.strip()
            mapping = {
                "api_key": "api_key",
                "api": "api_key",
                "totp": "totp_secret",
                "totp_secret": "totp_secret",
                "secret": "api_secret",
                "api_secret": "api_secret",
                "user": "user_id",
                "user_id": "user_id",
                "pass": "password",
                "password": "password",
            }
            if key in mapping:
                out[mapping[key]] = val

    required = ("api_key", "api_secret", "totp_secret", "user_id", "password")
    missing = [k for k in required if not out.get(k)]
    if missing:
        raise SystemExit(f"Creds file missing fields: {', '.join(missing)}")
    return out


def _mask(value: str, keep: int = 4) -> str:
    if len(value) <= keep * 2:
        return "***"
    return f"{value[:keep]}…{value[-keep:]}"


def _totp_remaining(secret: str) -> int:
    totp = pyotp.TOTP(secret.replace(" ", ""))
    # pyotp interval is 30s; remaining until next tick
    return int(totp.interval - (time.time() % totp.interval))


def _wait_for_fresh_totp_window(secret: str, *, min_remaining: int = 12) -> str:
    """Avoid mid-window rollover during the multi-step auth. Local only — no network."""
    remaining = _totp_remaining(secret)
    if remaining < min_remaining:
        wait_s = remaining + 1
        print(
            f"TOTP window ends in {remaining}s — waiting {wait_s}s for a fresh code "
            f"(still local; no login yet)..."
        )
        time.sleep(wait_s)
    code = generate_totp_code(secret)
    print(f"TOTP ready (remaining ~{_totp_remaining(secret)}s in this window)")
    return code


async def _validate_token_with_limits(user_id: str, access_token: str) -> dict:
    """Single GetLimits call — proves the session works on PiConnect."""
    payload = {
        "uid": user_id,
        "actid": user_id,
    }
    body = f"jData={json.dumps(payload)}&jKey={access_token}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(f"{API_BASE}/Limits", content=body)
        data = resp.json()
    if not isinstance(data, dict):
        raise ConnectionError(f"Unexpected Limits response: {data!r}")
    if data.get("stat") == "Not_Ok":
        raise ConnectionError(str(data.get("emsg") or "Limits Not_Ok"))
    return data


async def _run(creds_path: Path, *, skip_limits: bool) -> int:
    creds = _parse_creds(creds_path)
    print("Creds file OK")
    print(f"  user_id:    {creds['user_id']}")
    print(f"  api_key:    {_mask(creds['api_key'])}")
    print(f"  api_secret: {_mask(creds['api_secret'])}")
    print(f"  totp:       {_mask(creds['totp_secret'], keep=3)}")
    print(f"  password:   {'*' * min(8, len(creds['password']))}")
    print()
    print("Auth steps: session → ftauth(password+TOTP) → apitoken → (optional) Limits")
    print("ONE login attempt only — no retries.")
    print()

    _wait_for_fresh_totp_window(creds["totp_secret"], min_remaining=12)

    print("Attempting Flattrade TOTP auto-login (single shot)...")
    try:
        session = await automated_login_with_credentials(
            user_id=creds["user_id"],
            password=creds["password"],
            api_key=creds["api_key"],
            api_secret=creds["api_secret"],
            totp_secret=creds["totp_secret"],
        )
    except Exception as exc:
        msg = str(exc)
        print("\nRESULT: LOGIN FAILED")
        print(f"  error: {msg}")
        if "INVALID_IP" in msg.upper():
            print(
                "  hint: Register this machine's public IP in Flattrade Wall → Pi → "
                "API Key → Primary IP, then retry once later."
            )
        print("\nStopped after one attempt (no retry).")
        return 1

    FlattradeSessionStore(TOKEN_OUT).save(session)
    print("\nRESULT: LOGIN OK")
    print(f"  user_id:     {session.user_id}")
    print(f"  source:      {session.source}")
    print(f"  expires_at:  {session.expires_at.isoformat()} (daily 05:00 IST)")
    print(f"  token_file:  {TOKEN_OUT}")
    print(f"  token:       {_mask(session.access_token, keep=6)}")

    if skip_limits:
        print("\nSkipped Limits check (--skip-limits).")
        return 0

    print("\nValidating token with one GetLimits call...")
    try:
        limits = await _validate_token_with_limits(session.user_id, session.access_token)
    except Exception as exc:
        print("\nRESULT: TOKEN ISSUED BUT LIMITS FAILED")
        print(f"  error: {exc}")
        print("  Login credentials likely OK; trading API / IP / product access may be blocked.")
        return 2

    cash = limits.get("cash") or limits.get("Cash") or limits.get("marginused")
    print("RESULT: CREDENTIALS WORKING")
    print(f"  limits.stat: {limits.get('stat')}")
    if cash is not None:
        print(f"  cash/margin field present: yes ({cash})")
    else:
        print(f"  limits keys: {sorted(limits.keys())[:12]}")
    print(f"  theoretical expiry: {next_token_expiry().isoformat()}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-shot Flattrade TOTP credential validation (no retries)"
    )
    parser.add_argument(
        "--creds-file",
        type=Path,
        default=DEFAULT_CREDS,
        help=f"Path to Flattrade.txt (default: {DEFAULT_CREDS})",
    )
    parser.add_argument(
        "--skip-limits",
        action="store_true",
        help="Only do login; skip GetLimits probe",
    )
    args = parser.parse_args()
    if not args.creds_file.exists():
        raise SystemExit(f"Creds file not found: {args.creds_file}")
    raise SystemExit(asyncio.run(_run(args.creds_file, skip_limits=args.skip_limits)))


if __name__ == "__main__":
    main()
