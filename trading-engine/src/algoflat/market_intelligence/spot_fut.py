"""NIFTY spot vs FUT alignment features — New-Engine spec §5–6."""
from __future__ import annotations

from typing import Any


def _bias_from_vwap(price: float | None, vwap: float | None, buffer_pts: float = 0.0) -> str:
  if price is None or vwap is None:
    return "neutral"
  if price > vwap + buffer_pts:
    return "bullish"
  if price < vwap - buffer_pts:
    return "bearish"
  return "neutral"


def build_spot_fut_context(
  *,
  cash_spot: float | None,
  fut_spot: float | None,
  cash_vwap: float | None,
  fut_vwap: float | None,
  basis_buffer_pts: float = 0.0,
) -> dict[str, Any]:
  """Derived alignment / divergence context — not a reversal trigger."""
  basis = None
  basis_pct = None
  if cash_spot is not None and fut_spot is not None and cash_spot > 0:
    basis = round(fut_spot - cash_spot, 2)
    basis_pct = round((basis / cash_spot) * 100, 4)

  spot_bias = _bias_from_vwap(cash_spot, cash_vwap)
  fut_bias = _bias_from_vwap(fut_spot, fut_vwap)

  alignment = "neutral"
  divergence = False
  if spot_bias == fut_bias and spot_bias != "neutral":
    alignment = spot_bias
  elif spot_bias != "neutral" and fut_bias != "neutral" and spot_bias != fut_bias:
    alignment = "divergent"
    divergence = True
  elif spot_bias != "neutral" and fut_bias == "neutral":
    alignment = f"spot_{spot_bias}_fut_neutral"
  elif fut_bias != "neutral" and spot_bias == "neutral":
    alignment = f"fut_{fut_bias}_spot_neutral"

  return {
    "fut_vs_spot": basis,
    "futures_basis": basis,
    "futures_basis_pct": basis_pct,
    "spot_bias": spot_bias,
    "fut_bias": fut_bias,
    "spot_fut_alignment": alignment,
    "spot_fut_divergence": divergence,
    "spot_vs_vwap": spot_bias,
    "fut_vs_vwap_bias": fut_bias,
    "futures_vwap_distance": (
      round(float(fut_spot) - float(fut_vwap), 2)
      if fut_spot is not None and fut_vwap is not None
      else None
    ),
  }
