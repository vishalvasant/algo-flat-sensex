from datetime import datetime, timedelta, timezone
from decimal import Decimal

from algoflat.contract_selector.selector import ContractUniverse
from algoflat.models.events import (
  Bias,
  Candle,
  CandleInterval,
  CandidateSignal,
  FeatureSnapshot,
  Instrument,
  OptionState,
)
from algoflat.trading.option_lift import find_option_lift, find_option_lifts, split_confirmed_lifts
from algoflat.validator.trap_avoidance import trap_rejection_reasons

WHEN = datetime(2026, 10, 9, 4, 30, tzinfo=timezone.utc)
CFG = {
  "atm_band_steps": 2,
  "strike_step": 50,
  "lookback_bars": 3,
  "volume_lookback_bars": 5,
  "min_closed_bars": 6,
  "min_premium_rise_pct": 0.75,
  "min_volume_ratio": 1.8,
  "min_bar_volume": 1000,
  "index_lookback_bars": 3,
  "min_index_move_points": 8,
}


def _inst(token: str, side: str, strike: int) -> Instrument:
  return Instrument(
    exchange="NFO",
    token=token,
    tsym=f"NIFTY{side}{strike}",
    underlying="NIFTY",
    strike=Decimal(strike),
    option_type=side,
    lot_size=65,
  )


def _bars(token: str, closes: list[str], volumes: list[int], *, red_last: bool = False) -> list[Candle]:
  start = WHEN - timedelta(minutes=len(closes) + 1)
  rows: list[Candle] = []
  for i, (px, vol) in enumerate(zip(closes, volumes)):
    close = Decimal(px)
    open_px = close + Decimal("1") if red_last and i == len(closes) - 1 else close - Decimal("0.4")
    rows.append(
      Candle(
        instrument_token=token,
        ts=start + timedelta(minutes=i),
        open=open_px,
        high=max(open_px, close) + Decimal("0.2"),
        low=min(open_px, close) - Decimal("0.2"),
        close=close,
        volume=vol,
        interval=CandleInterval.M1,
      )
    )
  return rows


def _spot_bars(level: str = "22400") -> list[Candle]:
  return _bars("26000", [level] * 8, [0] * 8)


def _spot_move(start: str, end: str, *, n: int = 8) -> list[Candle]:
  """Flat, then a 3-bar push so the index-move check sees start → end."""
  head = [start] * (n - 3)
  a = Decimal(start)
  b = Decimal(end)
  step = (b - a) / Decimal("3")
  tail = [str(a + step), str(a + step * 2), end]
  return _bars("26000", head + tail, [0] * n)


def _features(spot: str, vwap: str) -> FeatureSnapshot:
  return FeatureSnapshot(
    ts=WHEN,
    nifty_spot=Decimal(spot),
    session_vwap=Decimal(vwap),
    bias_5m=Bias.BEARISH,
    extra={"cash_index_spot": float(spot)},
  )


def _universe() -> ContractUniverse:
  instruments = []
  for side in ("CE", "PE"):
    for strike in (22350, 22400, 22450):
      instruments.append(_inst(f"{side}{strike}", side, strike))
  return ContractUniverse(
    spot=Decimal("22400"),
    atm_strike=Decimal("22400"),
    instruments=instruments,
  )


def _states(universe: ContractUniverse) -> dict[str, OptionState]:
  return {
    inst.token: OptionState(instrument_token=inst.token, tsym=inst.tsym, ltp=Decimal("100"))
    for inst in universe.instruments
  }


def test_put_premium_rise_enters_when_index_is_below_vwap() -> None:
  universe = _universe()
  flat = ["100"] * 7
  rising = ["100", "100", "100", "100", "100", "101", "103"]
  option_m1 = {
    inst.token: _bars(inst.token, rising if inst.token == "PE22400" else flat, [500] * 7)
    for inst in universe.instruments
  }
  signal, note = find_option_lift(
    features=_features("22380", "22440"),
    universe=universe,
    band_states=_states(universe),
    option_m1_by_token=option_m1,
    spot_m1=_spot_move("22420", "22380"),
    cfg=CFG,
    strategy_version="test",
    when=WHEN,
  )
  assert signal is not None
  assert signal.side == "PE"
  assert signal.tsym == "NIFTYPE22400"
  assert signal.setup_type == "option_lift"
  assert note["lift"] == "premium"
  assert "below vwap" in note["reason"]


def test_call_is_blocked_while_index_is_below_vwap_and_not_rising() -> None:
  universe = _universe()
  rising = ["100", "100", "100", "100", "100", "102", "104"]
  option_m1 = {
    inst.token: _bars(inst.token, rising if inst.option_type == "CE" else ["100"] * 7, [500] * 7)
    for inst in universe.instruments
  }
  signal, note = find_option_lift(
    features=_features("22380", "22440"),
    universe=universe,
    band_states=_states(universe),
    option_m1_by_token=option_m1,
    spot_m1=_spot_bars("22380"),
    cfg=CFG,
    strategy_version="test",
    when=WHEN,
  )
  assert signal is None
  assert "no premium or volume lift" in note["reason"]


def test_call_volume_lift_enters_when_index_is_above_vwap() -> None:
  universe = _universe()
  volumes = [400, 400, 400, 400, 400, 400, 4000]
  option_m1 = {
    inst.token: _bars(
      inst.token,
      ["100"] * 7,
      volumes if inst.token == "CE22450" else [400] * 7,
    )
    for inst in universe.instruments
  }
  signal, note = find_option_lift(
    features=_features("22480", "22420"),
    universe=universe,
    band_states=_states(universe),
    option_m1_by_token=option_m1,
    spot_m1=_spot_move("22440", "22480"),
    cfg=CFG,
    strategy_version="test",
    when=WHEN,
  )
  assert signal is not None
  assert signal.side == "CE"
  assert signal.tsym == "NIFTYCE22450"
  assert note["lift"] == "volume"


def test_red_premium_bar_does_not_enter() -> None:
  universe = _universe()
  rising = ["100", "100", "100", "100", "100", "104", "102"]
  option_m1 = {
    inst.token: _bars(
      inst.token,
      rising if inst.token == "PE22400" else ["100"] * 7,
      [2000] * 7,
      red_last=inst.token == "PE22400",
    )
    for inst in universe.instruments
  }
  signal, _note = find_option_lift(
    features=_features("22380", "22440"),
    universe=universe,
    band_states=_states(universe),
    option_m1_by_token=option_m1,
    spot_m1=_spot_bars("22380"),
    cfg=CFG,
    strategy_version="test",
    when=WHEN,
  )
  assert signal is None


def test_two_puts_both_qualify_when_each_premium_is_rising() -> None:
  universe = _universe()
  mild = ["100", "100", "100", "100", "100", "101", "103"]
  strong = ["100", "100", "100", "100", "100", "103", "106"]
  option_m1 = {
    inst.token: _bars(
      inst.token,
      strong if inst.token == "PE22400" else mild if inst.token == "PE22350" else ["100"] * 7,
      [500] * 7,
    )
    for inst in universe.instruments
  }
  signals, note = find_option_lifts(
    features=_features("22380", "22440"),
    universe=universe,
    band_states=_states(universe),
    option_m1_by_token=option_m1,
    spot_m1=_spot_move("22420", "22380"),
    cfg=CFG,
    strategy_version="test",
    when=WHEN,
  )
  assert [sig.tsym for sig in signals] == ["NIFTYPE22400", "NIFTYPE22350"]
  assert {sig.side for sig in signals} == {"PE"}
  assert note["candidates"] == 2
  assert "2 lifts" in note["reason"]


def test_every_rising_call_is_a_candidate() -> None:
  instruments = [_inst(f"CE{strike}", "CE", strike) for strike in (22400, 22450, 22500, 22550, 22600)]
  universe = ContractUniverse(
    spot=Decimal("22500"),
    atm_strike=Decimal("22500"),
    instruments=instruments,
  )
  rising = ["100", "100", "100", "100", "100", "102", "105"]
  option_m1 = {
    inst.token: _bars(
      inst.token,
      rising if inst.strike >= 22500 else ["100"] * 7,
      [500] * 7,
    )
    for inst in instruments
  }
  option_m1["CE22450"] = _bars("CE22450", ["100"] * 7, [500] * 7, red_last=True)
  signals, note = find_option_lifts(
    features=_features("22520", "22480"),
    universe=universe,
    band_states=_states(universe),
    option_m1_by_token=option_m1,
    spot_m1=_spot_move("22480", "22520"),
    cfg=CFG,
    strategy_version="test",
    when=WHEN,
  )
  assert {sig.tsym for sig in signals} == {
    "NIFTYCE22500",
    "NIFTYCE22550",
    "NIFTYCE22600",
  }


def test_every_rising_put_is_a_candidate() -> None:
  instruments = [_inst(f"PE{strike}", "PE", strike) for strike in (22400, 22450, 22500, 22550, 22600)]
  universe = ContractUniverse(
    spot=Decimal("22500"),
    atm_strike=Decimal("22500"),
    instruments=instruments,
  )
  rising = ["100", "100", "100", "100", "100", "102", "105"]
  option_m1 = {
    inst.token: _bars(
      inst.token,
      rising if inst.strike <= 22500 else ["100"] * 7,
      [500] * 7,
    )
    for inst in instruments
  }
  signals, _note = find_option_lifts(
    features=_features("22470", "22520"),
    universe=universe,
    band_states=_states(universe),
    option_m1_by_token=option_m1,
    spot_m1=_spot_move("22510", "22470"),
    cfg=CFG,
    strategy_version="test",
    when=WHEN,
  )
  assert {sig.tsym for sig in signals} == {
    "NIFTYPE22500",
    "NIFTYPE22450",
    "NIFTYPE22400",
  }


def test_flat_index_above_vwap_still_buys_the_call() -> None:
  universe = _universe()
  rising = ["100", "100", "100", "100", "100", "102", "104"]
  option_m1 = {
    inst.token: _bars(inst.token, rising if inst.option_type == "CE" else ["100"] * 7, [500] * 7)
    for inst in universe.instruments
  }
  signal, note = find_option_lift(
    features=_features("22480", "22420"),
    universe=universe,
    band_states=_states(universe),
    option_m1_by_token=option_m1,
    spot_m1=_spot_bars("22480"),
    cfg=CFG,
    strategy_version="test",
    when=WHEN,
  )
  assert signal is not None
  assert signal.side == "CE"
  assert "index is flat" not in note["reason"]


def test_call_above_vwap_buys_without_a_range_gate() -> None:
  universe = _universe()
  rising = ["100", "100", "100", "100", "100", "102", "104"]
  option_m1 = {
    inst.token: _bars(inst.token, rising if inst.token == "CE22450" else ["100"] * 7, [500] * 7)
    for inst in universe.instruments
  }
  boxed = ["22470"] * 19 + ["22474", "22476", "22478"]
  signal, note = find_option_lift(
    features=_features("22478", "22440"),
    universe=universe,
    band_states=_states(universe),
    option_m1_by_token=option_m1,
    spot_m1=_bars("26000", boxed, [0] * len(boxed)),
    cfg=CFG,
    strategy_version="test",
    when=WHEN,
  )
  assert signal is not None
  assert signal.side == "CE"
  assert "sideways" not in note["reason"]


def test_lift_waits_for_a_second_pass_before_it_is_ready() -> None:
  first = CandidateSignal(
    ts=WHEN,
    setup_type="option_lift",
    side="CE",
    instrument_token="CE22400",
    tsym="NIFTYCE22400",
    strategy_version="test",
    feature_snapshot=FeatureSnapshot(ts=WHEN, nifty_spot=Decimal("22400"), bias_5m=Bias.BULLISH),
    confidence=80,
  )
  other = first.model_copy(update={"instrument_token": "CE22450", "tsym": "NIFTYCE22450"})
  ready, waiting, streaks = split_confirmed_lifts([first, other], {}, required=2)
  assert ready == []
  assert [sig.tsym for sig, _n in waiting] == ["NIFTYCE22400", "NIFTYCE22450"]
  assert streaks == {"CE22400:CE": 1, "CE22450:CE": 1}

  ready, waiting, streaks = split_confirmed_lifts([first], streaks, required=2)
  assert [sig.tsym for sig in ready] == ["NIFTYCE22400"]
  assert waiting == []
  assert "CE22450:CE" not in streaks


def test_option_lift_skips_the_setup_trap_list() -> None:
  signal = CandidateSignal(
    ts=WHEN,
    setup_type="option_lift",
    side="PE",
    instrument_token="1",
    tsym="NIFTYPE22400",
    strategy_version="test",
    feature_snapshot=FeatureSnapshot(
      ts=WHEN,
      nifty_spot=Decimal("22500"),
      session_vwap=Decimal("22400"),
      bias_5m=Bias.BULLISH,
    ),
    confidence=80,
  )
  assert trap_rejection_reasons(signal, {"enabled": True, "require_spot_vwap_alignment": True}) == []
