"""Unit tests for confidence-based lot sizing."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from algoflat.models.events import Bias, CandidateSignal, FeatureSnapshot, OptionState
from algoflat.risk.engine import (
  DailyRiskSnapshot,
  RiskEngine,
  confidence_capital_pct,
  fit_lots_to_capital,
  lots_for_confidence,
  strategy_lot_cap,
)


def _signal(confidence: int) -> CandidateSignal:
  return CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type="vwap_trend",
    side="PE",
    instrument_token="1",
    tsym="NIFTYPE",
    strategy_version="t",
    confidence=confidence,
    scanner_metadata={"lot_size": 65},
    feature_snapshot=FeatureSnapshot(
      ts=datetime.now(tz=timezone.utc),
      bias_5m=Bias.BEARISH,
    ),
  )


def _dynamic_cfg(**overrides):
  cfg = {
    "default_lots": 1,
    "max_premium_pct_of_available": 85,
    "max_deployed_pct_of_equity": 85,
    "max_daily_loss": 10000,
    "max_trades_per_day": 0,
    "max_concurrent_positions": 0,
    "max_consecutive_losses": 5,
    "confidence_lot_sizing": {
      "enabled": True,
      "mode": "dynamic",
      "base_confidence": 78,
      "max_confidence": 100,
      "min_capital_pct": 30,
      "max_capital_pct": 85,
      "aggressive_deploy_min_confidence": 90,
      "max_lots": 0,
    },
  }
  cfg.update(overrides)
  return cfg


def _tier_cfg(**overrides):
  cfg = _dynamic_cfg()
  cfg["confidence_lot_sizing"] = {
    "enabled": True,
    "mode": "tiers",
    "max_lots": 3,
    "scale_to_max_lots_min_confidence": 92,
    "tiers": [
      {"min_confidence": 70, "lots": 1},
      {"min_confidence": 80, "lots": 2},
      {"min_confidence": 90, "lots": 3},
    ],
  }
  cfg.update(overrides)
  return cfg


def test_confidence_capital_pct_scales_linearly():
  cfg = _dynamic_cfg()
  assert confidence_capital_pct(cfg, 78) == Decimal("30")
  assert confidence_capital_pct(cfg, 100) == Decimal("85")
  mid = confidence_capital_pct(cfg, 85)
  assert Decimal("40") < mid < Decimal("60")


def test_aggressive_deploy_at_high_confidence():
  cfg = _dynamic_cfg()
  cfg["confidence_lot_sizing"]["max_capital_pct"] = 90
  cfg["confidence_lot_sizing"]["aggressive_deploy_min_confidence"] = 90
  assert confidence_capital_pct(cfg, 89) < Decimal("90")
  assert confidence_capital_pct(cfg, 90) == Decimal("90")
  assert confidence_capital_pct(cfg, 95) == Decimal("90")


def test_dynamic_fit_more_confidence_more_lots():
  cfg = _dynamic_cfg()
  common = dict(
    entry_ltp=Decimal("80"),
    lot_size=65,
    available=Decimal("50000"),
    deployed=Decimal("0"),
    equity=Decimal("50000"),
  )
  low_lots, _ = fit_lots_to_capital(cfg, confidence=80, **common)
  high_lots, _ = fit_lots_to_capital(cfg, confidence=95, **common)
  assert high_lots > low_lots
  assert high_lots >= 3


def test_dynamic_uses_max_affordable_lots_within_budget():
  cfg = _dynamic_cfg()
  cfg["confidence_lot_sizing"]["aggressive_deploy_min_confidence"] = 90
  cfg["confidence_lot_sizing"]["max_capital_pct"] = 90
  cfg["max_premium_pct_of_available"] = 90
  lots, prem = fit_lots_to_capital(
    cfg,
    confidence=95,
    entry_ltp=Decimal("80"),
    lot_size=65,
    available=Decimal("50000"),
    deployed=Decimal("0"),
    equity=Decimal("50000"),
  )
  # conf 95 → 90% deploy = 45000; per lot 5200 → 8 lots
  assert lots == 8
  assert prem == Decimal("41600")


def test_dynamic_compounds_profits_into_lot_size():
  """After wins, equity grows and next entry can deploy more capital."""
  cfg = _dynamic_cfg()
  cfg["confidence_lot_sizing"]["aggressive_deploy_min_confidence"] = 90
  cfg["confidence_lot_sizing"]["max_capital_pct"] = 90
  cfg["max_premium_pct_of_available"] = 90
  base = dict(
    entry_ltp=Decimal("80"),
    lot_size=65,
    deployed=Decimal("0"),
    confidence=95,
  )
  start_lots, _ = fit_lots_to_capital(
    cfg,
    available=Decimal("50000"),
    equity=Decimal("50000"),
    **base,
  )
  after_win_lots, _ = fit_lots_to_capital(
    cfg,
    available=Decimal("56000"),
    equity=Decimal("56000"),
    **base,
  )
  assert after_win_lots > start_lots


def test_dynamic_compounds_losses_into_smaller_size():
  cfg = _dynamic_cfg()
  cfg["confidence_lot_sizing"]["aggressive_deploy_min_confidence"] = 90
  cfg["confidence_lot_sizing"]["max_capital_pct"] = 90
  lots, _ = fit_lots_to_capital(
    cfg,
    confidence=95,
    entry_ltp=Decimal("80"),
    lot_size=65,
    available=Decimal("44000"),
    deployed=Decimal("0"),
    equity=Decimal("44000"),
  )
  assert lots < 8


def test_dynamic_steps_down_when_premium_expensive():
  cfg = _dynamic_cfg()
  cfg["confidence_lot_sizing"]["aggressive_deploy_min_confidence"] = 90
  cfg["confidence_lot_sizing"]["max_capital_pct"] = 90
  cfg["max_premium_pct_of_available"] = 90
  lots, prem = fit_lots_to_capital(
    cfg,
    confidence=95,
    entry_ltp=Decimal("200"),
    lot_size=65,
    available=Decimal("50000"),
    deployed=Decimal("0"),
    equity=Decimal("50000"),
  )
  # 90% of 50k = 45000; per lot 13000 → 3 lots
  assert lots == 3
  assert prem == Decimal("39000")


def test_lots_for_confidence_disabled_uses_default():
  cfg = _dynamic_cfg()
  cfg["confidence_lot_sizing"]["enabled"] = False
  cfg["default_lots"] = 2
  assert lots_for_confidence(cfg, 95) == 2


def test_tier_mode_legacy():
  cfg = _tier_cfg()
  assert lots_for_confidence(cfg, 79) == 1
  assert lots_for_confidence(cfg, 80) == 2
  assert lots_for_confidence(cfg, 90) == 3


@pytest.mark.asyncio
async def test_size_entry_dynamic_high_confidence():
  config = SimpleNamespace(risk=_dynamic_cfg())
  risk = RiskEngine(config)  # type: ignore[arg-type]

  signal = _signal(95)
  option = OptionState(instrument_token="1", tsym="NIFTYPE", ltp=Decimal("80"))
  snap = DailyRiskSnapshot(
    trade_date=date.today(),
    starting_capital=Decimal("50000"),
    available_capital=Decimal("50000"),
    deployed_capital=Decimal("0"),
    realized_pnl=Decimal("0"),
    trade_count=0,
    consecutive_losses=0,
    kill_switch=False,
    entries_blocked=False,
  )

  sizing = await risk.size_entry(signal, option, snap)
  assert sizing.approved
  assert sizing.lots >= 6
  assert sizing.confidence == 95


@pytest.mark.asyncio
async def test_loss_stops_disabled_when_zero():
  config = SimpleNamespace(risk=_dynamic_cfg(max_consecutive_losses=0, max_daily_loss=0))
  risk = RiskEngine(config)  # type: ignore[arg-type]
  signal = _signal(85)
  option = OptionState(instrument_token="1", tsym="NIFTYPE", ltp=Decimal("80"))
  snap = DailyRiskSnapshot(
    trade_date=date.today(),
    starting_capital=Decimal("50000"),
    available_capital=Decimal("40000"),
    deployed_capital=Decimal("0"),
    realized_pnl=Decimal("-25000"),
    trade_count=12,
    consecutive_losses=20,
    kill_switch=False,
    entries_blocked=False,
  )
  sizing = await risk.size_entry(signal, option, snap)
  assert sizing.approved
  assert sizing.rejection_reason is None


@pytest.mark.asyncio
async def test_orb_strategy_lot_cap_not_applied_in_size_entry():
  """ORB used to cap at 4 lots; sizing is now margin + confidence + greeks only."""
  risk_cfg = _dynamic_cfg(strategy_lot_caps={"opening_range_breakout": 4})
  assert strategy_lot_cap(risk_cfg, "opening_range_breakout") == 4
  config = SimpleNamespace(risk=risk_cfg)
  risk = RiskEngine(config)  # type: ignore[arg-type]

  signal = _signal(95)
  signal.setup_type = "opening_range_breakout"
  option = OptionState(instrument_token="1", tsym="NIFTYPE", ltp=Decimal("80"))
  snap = DailyRiskSnapshot(
    trade_date=date.today(),
    starting_capital=Decimal("50000"),
    available_capital=Decimal("50000"),
    deployed_capital=Decimal("0"),
    realized_pnl=Decimal("0"),
    trade_count=0,
    consecutive_losses=0,
    kill_switch=False,
    entries_blocked=False,
  )

  sizing = await risk.size_entry(signal, option, snap)
  assert sizing.approved
  assert sizing.lots > 4


def test_risk_cfg_for_sizing_caps_expiry_deploy():
  from algoflat.risk.engine import risk_cfg_for_sizing

  base = _dynamic_cfg(
    max_premium_pct_of_available=90,
    max_deployed_pct_of_equity=90,
    expiry_day_sizing={"enabled": True, "max_capital_pct": 75},
  )
  merged = risk_cfg_for_sizing(base, is_expiry_day=True)
  assert merged["confidence_lot_sizing"]["max_capital_pct"] == 75
  assert merged["max_premium_pct_of_available"] == 75
  assert merged["max_deployed_pct_of_equity"] == 75

  normal = risk_cfg_for_sizing(base, is_expiry_day=False)
  assert normal["confidence_lot_sizing"]["max_capital_pct"] == 85
