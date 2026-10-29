"""Upstox historical market data for backtests (index + options).

Uses Upstox v2 History APIs. Credentials: UPSTOX_ACCESS_TOKEN env or Upstox.txt
(APIKEY / APISecret / AccessToken lines) at repo root.
"""
from __future__ import annotations

import os
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from algoflat.contract_selector.expiry import format_expiry_tag, next_weekly_expiry_dates
from algoflat.contract_selector.selector import ContractUniverse
from algoflat.features.indicators import aggregate_from_m1, aggregate_from_m5
from algoflat.models.events import Candle, CandleInterval, Instrument

IST = ZoneInfo("Asia/Kolkata")
_BASE = "https://api.upstox.com/v2"
_NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"
_UPSTOX_SPOT_TOKEN = "upstox_nifty_index"


@dataclass(frozen=True)
class UpstoxCredentials:
    api_key: str
    api_secret: str
    access_token: str


def _repo_roots() -> list[Path]:
    here = Path(__file__).resolve()
    roots: list[Path] = []
    # Walk up safely (Docker image paths are shallow: /app/src/...)
    for i, parent in enumerate(here.parents):
        if i < 2:
            continue
        if i > 8:
            break
        roots.append(parent)
        if (parent / "algo-flat").is_dir() or (parent / "UpStox.txt").exists() or (parent / "Upstox.txt").exists():
            break
    cwd = Path.cwd()
    if cwd not in roots:
        roots.append(cwd)
    out: list[Path] = []
    for p in roots:
        if (p / "algo-flat").is_dir():
            out.append(p)
        elif (p / "UpStox.txt").exists() or (p / "Upstox.txt").exists():
            out.append(p)
    return out or [Path.cwd(), Path("/app")]


def load_upstox_credentials() -> UpstoxCredentials | None:
    token = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    api_key = os.environ.get("UPSTOX_API_KEY", "").strip()
    api_secret = os.environ.get("UPSTOX_API_SECRET", "").strip()
    if token:
        return UpstoxCredentials(
            api_key=api_key or "env",
            api_secret=api_secret or "env",
            access_token=token,
        )
    for root in _repo_roots():
        for name in ("UpStox.txt", "Upstox.txt"):
            path = root / name
            if not path.exists():
                # Also check parent of algo-flat repo when running from /app
                alt = root.parent / name if root.name not in ("", "/") else None
                path = alt if alt and alt.exists() else path
            if not path.exists():
                continue
            parsed: dict[str, str] = {}
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                parsed[key.strip()] = val.strip()
            # Prefer Analytic-Token for historical; AccessToken for trading APIs.
            access = parsed.get("Analytic-Token") or parsed.get("AccessToken", "")
            if access:
                return UpstoxCredentials(
                    api_key=parsed.get("APIKEY", parsed.get("API", "")),
                    api_secret=parsed.get("APISecret", parsed.get("Secrate", parsed.get("Secret", ""))),
                    access_token=access,
                )
    return None


class UpstoxHistoryClient:
    """Read-only Upstox historical / option contract client."""

    def __init__(self, creds: UpstoxCredentials) -> None:
        self._creds = creds
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {creds.access_token}",
                "Accept": "application/json",
            }
        )
        self._last_call = 0.0

    @classmethod
    def from_env(cls) -> UpstoxHistoryClient | None:
        creds = load_upstox_credentials()
        return cls(creds) if creds else None

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < 0.12:
            time.sleep(0.12 - elapsed)
        self._last_call = time.monotonic()

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._throttle()
        url = f"{_BASE}{path}"
        resp = self._session.get(url, params=params, timeout=45)
        if resp.status_code == 401:
            raise RuntimeError("Upstox access token expired — refresh AccessToken in Upstox.txt")
        if not resp.ok:
            try:
                body = resp.json()
                msg = body.get("errors") or body.get("message") or resp.text
            except Exception:
                msg = resp.text[:200]
            raise RuntimeError(f"Upstox API {resp.status_code}: {msg}")
        return resp.json()

    @staticmethod
    def _parse_candles(
        rows: list[list[Any]],
        *,
        instrument_token: str,
        interval: CandleInterval,
    ) -> list[Candle]:
        out: list[Candle] = []
        for row in rows or []:
            if len(row) < 5:
                continue
            ts_raw = row[0]
            if isinstance(ts_raw, str):
                ts = datetime.fromisoformat(ts_raw)
            else:
                ts = datetime.fromtimestamp(float(ts_raw), tz=IST)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            ts = ts.astimezone(timezone.utc)
            vol = int(row[5]) if len(row) > 5 and row[5] is not None else None
            oi = int(row[6]) if len(row) > 6 and row[6] is not None else None
            out.append(
                Candle(
                    instrument_token=instrument_token,
                    ts=ts,
                    open=Decimal(str(row[1])),
                    high=Decimal(str(row[2])),
                    low=Decimal(str(row[3])),
                    close=Decimal(str(row[4])),
                    volume=vol,
                    oi=oi,
                    interval=interval,
                )
            )
        return sorted(out, key=lambda c: c.ts)

    def get_intraday_candles(
        self,
        instrument_key: str,
        interval: str,
    ) -> list[Candle]:
        """Today's session OHLCV (available while market is open / just after close)."""
        enc = urllib.parse.quote(instrument_key, safe="")
        path = f"/historical-candle/intraday/{enc}/{interval}"
        payload = self._get(path)
        token = instrument_key.split("|")[-1]
        return self._parse_candles(
            payload.get("data", {}).get("candles", []),
            instrument_token=token,
            interval=CandleInterval.M1,
        )

    def get_historical_candles(
        self,
        instrument_key: str,
        interval: str,
        day: date,
    ) -> list[Candle]:
        """Active instrument OHLCV for a single session day."""
        enc = urllib.parse.quote(instrument_key, safe="")
        day_s = day.isoformat()
        path = f"/historical-candle/{enc}/{interval}/{day_s}/{day_s}"
        payload = self._get(path)
        candle_interval = {
            "1minute": CandleInterval.M1,
            "30minute": CandleInterval.M1,
        }.get(interval, CandleInterval.M1)
        token = instrument_key.split("|")[-1]
        return self._parse_candles(
            payload.get("data", {}).get("candles", []),
            instrument_token=token,
            interval=candle_interval,
        )

    def get_session_m1_candles(
        self,
        instrument_key: str,
        day: date,
        *,
        instrument_token: str | None = None,
    ) -> list[Candle]:
        """Historical day candles; intraday fallback when history empty (same session day)."""
        token = instrument_token or instrument_key.split("|")[-1]
        bars = self.get_historical_candles(instrument_key, "1minute", day)
        if len(bars) < 30:
            try:
                intraday = self.get_intraday_candles(instrument_key, "1minute")
            except RuntimeError:
                intraday = []
            if len(intraday) > len(bars):
                bars = intraday
        for c in bars:
            c.instrument_token = token
        return bars

    def get_expired_historical_candles(
        self,
        expired_instrument_key: str,
        interval: str,
        day: date,
    ) -> list[Candle]:
        """Expired contract OHLCV (Upstox Plus)."""
        enc = urllib.parse.quote(expired_instrument_key, safe="")
        day_s = day.isoformat()
        path = f"/expired-instruments/historical-candle/{enc}/{interval}/{day_s}/{day_s}"
        payload = self._get(path)
        token = expired_instrument_key.split("|")[1] if "|" in expired_instrument_key else expired_instrument_key
        return self._parse_candles(
            payload.get("data", {}).get("candles", []),
            instrument_token=token,
            interval=CandleInterval.M1,
        )

    def get_option_contracts(self, expiry_date: date, *, expired: bool) -> list[dict[str, Any]]:
        if expired:
            path = "/expired-instruments/option/contract"
        else:
            path = "/option/contract"
        payload = self._get(
            path,
            params={
                "instrument_key": _NIFTY_INDEX_KEY,
                "expiry_date": expiry_date.isoformat(),
            },
        )
        return list(payload.get("data") or [])

    def get_index_session_candles(self, day: date) -> dict[CandleInterval, list[Candle]]:
        m1 = self.get_session_m1_candles(
            _NIFTY_INDEX_KEY, day, instrument_token=_UPSTOX_SPOT_TOKEN
        )
        m3 = aggregate_from_m1(m1, 3)
        m5 = aggregate_from_m1(m1, 5)
        m15 = aggregate_from_m5(m5, 15)
        return {
            CandleInterval.M1: m1,
            CandleInterval.M3: m3,
            CandleInterval.M5: m5,
            CandleInterval.M15: m15,
        }

    def discover_nifty_future_m1(
        self,
        day: date,
        cash_m1: list[Candle],
        *,
        candidate_tokens: list[str] | None = None,
    ) -> tuple[str, list[Candle]] | None:
        """Nearest live NIFTY index future (front month) with full session M1."""
        if not cash_m1:
            return None
        cash_avg = float(sum(c.close for c in cash_m1) / len(cash_m1))
        probes = list(candidate_tokens or [])
        probes.extend(["48704", "61471", "68407", "61093", "59182", "52738"])
        seen: set[str] = set()
        best: tuple[str, list[Candle], float] | None = None
        for tok in probes:
            tok = str(tok).strip()
            if not tok or tok in seen:
                continue
            seen.add(tok)
            key = f"NSE_FO|{tok}"
            try:
                bars = self.get_session_m1_candles(key, day, instrument_token=tok)
            except RuntimeError:
                continue
            if len(bars) < 30:
                continue
            avg = float(sum(c.close for c in bars) / len(bars))
            if avg < cash_avg * 1.003:
                continue
            if best is None or avg < best[2]:
                best = (tok, bars, avg)
        if best is None:
            return None
        return best[0], best[1]

    def candles_from_m1(
        self, m1: list[Candle]
    ) -> dict[CandleInterval, list[Candle]]:
        m3 = aggregate_from_m1(m1, 3)
        m5 = aggregate_from_m1(m1, 5)
        m15 = aggregate_from_m5(m5, 15)
        return {
            CandleInterval.M1: m1,
            CandleInterval.M3: m3,
            CandleInterval.M5: m5,
            CandleInterval.M15: m15,
        }


def _round_strike(spot: Decimal, step: Decimal) -> Decimal:
    return (spot / step).quantize(Decimal("1")) * step


def _contracts_to_universe(
    contracts: list[dict[str, Any]],
    *,
    mid_spot: Decimal,
    step: Decimal,
    band_steps: int = 5,
) -> ContractUniverse:
    atm = _round_strike(mid_spot, step)
    wanted = {atm + step * Decimal(i) for i in range(-band_steps, band_steps + 1)}
    instruments: list[Instrument] = []
    atm_ce = atm_pe = None
    expiry_symbol = ""
    expiry_dt: datetime | None = None
    for row in contracts:
        strike = Decimal(str(row["strike_price"]))
        if strike not in wanted:
            continue
        opt = str(row["instrument_type"])
        if opt not in ("CE", "PE"):
            continue
        exp_s = str(row["expiry"])
        exp_d = date.fromisoformat(exp_s)
        expiry_dt = datetime.combine(exp_d, dt_time(0), tzinfo=IST)
        expiry_symbol = format_expiry_tag(exp_d)
        inst = Instrument(
            exchange="NFO",
            token=str(row["exchange_token"]),
            tsym=str(row["trading_symbol"]),
            underlying=str(row.get("underlying_symbol") or "NIFTY"),
            expiry_date=expiry_dt,
            strike=strike,
            option_type=opt,
            lot_size=int(row.get("lot_size") or 65),
            tick_size=Decimal(str(row.get("tick_size") or "0.05")),
            is_atm=strike == atm,
            in_band=True,
        )
        instruments.append(inst)
        if inst.is_atm and opt == "CE":
            atm_ce = inst
        if inst.is_atm and opt == "PE":
            atm_pe = inst
    instruments.sort(key=lambda i: (i.strike, i.option_type))
    return ContractUniverse(
        spot=mid_spot,
        atm_strike=atm,
        expiry_symbol=expiry_symbol,
        instruments=instruments,
        atm_ce=atm_ce,
        atm_pe=atm_pe,
    )


def fetch_session_from_upstox(
    day: date,
    *,
    underlying: str = "NIFTY",
    band_steps: int = 5,
    client: UpstoxHistoryClient | None = None,
) -> dict[str, Any] | None:
    """Fetch full NIFTY session snap payload from Upstox (index + ATM±band options)."""
    if underlying.upper() != "NIFTY":
        raise ValueError("Upstox fetch currently supports NIFTY only")

    client = client or UpstoxHistoryClient.from_env()
    if client is None:
        return None

    cal = next_weekly_expiry_dates(
        underlying="NIFTY", as_of=day, count=1, include_today_if_expiry=True
    )
    if not cal:
        return None
    expiry_d = cal[0]
    today = datetime.now(tz=IST).date()
    expired_chain = expiry_d < today

    cash_candles = client.get_index_session_candles(day)
    cash_m1 = cash_candles.get(CandleInterval.M1) or []
    if len(cash_m1) < 30:
        return None

    fut_token = ""
    fut_m1: list[Candle] = []
    fut_discovered = client.discover_nifty_future_m1(day, cash_m1)
    if fut_discovered:
        fut_token, fut_m1 = fut_discovered
        all_candles = client.candles_from_m1(fut_m1)
        print(
            f"  Upstox fut token={fut_token} m1={len(fut_m1)} "
            f"(cash m1={len(cash_m1)})"
        )
    else:
        all_candles = cash_candles
        fut_m1 = list(cash_m1)
        print("  WARN: no NIFTY fut series from Upstox — using index for spot")

    m1 = all_candles.get(CandleInterval.M1) or []
    mid_spot = m1[len(m1) // 2].close
    step = Decimal("50")

    try:
        contracts = client.get_option_contracts(expiry_d, expired=expired_chain)
    except RuntimeError as exc:
        print(f"  Upstox option chain unavailable ({exc}) — index-only session")
        contracts = []

    universe = _contracts_to_universe(
        contracts, mid_spot=mid_spot, step=step, band_steps=band_steps
    )
    if not universe.instruments:
        print(
            f"  WARN: Upstox returned no option contracts for expiry {expiry_d} "
            f"(expired={expired_chain}; Plus plan required for expired chains)"
        )

    option_series: dict[str, list[Candle]] = {}
    for inst in universe.instruments:
        try:
            if expired_chain:
                row = next(
                    (
                        c
                        for c in contracts
                        if str(c.get("exchange_token")) == inst.token
                    ),
                    None,
                )
                if not row:
                    continue
                bars = client.get_expired_historical_candles(
                    str(row["instrument_key"]), "1minute", day
                )
            else:
                key = f"NSE_FO|{inst.token}"
                bars = client.get_session_m1_candles(
                    key, day, instrument_token=inst.token
                )
            if bars:
                option_series[inst.token] = bars
        except RuntimeError as exc:
            print(f"    skip {inst.tsym}: {exc}")
            continue

    prior_day = day - timedelta(days=1)
    while prior_day.weekday() >= 5:
        prior_day -= timedelta(days=1)
    prior_m5: list[Candle] = []
    try:
        prior_candles = client.get_index_session_candles(prior_day)
        prior_m5 = prior_candles.get(CandleInterval.M5) or []
    except Exception:
        prior_day = None

    print(
        f"==> Upstox session loaded for {day.isoformat()} "
        f"(fut 1m={len(m1)} cash 1m={len(cash_m1)} "
        f"options={len(option_series)} expiry={universe.expiry_symbol})"
    )
    spot_token = fut_token or _UPSTOX_SPOT_TOKEN
    return {
        "all_candles": all_candles,
        "cash_m1": cash_m1,
        "fut_m1": fut_m1,
        "prior_day": prior_day,
        "prior_m5": prior_m5,
        "universe": universe,
        "option_series": option_series,
        "meta": {
            "source": "upstox",
            "expected_expiry": universe.expiry_symbol,
            "option_tokens": len(option_series),
            "spot_bars": {k.value: len(v) for k, v in all_candles.items()},
            "cash_token": _UPSTOX_SPOT_TOKEN,
            "fut_token": fut_token,
            "spot_tokens": [spot_token, _UPSTOX_SPOT_TOKEN],
            "dual_spot": bool(fut_token and fut_m1 and cash_m1 is not fut_m1),
        },
    }
