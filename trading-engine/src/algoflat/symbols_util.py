"""SENSEX symbol helpers and index display tickers."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from algoflat.config import AppConfig
from algoflat.contract_selector.expiry import include_expiry_day, parse_expiry_tag

logger = structlog.get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")

_NIFTY_FUT_TSYM = re.compile(r"^NIFTY(\d{2}[A-Z]{3}\d{2})F$")
_SENSEX_FUT_TSYM = re.compile(r"^SENSEX(\d{2}[A-Z]{3}\d{2})F$")


def fut_expiry_label_from_tsym(tsym: str) -> str | None:
    """Human label for top-bar FUT card, e.g. SENSEX26NOV26F → 26 NOV."""
    text = str(tsym).upper()
    m = _SENSEX_FUT_TSYM.match(text) or _NIFTY_FUT_TSYM.match(text)
    if not m:
        return None
    parsed = parse_expiry_tag(m.group(1))
    if parsed is None:
        return m.group(1)
    return parsed.strftime("%d %b").upper()


def list_underlyings(config: AppConfig) -> list[dict[str, Any]]:
    rows = config.symbols.get("underlyings")
    if isinstance(rows, list) and rows:
        return rows
    return [
        {
            "symbol": config.symbols.get("underlying", "SENSEX"),
            "display_name": config.symbols.get("underlying", "SENSEX"),
            "exchange_spot": config.symbols.get("exchange_spot", "BSE"),
            "exchange_options": config.symbols.get("exchange_options", "BFO"),
            "spot_token": config.symbols.get("spot_token", ""),
            "strike_step": config.symbols.get("strike_step", 100),
            "strike_band_points": config.symbols.get("strike_band_points", 500),
            "atm_strike_steps": config.symbols.get("atm_strike_steps", 5),
            "fallback_spot": 80000,
            "lot_size": 20,
        }
    ]


def list_index_tickers(config: AppConfig) -> list[dict[str, Any]]:
    rows = config.symbols.get("index_tickers")
    if isinstance(rows, list):
        return [row for row in rows if row.get("symbol")]
    return []


def list_sector_indices(config: AppConfig) -> list[dict[str, Any]]:
    rows = config.symbols.get("sector_indices")
    if isinstance(rows, list):
        return [row for row in rows if row.get("symbol")]
    return []


def list_mi_index_symbols(config: AppConfig) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in list_index_tickers(config) + list_sector_indices(config):
        sym = str(row.get("symbol", "")).upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(row)
    return out


def is_ticker_only(config: AppConfig, symbol: str) -> bool:
    sym = symbol.upper()
    return any(str(row.get("symbol", "")).upper() == sym for row in list_index_tickers(config))


def index_quote_context(config: AppConfig, symbol: str) -> tuple[str, str] | None:
    """Exchange and token for a display index (VIX or a sector), not the primary cash token."""
    sym = symbol.upper().removesuffix("_FUT")
    for row in list_mi_index_symbols(config):
        if str(row.get("symbol", "")).upper() != sym:
            continue
        token = str(row.get("spot_token", "")).strip()
        if not token:
            return None
        return str(row.get("exchange", "NSE")), token
    return None


def uses_futures_price(sym_cfg: dict[str, Any]) -> bool:
    return str(sym_cfg.get("price_source", "")).lower() == "futures"


def price_exchange_for(sym_cfg: dict[str, Any]) -> str:
    if uses_futures_price(sym_cfg):
        return str(
            sym_cfg.get("exchange_futures")
            or sym_cfg.get("exchange_options")
            or sym_cfg.get("exchange_spot", "NFO")
        )
    return str(sym_cfg.get("exchange_spot", "NSE"))


def price_context(sym_cfg: dict[str, Any]) -> tuple[str, str]:
    """Exchange + token for candles, quotes, and signal price (futures when configured)."""
    return (
        price_exchange_for(sym_cfg),
        str(sym_cfg.get("spot_token", "")),
    )


def index_spot_exchange(sym_cfg: dict[str, Any]) -> str:
    """NSE (or other) cash index exchange for ATM / option chain."""
    if uses_futures_price(sym_cfg):
        return str(sym_cfg.get("index_spot_exchange", "NSE"))
    return str(sym_cfg.get("exchange_spot", "NSE"))


def index_spot_context(sym_cfg: dict[str, Any]) -> tuple[str, str]:
    """Exchange + token for cash index spot (SENSEX on BSE)."""
    return (
        index_spot_exchange(sym_cfg),
        str(sym_cfg.get("index_spot_token") or sym_cfg.get("spot_token", "")),
    )


def uses_dual_index_spot(sym_cfg: dict[str, Any]) -> bool:
    """True when futures drive candles but cash index is shown in the header."""
    return uses_futures_price(sym_cfg) and bool(
        str(sym_cfg.get("index_spot_exchange", "NSE")).strip()
    )


def symbols_for(config: AppConfig, symbol: str) -> dict[str, Any]:
    sym = symbol.upper()
    base = dict(getattr(config, "_symbols_root", None) or config.symbols)
    for row in list_underlyings(config):
        if str(row.get("symbol", "")).upper() == sym:
            merged = {**base, **row, "underlying": sym}
            return merged
    for row in list_index_tickers(config):
        if str(row.get("symbol", "")).upper() == sym:
            merged = {**base, **row, "underlying": sym}
            return merged
    base["underlying"] = sym
    return base


def strike_band_points(sym_cfg: dict[str, Any]) -> float:
    steps = int(sym_cfg.get("atm_strike_steps", sym_cfg.get("atm_band_steps", 5)))
    step = float(sym_cfg.get("strike_step", 100))
    explicit = sym_cfg.get("strike_band_points")
    if explicit is not None:
        return float(explicit)
    return steps * step


def apply_active_underlying(config: AppConfig, symbol: str) -> dict[str, Any]:
    merged = symbols_for(config, symbol)
    config.symbols.update(merged)
    return merged


POOL_UNDERLYING = "POOL"

# Exchange lot sizes (SENSEX index options = 20 qty per lot as of 2026).
SENSEX_LOT_SIZE = 20
NIFTY_LOT_SIZE = 65  # kept for shared helpers / tests

# SENSEX index sanity band — rejects wrong scrip matches.
SENSEX_SPOT_MIN = Decimal("40000")
SENSEX_SPOT_MAX = Decimal("150000")
NIFTY_SPOT_MIN = Decimal("12000")
NIFTY_SPOT_MAX = Decimal("45000")


def is_sane_sensex_spot(spot: Decimal | float | None) -> bool:
    if spot is None:
        return False
    try:
        px = Decimal(str(spot))
    except Exception:
        return False
    return SENSEX_SPOT_MIN <= px <= SENSEX_SPOT_MAX


def is_sane_nifty_spot(spot: Decimal | float | None) -> bool:
    """Backward-compatible name used across the engine — validates SENSEX spot here."""
    return is_sane_sensex_spot(spot)


def lot_size_for_tsym(tsym: str, *, metadata_lot_size: int | None = None) -> int:
    """Resolve lot size from scanner metadata or trading symbol prefix."""
    upper = (tsym or "").upper()
    if upper.startswith("SENSEX"):
        if metadata_lot_size is not None and metadata_lot_size >= SENSEX_LOT_SIZE:
            return int(metadata_lot_size)
        return SENSEX_LOT_SIZE
    if upper.startswith("NIFTY"):
        if metadata_lot_size is not None and metadata_lot_size >= NIFTY_LOT_SIZE:
            return int(metadata_lot_size)
        return NIFTY_LOT_SIZE
    if metadata_lot_size is not None and metadata_lot_size > 0:
        return int(metadata_lot_size)
    return SENSEX_LOT_SIZE


def uses_pooled_capital(config: AppConfig) -> bool:
    return bool(config.risk.get("use_pooled_capital", True))


def total_account_capital(config: AppConfig) -> Decimal:
    return Decimal(str(config.risk.get("account_capital_inr", 50000)))


def risk_underlying_key(config: AppConfig, symbol: str | None = None) -> str:
    """DB key for daily_risk_state — single POOL row when capital is shared."""
    if uses_pooled_capital(config):
        return POOL_UNDERLYING
    sym = (symbol or config.symbols.get("underlying", "SENSEX")).upper()
    return sym


def capital_for(config: AppConfig, symbol: str) -> Decimal:
    if uses_pooled_capital(config):
        return total_account_capital(config)
    sym = symbol.upper()
    alloc = config.risk.get("capital_allocations") or {}
    if sym in alloc:
        return Decimal(str(alloc[sym]))
    rows = list_underlyings(config)
    if len(rows) > 1:
        total = total_account_capital(config)
        return total / Decimal(len(rows))
    return total_account_capital(config)


def fallback_spot(config: AppConfig, symbol: str) -> Decimal:
    merged = symbols_for(config, symbol)
    raw = merged.get("fallback_spot", 80000)
    return Decimal(str(raw))


def _expiry_from_search_row(row: dict[str, Any]) -> date | None:
    for key in ("exd", "expiry", "Expiry"):
        raw = row.get(key)
        if not raw:
            continue
        text = str(raw).strip()
        for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        parsed = parse_expiry_tag(text.upper())
        if parsed is not None:
            return parsed
    tsym = str(row.get("tsym", "")).upper()
    m = _SENSEX_FUT_TSYM.match(tsym) or _NIFTY_FUT_TSYM.match(tsym)
    if m:
        return parse_expiry_tag(m.group(1))
    return None


def _fut_tsym_matches(symbol: str, tsym: str) -> bool:
    ul = symbol.upper()
    ts = tsym.upper()
    if ul == "SENSEX":
        return bool(_SENSEX_FUT_TSYM.match(ts)) or (ts.startswith("SENSEX") and ts.endswith(("F", "FUT")))
    if ul == "NIFTY":
        return bool(_NIFTY_FUT_TSYM.match(ts))
    return ts.startswith(ul) and ts.endswith(("F", "FUT"))


def _is_futidx_row(row: dict[str, Any], symbol: str) -> bool:
    inst = str(row.get("instname", row.get("Instrument", ""))).upper()
    if inst and inst != "FUTIDX":
        return False
    tsym = str(row.get("tsym", row.get("Tradingsymbol", ""))).upper()
    if not tsym:
        return False
    return _fut_tsym_matches(symbol, tsym)


async def resolve_index_future_token(
    broker: Any,
    symbol: str,
    exchange: str,
) -> tuple[str, str] | None:
    """Nearest live index future (FUTIDX) token for candles and price."""
    ul = symbol.upper()
    fut_rows: list[dict[str, Any]] = []
    try:
        rows = await broker.search_scrip(exchange, ul)
        fut_rows = [r for r in rows if _is_futidx_row(r, ul)]
    except Exception:
        logger.exception("futures_token_search_failed", symbol=ul, exchange=exchange)

    if not fut_rows:
        from algoflat.contract_selector.scripmaster import nearest_future_from_scripmaster

        fallback = nearest_future_from_scripmaster(underlying=ul, exchange=exchange)
        if fallback is None:
            return None
        token, tsym = fallback
        return token, tsym

    today = datetime.now(IST).date()
    include_today = include_expiry_day(today)

    def _sort_key(row: dict[str, Any]) -> tuple[int, str]:
        exp = _expiry_from_search_row(row)
        if exp is not None:
            return (0, exp.isoformat())
        return (1, str(row.get("tsym", "")))

    fut_rows.sort(key=_sort_key)
    for row in fut_rows:
        exp = _expiry_from_search_row(row)
        if exp is not None:
            if exp > today:
                pass
            elif exp < today:
                continue
            elif not include_today:
                continue
        token = str(row.get("token", row.get("Token", ""))).strip()
        tsym = str(row.get("tsym", row.get("Tradingsymbol", ""))).strip()
        if token:
            return token, tsym
    return None


async def resolve_all_spot_tokens(config: AppConfig, broker: Any) -> dict[str, str]:
    """Resolve price tokens — current-month FUT when price_source is futures."""
    resolved: dict[str, str] = {}
    for row in list_underlyings(config):
        sym = str(row.get("symbol", "")).upper()
        if not sym:
            continue

        if uses_futures_price(row):
            exchange = price_exchange_for(row)
            row["exchange_spot"] = exchange
            existing = str(row.get("spot_token", "")).strip()
            if existing:
                resolved[sym] = existing
            else:
                result = await resolve_index_future_token(broker, sym, exchange)
                if result:
                    token, tsym = result
                    row["spot_token"] = token
                    row["fut_tsym"] = tsym
                    resolved[sym] = token
                    logger.info(
                        "futures_token_resolved",
                        symbol=sym,
                        exchange=exchange,
                        token=token,
                        tsym=tsym,
                    )
            idx_exchange = index_spot_exchange(row)
            idx_token = str(row.get("index_spot_token", "")).strip()
            if not idx_token:
                cash = await resolve_cash_index_token(broker, sym, idx_exchange)
                if cash:
                    idx_token, idx_tsym = cash
                    row["index_spot_token"] = idx_token
                    logger.info(
                        "cash_index_token_resolved",
                        symbol=sym,
                        exchange=idx_exchange,
                        token=idx_token,
                        tsym=idx_tsym,
                    )
            continue

        existing = str(row.get("spot_token", "")).strip()
        if existing:
            resolved[sym] = existing

    if resolved:
        primary = str(config.symbols.get("underlying", "SENSEX")).upper()
        if primary in resolved:
            merged = apply_active_underlying(config, primary)
            merged["spot_token"] = resolved[primary]
    return resolved


NSE_CASH_INDEX: dict[str, tuple[str, str]] = {
    "NIFTY": ("26000", "NIFTY 50"),
    "BANKNIFTY": ("26009", "NIFTY BANK"),
}

BSE_CASH_INDEX: dict[str, tuple[str, str]] = {
    "SENSEX": ("1", "SENSEX"),
    "BANKEX": ("12", "BANKEX"),
}


async def resolve_cash_index_token(
    broker: Any,
    symbol: str,
    exchange: str,
) -> tuple[str, str] | None:
    """Resolve cash index token for NSE/BSE underlyings."""
    ul = symbol.upper()
    ex = exchange.upper()
    if ex == "NSE" and ul in NSE_CASH_INDEX:
        token, tsym = NSE_CASH_INDEX[ul]
        return token, tsym
    if ex == "BSE" and ul in BSE_CASH_INDEX:
        token, tsym = BSE_CASH_INDEX[ul]
        return token, tsym
    try:
        rows = await broker.search_scrip(exchange, ul)
    except Exception:
        logger.exception("cash_index_search_failed", symbol=ul, exchange=exchange)
        return None
    exact: tuple[str, str] | None = None
    prefix: tuple[str, str] | None = None
    for row in rows or []:
        tsym = str(row.get("tsym", row.get("Tradingsymbol", ""))).upper()
        token = str(row.get("token", row.get("Token", ""))).strip()
        if not token:
            continue
        if ul == "NIFTY" and "NEXT" in tsym:
            continue
        if tsym in (ul, f"{ul} 50", "NIFTY 50", "SENSEX"):
            exact = (token, tsym)
            break
        if prefix is None and tsym.startswith(ul) and "NEXT" not in tsym:
            prefix = (token, tsym)
    return exact or prefix


async def resolve_index_ticker_token(
    broker: Any,
    symbol: str,
    exchange: str,
) -> tuple[str, str] | None:
    """Resolve cash/index token for display-only tickers (e.g. INDIAVIX on NSE)."""
    ul = symbol.upper()
    static_tokens: dict[str, tuple[str, str]] = {
        # Flattrade/Noren NSE token (was Upstox-shaped "India VIX" string)
        "INDIAVIX": ("26017", "NSE"),
    }
    hit = static_tokens.get(ul)
    if hit:
        token, ex = hit
        if exchange.upper() == ex.upper():
            return token, ul

    try:
        rows = await broker.search_scrip(exchange, ul)
    except Exception:
        logger.exception("index_ticker_search_failed", symbol=ul, exchange=exchange)
        return None
    exact: tuple[str, str] | None = None
    prefix: tuple[str, str] | None = None
    for row in rows or []:
        tsym = str(row.get("tsym", row.get("Tradingsymbol", ""))).upper()
        token = str(row.get("token", row.get("Token", ""))).strip()
        if not token:
            continue
        if tsym == ul:
            exact = (token, tsym)
            break
        if prefix is None and tsym.startswith(ul):
            prefix = (token, tsym)
    return exact or prefix


async def resolve_index_ticker_tokens(config: AppConfig, broker: Any) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for row in list_index_tickers(config):
        sym = str(row.get("symbol", "")).upper()
        if not sym:
            continue
        existing = str(row.get("spot_token", "")).strip()
        if existing:
            resolved[sym] = existing
            continue
        exchange = str(row.get("exchange", "NSE"))
        result = await resolve_index_ticker_token(broker, sym, exchange)
        if result:
            token, tsym = result
            row["spot_token"] = token
            resolved[sym] = token
            logger.info(
                "index_ticker_token_resolved",
                symbol=sym,
                exchange=exchange,
                token=token,
                tsym=tsym,
            )
    return resolved


async def resolve_sector_index_tokens(config: AppConfig, broker: Any) -> dict[str, str]:
    """Resolve sector index tokens lazily — not part of startup WS burst."""
    resolved: dict[str, str] = {}
    for row in list_sector_indices(config):
        sym = str(row.get("symbol", "")).upper()
        if not sym:
            continue
        existing = str(row.get("spot_token", "")).strip()
        if existing:
            resolved[sym] = existing
            continue
        exchange = str(row.get("exchange", "NSE"))
        result = await resolve_index_ticker_token(broker, sym, exchange)
        if result:
            token, tsym = result
            row["spot_token"] = token
            resolved[sym] = token
            logger.info(
                "sector_index_token_resolved",
                symbol=sym,
                exchange=exchange,
                token=token,
                tsym=tsym,
            )
    return resolved
