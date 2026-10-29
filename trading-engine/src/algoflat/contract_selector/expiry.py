from __future__ import annotations

import re
from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# NSE-style: NIFTY12NOV26C24000
_OPTION_TSYM = re.compile(
  r"^(?P<ul>[A-Z]+)(?P<tag>\d{2}[A-Z]{3}\d{2})(?P<opt>[CP]|CE|PE)(?P<strike>\d+)$"
)
# BSE weekly compact: SENSEX26N1265600PE (YY + month code 1-9OND + DD + strike + CE/PE).
_BSE_WEEKLY_TSYM = re.compile(
  r"^(?P<ul>SENSEX|BANKEX)(?P<yy>\d{2})(?P<mon>[1-9OND])(?P<dd>\d{2})"
  r"(?P<strike>\d+)(?P<opt>CE|PE)$"
)

_BSE_MONTH_CODES = {
  "1": 1,
  "2": 2,
  "3": 3,
  "4": 4,
  "5": 5,
  "6": 6,
  "7": 7,
  "8": 8,
  "9": 9,
  "O": 10,
  "N": 11,
  "D": 12,
}

# SENSEX weekly options expire on Thursday.
_DEFAULT_WEEKLY_WEEKDAY = {
  "SENSEX": 3,      # Thursday
  "BANKEX": 3,      # Thursday
  "NIFTY": 1,       # Tuesday
  "BANKNIFTY": 1,
  "FINNIFTY": 1,
  "MIDCPNIFTY": 1,
}


def parse_expiry_tag(tag: str) -> date | None:
  text = (tag or "").strip().upper()
  if not text:
    return None
  try:
    return datetime.strptime(text, "%d%b%y").date()
  except ValueError:
    pass
  # Compact BSE weekly tag: 26N12 → 12 Nov 2026
  m = re.fullmatch(r"(?P<yy>\d{2})(?P<mon>[1-9OND])(?P<dd>\d{2})", text)
  if not m:
    return None
  month = _BSE_MONTH_CODES.get(m.group("mon"))
  if month is None:
    return None
  try:
    return date(2000 + int(m.group("yy")), month, int(m.group("dd")))
  except ValueError:
    return None


def format_expiry_tag(d: date) -> str:
  # Force English month abbreviation (locale-independent).
  months = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
  )
  return f"{d.day:02d}{months[d.month - 1]}{d.year % 100:02d}"


def format_bse_weekly_tag(d: date) -> str:
  """BSE weekly compact expiry fragment used inside trading symbols (e.g. 26N12)."""
  rev = {v: k for k, v in _BSE_MONTH_CODES.items()}
  return f"{d.year % 100:02d}{rev[d.month]}{d.day:02d}"


def match_option_tsym(tsym: str) -> re.Match[str] | None:
  text = (tsym or "").upper()
  return _OPTION_TSYM.match(text) or _BSE_WEEKLY_TSYM.match(text)


def expiry_from_option_tsym(tsym: str) -> date | None:
  text = (tsym or "").upper()
  m = _OPTION_TSYM.match(text)
  if m:
    return parse_expiry_tag(m.group("tag"))
  m = _BSE_WEEKLY_TSYM.match(text)
  if not m:
    return None
  month = _BSE_MONTH_CODES.get(m.group("mon"))
  if month is None:
    return None
  try:
    return date(2000 + int(m.group("yy")), month, int(m.group("dd")))
  except ValueError:
    return None


def weekly_weekday_for(underlying: str) -> int:
  return _DEFAULT_WEEKLY_WEEKDAY.get(underlying.upper(), 3)


def next_weekly_expiry_dates(
  *,
  underlying: str,
  as_of: date | None = None,
  count: int = 6,
  include_today_if_expiry: bool = True,
) -> list[date]:
  """Upcoming weekly expiry calendar dates (no broker call)."""
  today = as_of or datetime.now(IST).date()
  weekday = weekly_weekday_for(underlying)
  dates: list[date] = []

  cursor = today
  if not include_today_if_expiry and cursor.weekday() == weekday:
    cursor += timedelta(days=1)

  for _ in range(60):
    if cursor.weekday() == weekday:
      if cursor > today or (include_today_if_expiry and cursor == today):
        dates.append(cursor)
        if len(dates) >= count:
          break
    cursor += timedelta(days=1)
  return dates


def include_expiry_day(as_of: date | None = None, now: datetime | None = None) -> bool:
  """Keep today's expiry only while the cash session is still live."""
  now_ist = (now or datetime.now(IST)).astimezone(IST)
  day = as_of or now_ist.date()
  if now_ist.date() != day:
    return True
  return now_ist.time() < time(15, 30)


def collect_expiry_tags_from_search(
  rows: list[dict],
  *,
  underlying: str,
  as_of: date | None = None,
  include_today: bool | None = None,
) -> dict[str, date]:
  today = as_of or datetime.now(IST).date()
  if include_today is None:
    include_today = include_expiry_day(as_of=today)

  expiries: dict[str, date] = {}
  ul = underlying.upper()

  for row in rows:
    tsym = str(row.get("tsym", ""))
    if "NXT" in tsym:
      continue
    instname = row.get("instname")
    if instname and instname != "OPTIDX":
      continue
    match = match_option_tsym(tsym)
    exp = expiry_from_option_tsym(tsym)
    if match is None or match.group("ul") != ul or exp is None:
      continue
    if exp < today:
      continue
    if exp == today and not include_today:
      continue
    expiries.setdefault(format_expiry_tag(exp), exp)
  return expiries


def nearest_weekly_expiry_tag(
  rows: list[dict],
  *,
  underlying: str,
  as_of: date | None = None,
  include_today: bool | None = None,
) -> str | None:
  expiries = collect_expiry_tags_from_search(
    rows, underlying=underlying, as_of=as_of, include_today=include_today
  )
  if expiries:
    return min(expiries.items(), key=lambda item: item[1])[0]

  today = as_of or datetime.now(IST).date()
  if include_today is None:
    include_today = include_expiry_day(as_of=today)
  dates = next_weekly_expiry_dates(
    underlying=underlying,
    as_of=today,
    count=1,
    include_today_if_expiry=include_today,
  )
  return format_expiry_tag(dates[0]) if dates else None


def weekly_expiry_candidates(
  rows: list[dict],
  *,
  underlying: str,
  as_of: date | None = None,
  include_today: bool | None = None,
  limit: int = 4,
) -> list[str]:
  """Ordered list of weekly expiry tags to try (search first, then calendar)."""
  today = as_of or datetime.now(IST).date()
  if include_today is None:
    include_today = include_expiry_day(as_of=today)

  expiries = collect_expiry_tags_from_search(
    rows, underlying=underlying, as_of=today, include_today=include_today
  )
  tags = [t for t, _ in sorted(expiries.items(), key=lambda item: item[1])]

  for d in next_weekly_expiry_dates(
    underlying=underlying,
    as_of=today,
    count=limit,
    include_today_if_expiry=include_today,
  ):
    tag = format_expiry_tag(d)
    if tag not in tags:
      tags.append(tag)

  return tags[:limit]


def chain_anchor_symbol(underlying: str, expiry_tag: str, atm_strike: int, side: str = "CE") -> str:
  """Build a GetOptionChain anchor tsym for the underlying."""
  ul = underlying.upper()
  opt = "CE" if side.upper().startswith("C") else "PE"
  exp = parse_expiry_tag(expiry_tag)
  if ul in ("SENSEX", "BANKEX") and exp is not None:
    return f"{ul}{format_bse_weekly_tag(exp)}{int(atm_strike)}{opt}"
  return f"{ul}{expiry_tag.upper()}{opt[0]}{int(atm_strike)}"
