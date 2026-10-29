"""Tests for opportunity scoring and enhanced strike selection."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from algoflat.contract_selector.selector import ContractUniverse
from algoflat.contract_selector.strike_picker import (
  candidate_instruments,
  effective_quote_band_steps,
  select_option_contract,
)
from algoflat.models.events import (
  CandidateSignal,
  FeatureSnapshot,
  Instrument,
  MarketRegime,
  OptionState,
)
from algoflat.strategy.opportunity_score import OpportunityScorer
from algoflat.strategy.router import StrategyRouter
from algoflat.quality.gate import QualityGate
from algoflat.scanner.library import FeatureSetupScanner


def _inst(side: str, strike: int, *, atm: int = 24050) -> Instrument:
  return Instrument(
    exchange="NFO",
    token=f"T-{side}-{strike}",
    tsym=f"NIFTY{strike}{side}",
    underlying="NIFTY",
    expiry_date=datetime(2026, 7, 16, 15, 30),
    strike=Decimal(strike),
    option_type=side,
    lot_size=65,
    is_atm=strike == atm,
    in_band=True,
  )


def _universe(atm: int = 24050) -> ContractUniverse:
  step = 50
  instruments = []
  for i in range(-3, 4):
    k = atm + i * step
    instruments.append(_inst("CE", k, atm=atm))
    instruments.append(_inst("PE", k, atm=atm))
  return ContractUniverse(
    spot=Decimal(atm),
    atm_strike=Decimal(atm),
    expiry_symbol="16JUL26",
    instruments=instruments,
    atm_ce=next(i for i in instruments if i.strike == atm and i.option_type == "CE"),
    atm_pe=next(i for i in instruments if i.strike == atm and i.option_type == "PE"),
  )


def _cfg() -> SimpleNamespace:
  return SimpleNamespace(
    strategy={
      "router": {"min_confidence": 76, "enabled_strategies": ["oi_breakout"]},
      "strike_selection": {
        "enabled": True,
        "atm_band_steps": 3,
        "prefer_atm_first": True,
        "modes": ["atm", "otm"],
        "otm_steps": [1, 2],
        "atm_target_delta": 0.50,
        "otm_target_delta": 0.35,
        "delta_min": 0.40,
        "delta_max": 0.65,
        "otm_delta_min": 0.25,
        "otm_delta_max": 0.45,
        "prefer_higher_gamma": True,
        "max_spread_pct": 8,
        "min_oi": 10000,
        "oi_weight": 40,
        "volume_weight": 10,
      },
      "opportunity_score": {
        "enabled": True,
        "max_bonus": 30,
        "oi_confirm_bonus": 12,
        "volume_bonus": 8,
        "oi_floor_bonus": 6,
        "greeks_bonus": 8,
        "oi_build_bonus": 6,
      },
      "regime": {
        "filter_mode": "soft",
        "sideways_penalty": 18,
        "gap_mode_penalty": 15,
        "expiry_mode_penalty": 12,
        "sideways_allowed_strategies": ["level_rejection"],
      },
    },
    symbols={"strike_step": 50},
    validator={"min_option_volume": 5000, "min_option_oi": 25000},
  )


def test_effective_quote_band_steps_includes_otm():
  cfg = {"atm_band_steps": 3, "modes": ["atm", "otm"], "otm_steps": [1, 2, 3]}
  assert effective_quote_band_steps(cfg) == 3


def test_candidate_instruments_includes_otm_ce():
  uni = _universe()
  ces = candidate_instruments(
    uni,
    "CE",
    band_steps=1,
    otm_steps=[1, 2],
    modes=["atm", "otm"],
    step=Decimal("50"),
  )
  strikes = sorted(float(i.strike) for i in ces)
  assert 24100.0 in strikes
  assert 24150.0 in strikes


def test_select_prefers_high_oi_strike():
  uni = _universe()
  states: dict[str, OptionState | None] = {}
  for inst in uni.instruments:
    if inst.option_type != "CE":
      continue
    if int(inst.strike) not in (24050, 24100, 24150):
      continue
    oi = 200_000 if int(inst.strike) == 24100 else 500
    states[inst.token] = OptionState(
      instrument_token=inst.token,
      tsym=inst.tsym,
      ltp=Decimal("95") if int(inst.strike) == 24100 else Decimal("145"),
      bid=Decimal("119"),
      ask=Decimal("121"),
      spread_pct=Decimal("1"),
      volume=100_000,
      oi=oi,
    )
  pick = select_option_contract(
    config=_cfg(),
    universe=uni,
    side="CE",
    spot=Decimal("24050"),
    option_states=states,
    expiry=datetime(2026, 7, 16).date(),
    now=datetime(2026, 7, 14, 11, 0),
  )
  assert pick is not None
  assert pick.instrument.strike == Decimal("24100")
  assert pick.moneyness == "otm"


def test_opportunity_scorer_oi_confirm_bonus():
  cfg = _cfg()
  scorer = OpportunityScorer(cfg)  # type: ignore[arg-type]
  signal = CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type="oi_breakout",
    side="CE",
    instrument_token="T",
    tsym="NCE",
    strategy_version="t",
    feature_snapshot=FeatureSnapshot(ts=datetime.now(tz=timezone.utc)),
  )
  features = FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    extra={"chain": {"oi_confirms_ce": True, "oi_delta_ce_atm": 500}},
  )
  bonus, logs = scorer.score(
    signal,
    features,
    MarketRegime(
      ts=datetime.now(tz=timezone.utc),
      primary="trending_up",
      trade_allowed=True,
      risk_score=30,
    ),
    context={"volume": 50_000, "oi": 40_000, "delta": 0.48},
  )
  assert bonus >= 20
  assert any("opp_oi_confirm_ce" in log for log in logs)


def test_opportunity_regime_soft_penalty():
  cfg = _cfg()
  scorer = OpportunityScorer(cfg)  # type: ignore[arg-type]
  features = FeatureSnapshot(ts=datetime.now(tz=timezone.utc), extra={})
  penalty, logs = scorer.regime_penalty(
    "momentum_continuation",
    MarketRegime(
      ts=datetime.now(tz=timezone.utc),
      primary="sideways",
      trade_allowed=True,
      risk_score=40,
    ),
    features,
  )
  assert penalty == 18
  assert any("sideways" in log for log in logs)


def test_router_soft_regime_keeps_all_scanners():
  cfg = _cfg()
  cfg.strategy["router"]["enabled_strategies"] = ["oi_breakout", "momentum_continuation"]
  scanners = [
    FeatureSetupScanner(cfg, "oi_breakout"),  # type: ignore[arg-type]
    FeatureSetupScanner(cfg, "momentum_continuation"),  # type: ignore[arg-type]
  ]
  router = StrategyRouter(cfg, scanners, QualityGate(cfg))  # type: ignore[arg-type]
  active, logs = router._active_scanners(
    MarketRegime(
      ts=datetime.now(tz=timezone.utc),
      primary="sideways",
      trade_allowed=True,
      risk_score=40,
    ),
    FeatureSnapshot(ts=datetime.now(tz=timezone.utc)),
  )
  names = {s.name for s in active}
  assert "momentum_continuation" in names
  assert "oi_breakout" in names
  assert any("soft" in log for log in logs)
