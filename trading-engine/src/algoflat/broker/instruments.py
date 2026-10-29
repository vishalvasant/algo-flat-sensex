"""Map internal exchange|token keys to Upstox instrument_key format."""
from __future__ import annotations

_INDEX_KEYS: dict[tuple[str, str], str] = {
    ("NSE", "26000"): "NSE_INDEX|Nifty 50",
    ("NSE", "Nifty 50"): "NSE_INDEX|Nifty 50",
    ("NSE", "India VIX"): "NSE_INDEX|India VIX",
    ("NSE", "INDIAVIX"): "NSE_INDEX|India VIX",
    ("NSE", "Nifty Bank"): "NSE_INDEX|Nifty Bank",
    ("NSE", "BANKNIFTY"): "NSE_INDEX|Nifty Bank",
    ("NSE", "Nifty IT"): "NSE_INDEX|Nifty IT",
    ("NSE", "NIFTYIT"): "NSE_INDEX|Nifty IT",
    ("NSE", "Nifty Auto"): "NSE_INDEX|Nifty Auto",
    ("NSE", "Nifty Pharma"): "NSE_INDEX|Nifty Pharma",
    ("NSE", "Nifty Metal"): "NSE_INDEX|Nifty Metal",
    ("NSE", "Nifty FMCG"): "NSE_INDEX|Nifty FMCG",
    ("NSE", "Nifty Realty"): "NSE_INDEX|Nifty Realty",
    ("NSE", "Nifty Energy"): "NSE_INDEX|Nifty Energy",
    ("NSE", "Nifty Fin Service"): "NSE_INDEX|Nifty Fin Service",
}

_EXCHANGE_SEGMENT = {
    "NSE": "NSE_EQ",
    "NFO": "NSE_FO",
    "BFO": "BSE_FO",
    "BSE": "BSE_EQ",
    "MCX": "MCX_FO",
    "GLOBAL": "GLOBAL_INDEX",
}


def to_upstox_instrument_key(
    exchange: str,
    token: str,
    *,
    tsym: str | None = None,
) -> str:
    """Build Upstox instrument_key from algo-flat exchange + token."""
    ex = (exchange or "").upper().strip()
    tok = str(token or "").strip()
    hit = _INDEX_KEYS.get((ex, tok))
    if hit:
        return hit
    if ex == "NSE" and tok.isdigit() and int(tok) == 26000:
        return "NSE_INDEX|Nifty 50"
    seg = _EXCHANGE_SEGMENT.get(ex, "NSE_FO")
    return f"{seg}|{tok}"


def from_upstox_instrument_key(key: str) -> tuple[str, str]:
    """Return (exchange, token) for subscription keys."""
    if "|" not in key:
        return "", key
    seg, tok = key.split("|", 1)
    if seg == "NSE_INDEX":
        return "NSE", tok if tok != "Nifty 50" else "26000"
    if seg == "NSE_FO":
        return "NFO", tok
    if seg == "BSE_FO":
        return "BFO", tok
    if seg == "NSE_EQ":
        return "NSE", tok
    if seg == "BSE_EQ":
        return "BSE", tok
    if seg == "GLOBAL_INDEX":
        return "GLOBAL", tok
    return seg, tok


def format_subscription_key(exchange: str, token: str) -> str:
    """Keep algo-flat convention: EXCH|token (not Upstox segment)."""
    return f"{exchange}|{token}"
