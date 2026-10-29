"""Decision flow snapshot for cockpit — maps live pipeline stages."""
from __future__ import annotations

from typing import Any

from algoflat.market_intelligence.no_trade import classify_no_trade_code
from algoflat.models.events import Bias, FeatureSnapshot, StrategyDecision
from algoflat.validator.basis_util import expected_max_basis_points

StageStatus = str  # ok | warn | block | pending | idle


def _stage(
  *,
  stage_id: str,
  label: str,
  status: StageStatus,
  detail: str,
  value: Any = None,
) -> dict[str, Any]:
  row: dict[str, Any] = {
    "id": stage_id,
    "label": label,
    "status": status,
    "detail": detail,
  }
  if value is not None:
    row["value"] = value
  return row


def _index_display_name(underlying: str | None, *, futures: bool = False) -> str:
  ul = (underlying or "SENSEX").upper()
  labels = {
    "SENSEX": ("SENSEX", "SENSEX FUT"),
    "BANKEX": ("BANKEX", "BANKEX FUT"),
    "NIFTY": ("NIFTY 50", "NIFTY FUT"),
    "BANKNIFTY": ("NIFTY BANK", "BANKNIFTY FUT"),
  }
  cash_name, fut_name = labels.get(ul, (ul, f"{ul} FUT"))
  return fut_name if futures else cash_name


def build_decision_flow(
  features: FeatureSnapshot,
  *,
  decision: StrategyDecision | None = None,
  basis_cfg: dict | None = None,
  premium_sample_count: int = 0,
  entries_blocked: bool = False,
  block_reason: str | None = None,
  has_open_position: bool = False,
  mi_snapshot: dict[str, Any] | None = None,
  ml_prediction: dict[str, Any] | None = None,
  last_entry_block: str | None = None,
  last_entry_confirm: str | None = None,
  underlying: str | None = None,
) -> dict[str, Any]:
  """Build ordered stages: Spot → Levels → price → Momentum → CE/PE → Premium → Entry."""
  basis_cfg = basis_cfg or {}
  extra = features.extra or {}
  mi = mi_snapshot or extra.get("market_intelligence") or {}
  cash = extra.get("cash_index_spot")
  if cash is None and features.nifty_spot is not None:
    cash = features.nifty_spot
  fut = extra.get("fut_spot")
  basis = extra.get("basis_points")
  structure = extra.get("structure_5m")
  levels = extra.get("levels") or {}
  flow_meta = extra.get("decision_flow") or {}
  ul = (
    underlying
    or extra.get("underlying")
    or flow_meta.get("underlying")
    or "SENSEX"
  )
  cash_label = _index_display_name(str(ul), futures=False)
  fut_label = _index_display_name(str(ul), futures=True)

  basis_enabled = bool(basis_cfg.get("enabled", True))
  max_basis = expected_max_basis_points(
    float(cash) if cash is not None else (float(fut) if fut is not None else None),
    dte_futures=extra.get("futures_dte"),
    cfg=basis_cfg,
  )
  basis_abs = abs(float(basis)) if basis is not None else None
  basis_ok = not basis_enabled or basis_abs is None or basis_abs <= max_basis
  basis_wide = basis_enabled and basis_abs is not None and basis_abs > max_basis

  vwap = features.session_vwap
  fut_vs_vwap: str | None = None
  if fut is not None and vwap is not None:
    f, v = float(fut), float(vwap)
    if f > v:
      fut_vs_vwap = "ABOVE"
    elif f < v:
      fut_vs_vwap = "BELOW"
    else:
      fut_vs_vwap = "AT"

  bias = features.bias_5m.value.upper() if features.bias_5m else Bias.NEUTRAL.value.upper()
  momentum_aligned = True
  if fut_vs_vwap == "ABOVE" and bias == "BEARISH":
    momentum_aligned = False
  elif fut_vs_vwap == "BELOW" and bias == "BULLISH":
    momentum_aligned = False

  level_bits: list[str] = []
  if structure:
    level_bits.append(f"5m {structure}")
  if levels.get("at_resistance"):
    level_bits.append("at resistance")
  elif levels.get("at_support"):
    level_bits.append("at support")
  if levels.get("rejection_setup"):
    level_bits.append(str(levels.get("rejection_setup")))
  chain = extra.get("chain") or {}
  call_wall = chain.get("call_writer_strike")
  put_wall = chain.get("put_writer_strike")
  if call_wall is not None:
    level_bits.append(f"call writers {float(call_wall):,.0f}")
  if put_wall is not None:
    level_bits.append(f"put writers {float(put_wall):,.0f}")
  levels_detail = " · ".join(level_bits) if level_bits else "Watching levels / VWAP context"

  ce_pe = mi.get("ce_pe") or {}
  ce_str = ce_pe.get("ce_strength")
  pe_str = ce_pe.get("pe_strength")
  ce_pe_line = (
    f"CE {ce_str} · PE {pe_str}" if ce_str is not None and pe_str is not None else ""
  )

  side_status: StageStatus = "idle"
  side_detail = ce_pe_line or "No candidate"
  if decision is not None:
    strat = decision.selected_strategy or "NO_TRADE"
    if has_open_position and decision.selected_strategy not in ("option_lift", "NO_TRADE"):
      side_status = "ok"
      side_detail = f"In position · {decision.position_side or 'OPEN'}"
    elif decision.trade_allowed and strat not in ("NO_TRADE", ""):
      pick = f"{decision.position_side} · {strat.replace('_', ' ')}"
      if last_entry_block:
        side_status = "block"
        side_detail = f"{pick} · blocked: {last_entry_block.replace('_', ' ')[:48]}"
      elif last_entry_confirm:
        side_status = "warn"
        side_detail = f"{pick} · {last_entry_confirm.replace('_', ' ')}"
      else:
        side_status = "ok"
        side_detail = f"{pick} · {ce_pe_line}" if ce_pe_line else pick
    elif strat == "NO_TRADE":
      side_status = "pending"
      reason = (decision.selected_reason or "waiting").replace("_", " ")
      side_detail = reason[:72]
      if last_entry_block:
        side_status = "block"
        side_detail = f"Last block: {last_entry_block.replace('_', ' ')[:56]}"
    else:
      side_status = "warn"
      side_detail = (decision.selected_reason or "setup blocked").replace("_", " ")[:72]

  if premium_sample_count >= 4:
    premium_status: StageStatus = "ok"
    premium_detail = f"Tracking ({premium_sample_count} samples)"
  elif premium_sample_count > 0:
    premium_status = "warn"
    premium_detail = f"Warming up ({premium_sample_count}/4 samples)"
  else:
    premium_status = "pending"
    premium_detail = "Collecting spot/premium ticks"

  premium = mi.get("premium") or {}
  if decision is not None and (
    decision.selected_strategy == "option_lift" or "ATM" in (decision.selected_reason or "")
  ):
    premium_status = "ok" if decision.trade_allowed else "pending"
    premium_detail = (decision.selected_reason or "Watching ATM band")[:80]
  elif premium.get("available"):
    ce_ok = premium.get("ce_confirm")
    pe_ok = premium.get("pe_confirm")
    if ce_ok or pe_ok:
      premium_status = "ok"
      premium_detail = f"MI confirm CE={'yes' if ce_ok else 'no'} PE={'yes' if pe_ok else 'no'}"
    elif premium.get("ce", {}).get("ltp") or premium.get("pe", {}).get("ltp"):
      premium_status = "warn"
      premium_detail = "MI premium not confirmed yet"

  if has_open_position and not (
    decision is not None
    and decision.selected_strategy in ("option_lift", "NO_TRADE")
    and decision.selected_reason
  ):
    entry_status: StageStatus = "ok"
    entry_detail = "Position open — exit rules active"
  elif entries_blocked:
    entry_status = "block"
    entry_detail = (block_reason or "entries blocked").replace("_", " ")
  elif last_entry_block:
    entry_status = "block"
    entry_detail = last_entry_block.replace("_", " ")[:72]
  elif last_entry_confirm:
    entry_status = "warn"
    entry_detail = last_entry_confirm.replace("_", " ")
  elif decision is not None and decision.trade_allowed:
    entry_status = "ok"
    entry_detail = "Ready — risk and balance check"
  elif decision is not None and decision.selected_strategy in ("option_lift", "NO_TRADE") and decision.selected_reason:
    entry_status = "pending"
    entry_detail = decision.selected_reason.replace("_", " ")[:72]
  else:
    entry_status = "pending"
    entry_detail = "Waiting for a premium or volume lift"

  # Cash-index VWAP (NSE or BSE) vs futures-priced book.
  index_base = str(flow_meta.get("vwap_source") or "").endswith("_index") or str(
    flow_meta.get("vwap_source") or ""
  ) in ("nse_index", "bse_index", "index")
  if flow_meta.get("vwap_source") is None:
    # Default for this Sensex stack: cash index drives VWAP / ATM.
    index_base = True
  price_name = cash_label if index_base else fut_label
  vwap_side = fut_vs_vwap or "—"
  if (not index_base) and basis is not None and fut_vs_vwap:
    momentum_detail = (
      f"FUT {vwap_side} VWAP · basis {basis:+.1f} pts (limit ±{max_basis:.0f})"
    )
  else:
    momentum_detail = f"{price_name} — VWAP · 5m {bias.lower()}"

  stages = [
    _stage(
      stage_id="spot",
      label=cash_label if index_base else f"{cash_label} Spot",
      status="ok" if cash is not None else "warn",
      detail=f"₹{float(cash):,.2f}" if cash is not None else "Index quote pending",
      value=cash,
    ),
    _stage(
      stage_id="levels",
      label="Structure / Levels",
      status="ok" if level_bits else "pending",
      detail=levels_detail,
      value=structure,
    ),
  ]
  if not index_base:
    stages.append(
      _stage(
        stage_id="fut",
        label=fut_label,
        status="ok" if fut is not None else "warn",
        detail=f"₹{float(fut):,.2f}" if fut is not None else "Futures quote pending",
        value=fut,
      )
    )
  stages.extend([
    _stage(
      stage_id="momentum",
      label="Momentum + Confirm",
      status="ok" if momentum_aligned and basis_ok else ("block" if basis_wide else "warn"),
      detail=momentum_detail,
      value=fut_vs_vwap,
    ),
    _stage(
      stage_id="side",
      label="CE / PE Opportunity",
      status=side_status,
      detail=side_detail,
      value=decision.selected_strategy if decision else None,
    ),
    _stage(
      stage_id="premium",
      label="Premium Behaviour",
      status=premium_status,
      detail=premium_detail,
      value=premium_sample_count,
    ),
    _stage(
      stage_id="entry",
      label="Final Entry",
      status=entry_status,
      detail=entry_detail,
      value=block_reason,
    ),
  ])

  regime_allowed = True
  if decision and decision.regime is not None:
    regime_allowed = bool(decision.regime.trade_allowed)

  no_trade_code = classify_no_trade_code(
    veto_reasons=[block_reason] if block_reason else [],
    pipeline_vetoes=(
      [decision.selected_reason] if decision and not decision.trade_allowed else []
    ),
    extra=extra,
    regime_trade_allowed=regime_allowed,
    trade_allowed=bool(decision.trade_allowed) if decision else False,
    ml_prediction=ml_prediction,
  )

  vix = mi.get("vix") or {}
  breadth = mi.get("breadth") or {}

  return {
    "stages": stages,
    "cash_index_spot": cash,
    "fut_spot": fut,
    "basis_points": basis,
    "basis_limit_points": max_basis,
    "basis_ok": basis_ok,
    "fut_vs_vwap": fut_vs_vwap,
    "structure_5m": structure,
    "bias_5m": bias,
    "levels_anchor": flow_meta.get("levels_anchor"),
    "vwap_source": flow_meta.get("vwap_source"),
    "momentum_source": flow_meta.get("momentum_source"),
    "momentum_aligned": momentum_aligned,
    "no_trade_code": no_trade_code,
    "vix_level": vix.get("vix_level"),
    "vix_regime": vix.get("volatility_regime"),
    "sector_breadth_pct": (mi.get("sectors") or {}).get("sector_breadth_pct"),
    "advance_pct": breadth.get("advance_pct"),
    "ce_strength": ce_str,
    "pe_strength": pe_str,
    "ce_pe_verdict": ce_pe.get("verdict"),
    "early_bird_active": (mi.get("early_bird") or {}).get("early_bird_active"),
    "reversal_risk_elevated": (mi.get("reversal") or {}).get("reversal_risk_elevated"),
    "data_quality": (mi.get("data_quality") or {}).get("state"),
    "ml_shadow": ml_prediction,
  }
