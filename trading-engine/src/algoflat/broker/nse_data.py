"""NSE F&O bhavcopy for historical option universe + EOD OI/volume.

Uses ``nse-archives`` (``nsedata.nse``) — no login required.
Intraday option premiums are synthesised from spot 1m via Black-Scholes when
broker 1m history is unavailable (expired weeklies).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from algoflat.contract_selector.expiry import (
    format_expiry_tag,
    next_weekly_expiry_dates,
    parse_expiry_tag,
)
from algoflat.contract_selector.selector import ContractUniverse
from algoflat.models.events import Candle, CandleInterval, Instrument
from algoflat.option_data.greeks import DEFAULT_RATE, _bs_price, years_to_expiry

IST = ZoneInfo("Asia/Kolkata")

DEFAULT_IV = 0.14
_NSE_SPOT_TOKEN = "nse_nifty_index"


@dataclass(frozen=True)
class BhavOptionRow:
    strike: Decimal
    option_type: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    oi: int
    volume: int


def _require_nsedata():
    try:
        import nsedata.nse as nse  # type: ignore[import-untyped]

        return nse
    except ImportError as exc:
        raise ImportError(
            "NSE bhavcopy requires nse-archives: pip install nse-archives"
        ) from exc


def fetch_fo_bhav(day: date) -> Any:
    """Return pandas DataFrame for F&O UDiFF bhavcopy on ``day``."""
    nse = _require_nsedata()
    return nse.get("derivatives", "equity", "fo_bhav_udiff", day.isoformat())


def session_weekly_expiry(day: date, underlying: str = "NIFTY") -> date | None:
    cal = next_weekly_expiry_dates(
        underlying=underlying.upper(),
        as_of=day,
        count=1,
        include_today_if_expiry=True,
    )
    return cal[0] if cal else None


def _parse_bhav_expiry(val: Any) -> date | None:
    if val is None:
        return None
    s = str(val).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _nifty_options_for_expiry(bhav: Any, expiry: date) -> list[BhavOptionRow]:
    if bhav is None or len(bhav) == 0:
        return []
    sym = bhav["TckrSymb"] == "NIFTY"
    opts = bhav["OptnTp"].isin(["CE", "PE"])
    exp = bhav["XpryDt"].apply(_parse_bhav_expiry) == expiry
    sub = bhav[sym & opts & exp]
    rows: list[BhavOptionRow] = []
    for _, r in sub.iterrows():
        try:
            rows.append(
                BhavOptionRow(
                    strike=Decimal(str(r["StrkPric"])),
                    option_type=str(r["OptnTp"]),
                    open=Decimal(str(r.get("OpnPric") or 0)),
                    high=Decimal(str(r.get("HghPric") or 0)),
                    low=Decimal(str(r.get("LwPric") or 0)),
                    close=Decimal(str(r.get("ClsPric") or 0)),
                    oi=int(r.get("OpnIntrst") or 0),
                    volume=int(r.get("TtlTradgVol") or 0),
                )
            )
        except (TypeError, ValueError, KeyError):
            continue
    return rows


def _round_strike(spot: Decimal, step: Decimal) -> Decimal:
    return (spot / step).quantize(Decimal("1")) * step


def build_universe_from_bhav(
    bhav_rows: list[BhavOptionRow],
    *,
    mid_spot: Decimal,
    expiry: date,
    underlying: str = "NIFTY",
    band_steps: int = 5,
    strike_step: Decimal | None = None,
    lot_size: int = 65,
    exchange: str = "NFO",
) -> ContractUniverse:
    """ATM±band universe from NSE bhavcopy rows (real OI/volume metadata)."""
    ul = underlying.upper()
    step = strike_step or Decimal("50")
    atm = _round_strike(mid_spot, step)
    wanted = {atm + step * Decimal(i) for i in range(-band_steps, band_steps + 1)}
    expiry_tag = format_expiry_tag(expiry)
    expiry_dt = datetime.combine(expiry, dt_time(15, 30), tzinfo=IST)
    by_key: dict[tuple[Decimal, str], BhavOptionRow] = {
        (r.strike, r.option_type): r for r in bhav_rows
    }
    instruments: list[Instrument] = []
    atm_ce = atm_pe = None
    for strike in sorted(wanted):
        is_atm = strike == atm
        for side in ("CE", "PE"):
            row = by_key.get((strike, side))
            token = f"nse_{expiry_tag}_{int(strike)}{side}"
            tsym = f"{ul}{expiry_tag}{'C' if side == 'CE' else 'P'}{int(strike)}"
            inst = Instrument(
                exchange=exchange,
                token=token,
                tsym=tsym,
                underlying=ul,
                expiry_date=expiry_dt,
                strike=strike,
                option_type=side,
                lot_size=lot_size,
                is_atm=is_atm,
                in_band=True,
            )
            instruments.append(inst)
            if is_atm and side == "CE":
                atm_ce = inst
            if is_atm and side == "PE":
                atm_pe = inst
    return ContractUniverse(
        spot=mid_spot,
        atm_strike=atm,
        expiry_symbol=expiry_tag,
        instruments=instruments,
        atm_ce=atm_ce,
        atm_pe=atm_pe,
    )


def _bs_premium(
    spot: Decimal,
    strike: Decimal,
    side: str,
    when: datetime,
    expiry: date,
    iv: float = DEFAULT_IV,
) -> Decimal:
    t = years_to_expiry(expiry, now=when)
    px = _bs_price(float(spot), float(strike), t, DEFAULT_RATE, iv, side)
    return Decimal(str(round(max(px, 0.05), 2)))


def build_bs_option_m1_series(
    universe: ContractUniverse,
    spot_m1: list[Candle],
    bhav_rows: list[BhavOptionRow],
    *,
    expiry: date,
    iv: float = DEFAULT_IV,
) -> dict[str, list[Candle]]:
    """1m option candles from spot path + BS; OI/volume from bhav EOD."""
    if not spot_m1 or not universe.instruments:
        return {}
    meta: dict[tuple[Decimal, str], BhavOptionRow] = {
        (r.strike, r.option_type): r for r in bhav_rows
    }
    out: dict[str, list[Candle]] = {}
    for inst in universe.instruments:
        row = meta.get((inst.strike, inst.option_type))
        oi = int(row.oi) if row else 0
        vol = int(row.volume) if row else 0
        bars: list[Candle] = []
        for sb in spot_m1:
            when = sb.ts
            o = _bs_premium(sb.open, inst.strike, inst.option_type, when, expiry, iv)
            h = _bs_premium(sb.high, inst.strike, inst.option_type, when, expiry, iv)
            l = _bs_premium(sb.low, inst.strike, inst.option_type, when, expiry, iv)
            c = _bs_premium(sb.close, inst.strike, inst.option_type, when, expiry, iv)
            hi = max(o, h, l, c)
            lo = min(o, h, l, c)
            bars.append(
                Candle(
                    instrument_token=inst.token,
                    ts=sb.ts,
                    open=o,
                    high=hi,
                    low=lo,
                    close=c,
                    volume=vol,
                    oi=oi,
                    interval=CandleInterval.M1,
                    vwap=c,
                )
            )
        if bars:
            out[inst.token] = bars
    return out


def supplement_session_from_nse(
    snap: dict[str, Any],
    day: date,
    *,
    underlying: str = "NIFTY",
    band_steps: int = 5,
) -> dict[str, Any]:
    """Merge NSE bhav universe + BS option 1m into an existing session payload."""
    m1 = (snap.get("all_candles") or {}).get(CandleInterval.M1) or []
    opts = snap.get("option_series") or {}
    universe = snap.get("universe")
    insts = list(getattr(universe, "instruments", None) or [])
    syn_only = not insts or all(
        str(i.token).startswith(("syn_", "nse_")) for i in insts
    )
    need_opts = len(opts) < 4
    need_uni = syn_only or not insts
    if not need_opts and not need_uni:
        return snap
    if len(m1) < 30:
        print("  NSE supplement skipped — need spot 1m bars first")
        return snap

    expiry = session_weekly_expiry(day, underlying)
    if expiry is None:
        print("  NSE supplement skipped — no weekly expiry for session")
        return snap

    print(f"==> Supplementing session from NSE bhavcopy ({day.isoformat()}, expiry {expiry})...")
    try:
        bhav = fetch_fo_bhav(day)
        bhav_rows = _nifty_options_for_expiry(bhav, expiry)
    except Exception as exc:
        print(f"  NSE bhavcopy fetch failed: {exc}")
        return snap

    if not bhav_rows:
        print(f"  NSE: no NIFTY options for expiry {expiry} on {day}")
        return snap

    mid_spot = m1[len(m1) // 2].close
    new_uni = build_universe_from_bhav(
        bhav_rows,
        mid_spot=mid_spot,
        expiry=expiry,
        underlying=underlying,
        band_steps=band_steps,
    )
    option_series = build_bs_option_m1_series(
        new_uni, m1, bhav_rows, expiry=expiry
    )

    meta = snap.setdefault("meta", {})
    meta["universe_from"] = "nse_bhav"
    meta["options_from"] = "nse_bs"
    meta["nse_bhav_rows"] = len(bhav_rows)
    meta["option_tokens"] = len(option_series)
    if universe and getattr(universe, "expiry_symbol", None):
        meta["expected_expiry"] = new_uni.expiry_symbol

    snap["universe"] = new_uni
    snap["option_series"] = option_series
    print(
        f"  NSE: universe={len(new_uni.instruments)} instruments, "
        f"option_series={len(option_series)} (BS intraday, bhav OI/vol)"
    )
    return snap
