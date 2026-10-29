from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.request import urlopen

import structlog

from algoflat.contract_selector.expiry import (
  expiry_from_option_tsym,
  format_expiry_tag,
  match_option_tsym,
  parse_expiry_tag,
)
from algoflat.models.events import Instrument

logger = structlog.get_logger(__name__)

# Public Flattrade contract master (available after hours when REST search/chain are empty).
FLATTRADE_NFO_INDEX_CSV = (
  "https://flattrade.s3.ap-south-1.amazonaws.com/scripmaster/Nfo_Index_Derivatives.csv"
)
FLATTRADE_BFO_INDEX_CSV = (
  "https://flattrade.s3.ap-south-1.amazonaws.com/scripmaster/Bfo_Index_Derivatives.csv"
)

_INDEX_CSV_BY_EXCHANGE = {
  "NFO": FLATTRADE_NFO_INDEX_CSV,
  "BFO": FLATTRADE_BFO_INDEX_CSV,
}

_NIFTY_FUT_TSYM = re.compile(r"^NIFTY(\d{2}[A-Z]{3}\d{2})F$")
_SENSEX_FUT_TSYM = re.compile(r"^SENSEX(\d{2}[A-Z]{3}\d{2})F$")


def _fut_tsym_matches(symbol: str, tsym: str) -> bool:
  ul = symbol.upper()
  ts = tsym.upper()
  if ul == "NIFTY":
    return bool(_NIFTY_FUT_TSYM.match(ts))
  if ul == "SENSEX":
    return bool(_SENSEX_FUT_TSYM.match(ts)) or (ts.startswith("SENSEX") and ts.endswith(("F", "FUT")))
  return ts.startswith(ul) and ts.endswith(("F", "FUT"))


def _parse_master_expiry(raw: str) -> date | None:
  raw = (raw or "").strip()
  if not raw:
    return None
  for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
    try:
      return datetime.strptime(raw, fmt).date()
    except ValueError:
      continue
  try:
    return datetime.strptime(raw.upper(), "%d-%b-%Y").date()
  except ValueError:
    return parse_expiry_tag(raw.upper())


def download_index_rows(
  exchange: str = "NFO",
  url: str | None = None,
) -> list[dict[str, str]]:
  if url is None:
    url = _INDEX_CSV_BY_EXCHANGE.get(exchange.upper(), FLATTRADE_NFO_INDEX_CSV)
  with urlopen(url, timeout=60) as resp:  # noqa: S310 — fixed Flattrade CDN URL
    payload = resp.read().decode("utf-8", errors="replace")
  reader = csv.DictReader(io.StringIO(payload))
  return [dict(row) for row in reader]


def download_nfo_index_rows(url: str = FLATTRADE_NFO_INDEX_CSV) -> list[dict[str, str]]:
  return download_index_rows("NFO", url)


def instruments_from_scripmaster(
  rows: list[dict[str, str]],
  *,
  underlying: str,
  expiry_tag: str,
  atm: Decimal,
  band_points: Decimal,
  exchange: str = "NFO",
) -> list[Instrument]:
  expiry_date = parse_expiry_tag(expiry_tag)
  if expiry_date is None:
    return []

  instruments: list[Instrument] = []
  ul = underlying.upper()
  for row in rows:
    symbol = (row.get("Symbol") or row.get("symbol") or "").upper()
    if symbol != ul:
      continue
    instrument = (row.get("Instrument") or row.get("instrument") or "").upper()
    if instrument and instrument != "OPTIDX":
      continue

    tsym = row.get("Tradingsymbol") or row.get("TradingSymbol") or row.get("tsym") or ""
    token = str(row.get("Token") or row.get("token") or "")
    if not tsym or not token:
      continue

    exp = _parse_master_expiry(row.get("Expiry") or row.get("expiry") or "")
    if exp is None:
      exp = expiry_from_option_tsym(tsym)
    if exp != expiry_date:
      continue

    try:
      strike = Decimal(str(row.get("Strike") or row.get("strike") or "0"))
    except Exception:
      continue
    if abs(strike - atm) > band_points:
      continue

    opt = (row.get("Optiontype") or row.get("OptionType") or row.get("optt") or "").upper()
    if opt in ("CE", "C", "CALL"):
      option_type = "CE"
    elif opt in ("PE", "P", "PUT"):
      option_type = "PE"
    else:
      continue

    lot = int(float(row.get("Lotsize") or row.get("ls") or 20))
    instruments.append(
      Instrument(
        exchange=exchange,
        token=token,
        tsym=tsym,
        underlying=ul,
        expiry_date=datetime.combine(exp, datetime.min.time()),
        strike=strike,
        option_type=option_type,
        lot_size=lot,
        tick_size=Decimal("0.05"),
        is_atm=strike == atm,
        in_band=True,
      )
    )

  instruments.sort(key=lambda i: (i.strike, 0 if i.option_type == "CE" else 1))
  return instruments


def futures_tokens_for_underlying(
  *,
  underlying: str,
  exchange: str = "NFO",
) -> list[str]:
  """All FUTIDX tokens for an index (used to stitch chart history across rollovers)."""
  rows = download_index_rows(exchange)
  ul = underlying.upper()
  tokens: list[str] = []
  for row in rows:
    symbol = (row.get("Symbol") or row.get("symbol") or "").upper()
    if symbol != ul:
      continue
    instrument = (row.get("Instrument") or row.get("instrument") or "").upper()
    if instrument != "FUTIDX":
      continue
    tsym = row.get("Tradingsymbol") or row.get("TradingSymbol") or row.get("tsym") or ""
    token = str(row.get("Token") or row.get("token") or "").strip()
    if token and _fut_tsym_matches(ul, str(tsym)):
      tokens.append(token)
  return tokens


def nearest_future_from_scripmaster(
  *,
  underlying: str,
  exchange: str = "NFO",
  as_of: date | None = None,
) -> tuple[str, str] | None:
  """Current/nearest index future from Flattrade scripmaster (FUTIDX)."""
  today = as_of or date.today()
  rows = download_index_rows(exchange)
  ul = underlying.upper()
  candidates: list[tuple[date, str, str]] = []
  for row in rows:
    symbol = (row.get("Symbol") or row.get("symbol") or "").upper()
    if symbol != ul:
      continue
    instrument = (row.get("Instrument") or row.get("instrument") or "").upper()
    if instrument != "FUTIDX":
      continue
    tsym = row.get("Tradingsymbol") or row.get("TradingSymbol") or row.get("tsym") or ""
    token = str(row.get("Token") or row.get("token") or "").strip()
    if not tsym or not token:
      continue
    if not _fut_tsym_matches(ul, str(tsym)):
      continue
    exp = _parse_master_expiry(row.get("Expiry") or row.get("expiry") or "")
    if exp is None or exp < today:
      continue
    candidates.append((exp, token, str(tsym)))
  if not candidates:
    return None
  candidates.sort(key=lambda x: x[0])
  _, token, tsym = candidates[0]
  return token, tsym


def option_expiry_tags_from_scripmaster(
  *,
  underlying: str,
  as_of: date | None = None,
  include_today: bool = True,
  limit: int = 6,
  exchange: str = "NFO",
  min_contracts: int = 50,
) -> list[str]:
  """Upcoming OPTIDX expiry tags from the Flattrade contract master, nearest first."""
  today = as_of or date.today()
  rows = download_index_rows(exchange)
  ul = underlying.upper()
  by_tag: dict[str, date] = {}
  counts: dict[str, int] = {}
  for row in rows:
    symbol = (row.get("Symbol") or row.get("symbol") or "").upper()
    if symbol != ul:
      continue
    instrument = (row.get("Instrument") or row.get("instrument") or "").upper()
    if instrument and instrument != "OPTIDX":
      continue
    tsym = str(row.get("Tradingsymbol") or row.get("TradingSymbol") or row.get("tsym") or "")
    exp = _parse_master_expiry(row.get("Expiry") or row.get("expiry") or "")
    if exp is None:
      exp = expiry_from_option_tsym(tsym)
    if exp is None:
      continue
    # Prefer Expiry column; still require tsym to belong to this underlying when parseable.
    match = match_option_tsym(tsym)
    if match is not None and match.group("ul") != ul:
      continue
    if exp < today or (exp == today and not include_today):
      continue
    tag = format_expiry_tag(exp)
    by_tag.setdefault(tag, exp)
    counts[tag] = counts.get(tag, 0) + 1
  ranked = sorted(
    ((tag, exp) for tag, exp in by_tag.items() if counts.get(tag, 0) >= min_contracts),
    key=lambda item: item[1],
  )
  return [tag for tag, _ in ranked[:limit]]


def load_weekly_band_from_scripmaster(
  *,
  underlying: str,
  expiry_tag: str,
  atm: Decimal,
  band_points: Decimal,
  exchange: str = "NFO",
) -> list[Instrument]:
  rows = download_index_rows(exchange)
  instruments = instruments_from_scripmaster(
    rows,
    underlying=underlying,
    expiry_tag=expiry_tag,
    atm=atm,
    band_points=band_points,
    exchange=exchange,
  )
  logger.info(
    "scripmaster_loaded",
    underlying=underlying,
    expiry=expiry_tag,
    atm=str(atm),
    count=len(instruments),
  )
  return instruments
