"""Data quality hard gate — never trade on invalid market data."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from algoflat.models.events import FeatureSnapshot, OptionState


def assess_data_quality(
  *,
  features: FeatureSnapshot,
  option: OptionState | None,
  candles_stale: bool,
  last_refresh_ok: bool,
  cfg: dict,
) -> tuple[str, list[str]]:
  """
  Returns (state, failures).
  state: DATA_QUALITY_OK | DATA_QUALITY_DEGRADED | DATA_QUALITY_INVALID
  """
  if not cfg.get("enabled", True):
    return "DATA_QUALITY_OK", []

  failures: list[str] = []
  extra = features.extra or {}

  if candles_stale:
    failures.append("stale_candle_feed")
  if not last_refresh_ok and cfg.get("require_fresh_refresh", True):
    failures.append("candle_refresh_failed")

  if features.nifty_spot is None:
    failures.append("missing_spot")
  if features.session_vwap is None:
    failures.append("missing_vwap")

  if option is None:
    failures.append("option_state_missing")
  else:
    if option.ltp is None or option.ltp <= 0:
      failures.append("invalid_option_ltp")
    max_spread = float(cfg.get("max_spread_pct", 2.5))
    if option.spread_pct is not None and float(option.spread_pct) > max_spread:
      failures.append("abnormal_spread")
    quote_age = option.quote_age_seconds if hasattr(option, "quote_age_seconds") else None
    if quote_age is None:
      meta_age = extra.get("option_quote_age_seconds")
      if meta_age is not None:
        quote_age = float(meta_age)
    max_age = float(cfg.get("max_quote_age_seconds", 30))
    if quote_age is not None and quote_age > max_age:
      failures.append("stale_option_quote")

  if failures:
    hard = set(cfg.get("invalid_reasons") or failures)
    if any(f in hard for f in failures):
      return "DATA_QUALITY_INVALID", failures
    return "DATA_QUALITY_DEGRADED", failures
  return "DATA_QUALITY_OK", []


def quote_age_seconds(option: OptionState, *, now: datetime | None = None) -> float | None:
  ts = getattr(option, "ts", None) or getattr(option, "quote_ts", None)
  if ts is None:
    return None
  now = now or datetime.now(tz=timezone.utc)
  if ts.tzinfo is None:
    ts = ts.replace(tzinfo=timezone.utc)
  return max(0.0, (now - ts).total_seconds())
