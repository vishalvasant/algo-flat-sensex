#!/usr/bin/env python3
"""Upstox OAuth login — generates daily access token.

Modes:
  1. Browser OAuth (default): python scripts/upstox_login.py
  2. Manual code paste: python scripts/upstox_login.py --code 'AUTH_CODE'
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading-engine" / "src"))

os.chdir(ROOT)
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import uvicorn

from algoflat.broker.upstox_auth import (
    UpstoxSessionStore,
    build_authorization_url,
    exchange_auth_code,
    parse_redirect_url,
    resolve_session,
)
from algoflat.broker.upstox_credentials import load_upstox_config
from algoflat.db.connection import close_pool, init_pool

app = FastAPI(title="Upstox OAuth", docs_url=None)
_login_result: dict | None = None

SUCCESS_HTML = """
<!DOCTYPE html>
<html><head><title>Algo-Flat — Upstox Login</title>
<style>
  body {{ font-family: system-ui; max-width: 520px; margin: 3rem auto; padding: 1rem; }}
  .ok {{ color: #166534; }} .err {{ color: #991b1b; }}
</style></head><body>
  <h2 class="{css}">{title}</h2>
  <p>{message}</p>
  <p>You can close this tab and return to the terminal.</p>
</body></html>
"""


@app.get("/callback")
@app.get("/")
async def oauth_callback(request: Request) -> HTMLResponse:
    global _login_result
    code = request.query_params.get("code")
    if not code:
        html = SUCCESS_HTML.format(
            css="err", title="Login failed", message="Missing ?code= in redirect URL."
        )
        _login_result = {"ok": False, "error": "missing_code"}
        return HTMLResponse(html, status_code=400)

    await init_pool()
    cfg = await load_upstox_config(force=True)
    if not cfg.api_key or not cfg.api_secret:
        html = SUCCESS_HTML.format(
            css="err",
            title="Configuration error",
            message="Save API key/secret in Settings → Broker or set UPSTOX_* in .env",
        )
        _login_result = {"ok": False, "error": "missing_api_credentials"}
        return HTMLResponse(html, status_code=500)

    try:
        session = await exchange_auth_code(
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
            redirect_uri=cfg.redirect_url,
            code=code,
        )
        token_path = Path(cfg.token_file)
        UpstoxSessionStore(token_path).save(session)
        _login_result = {"ok": True, "session": session.to_dict()}
        html = SUCCESS_HTML.format(
            css="ok",
            title="Login successful",
            message=f"Token saved. Expires {session.expires_at.strftime('%Y-%m-%d %H:%M %Z')}.",
        )
        return HTMLResponse(html)
    except Exception as exc:
        _login_result = {"ok": False, "error": str(exc)}
        html = SUCCESS_HTML.format(css="err", title="Token exchange failed", message=str(exc))
        return HTMLResponse(html, status_code=500)


async def run_login_server() -> dict:
    await init_pool()
    cfg = await load_upstox_config(force=True)
    if not cfg.api_key or not cfg.api_secret:
        print("ERROR: Save API key/secret in Settings → Broker or set UPSTOX_* in .env")
        sys.exit(1)

    _, port, path = parse_redirect_url(cfg.redirect_url)
    if path != "/callback":
        print(f"WARNING: Redirect path is {path}; this server only handles /callback")

    auth_url = build_authorization_url(cfg.api_key, cfg.redirect_url)
    print("\n" + "=" * 60)
    print("Upstox OAuth — open this URL in your browser:")
    print(auth_url)
    print("=" * 60 + "\n")
    with contextlib.suppress(Exception):
        webbrowser.open(auth_url)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
    return _login_result or {"ok": False, "error": "no_result"}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Upstox OAuth login")
    parser.add_argument("--code", help="Paste authorization code from redirect URL")
    args = parser.parse_args()

    try:
        await init_pool()
        cfg = await load_upstox_config(force=True)
        token_path = Path(cfg.token_file)

        existing = resolve_session(cfg, token_path)
        if existing and existing.is_valid and not args.code:
            print(f"Valid token already on disk (expires {existing.expires_at})")
            return

        if args.code:
            if not cfg.api_key or not cfg.api_secret:
                print("ERROR: Save API key/secret in Settings → Broker or set UPSTOX_* in .env")
                sys.exit(1)
            session = await exchange_auth_code(
                api_key=cfg.api_key,
                api_secret=cfg.api_secret,
                redirect_uri=cfg.redirect_url,
                code=args.code,
            )
            UpstoxSessionStore(token_path).save(session)
            print(f"Token saved to {token_path} (expires {session.expires_at})")
            return

        result = await run_login_server()
        if not result.get("ok"):
            print("Login failed:", result.get("error"))
            sys.exit(1)
        print("Login OK — token saved.")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
