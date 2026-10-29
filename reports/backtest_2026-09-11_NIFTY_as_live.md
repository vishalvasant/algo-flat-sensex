# Algo-Flat Day Backtest Report — 2026-09-11 (NIFTY)

## Session overview

| Field | Value |
|---|---|
| Spot | 23405.0 → 23575.0 (Δ 170.0) |
| Session VWAP | 23465.28 |
| Pool equity | ₹25000 → ₹49454.30 |
| **Session P&L** | **₹24454.30** |
| Trades | 6 (5 wins / 1 losses) |
| Win rate | 83.3% |
| Peak deployed | ₹30322 (88.9% of equity) |
| Deploy target | 90% |

## Rules used

- **NIFTY lot sizing**: 1 lot = **65 qty**; deploy = premium/unit × 65 × lots
- Confidence-based dynamic lots toward deploy room (not 1 qty per lot)
- Multiple concurrent positions (different strike/side)
- Max ~85% equity deployed; per-trade premium ≤ configured % of available
- **5% max stop** on losers; early cut if never +2% green
- **Progressive trailing** from +1.5% MFE (tighter lock-in as profit grows)
- **No time exit while in profit** — winners run on trailing SL
- Trend reversal (VWAP bias flip) still exits immediately
- Strike pick among **ATM / ATM±1** via Black-Scholes delta/gamma + spread
- **Live-parity cadence**: exit ticks ~2s (quote pace); entry scans every scan_interval + post-exit
- Spot/premium path visits OHLC extremes (tick-like adverse/trail)
- No fixed profit target wait
- Premium marked from **live option 1m candles** (intra-bar OHLC path)

## Trade-by-trade detail

### Trade #1 — WIN ₹+5084.62

| Field | Value |
|---|---|
| Strategy | `vwap_pullback` |
| Contract | CE 23450 (`NIFTY 23450 CE 15 SEP 26`) |
| Confidence | 100 |
| Size | 9 lot(s) × 65 qty/lot = **585 units** |
| Entry deploy | **₹21,557.25** (9×65×₹36.85/unit) |
| Entry | **2026-09-11 10:06:00 IST** @ **₹36.85/unit** (spot 23425.10) |
| Exit | **2026-09-11 10:14:50 IST** @ **₹45.54/unit** (spot 23442.00) |
| Hold | 8.8 min |
| Premium Δ | +23.59% per unit |
| P&L calc | (exit−entry)×qty = (45.54−36.85)×585 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+5084.62** |

### Trade #2 — WIN ₹+4090.12

| Field | Value |
|---|---|
| Strategy | `vwap_trend` |
| Contract | CE 23450 (`NIFTY 23450 CE 15 SEP 26`) |
| Confidence | 100 |
| Size | 9 lot(s) × 65 qty/lot = **585 units** |
| Entry deploy | **₹26,169.00** (9×65×₹44.73/unit) |
| Entry | **2026-09-11 10:15:10 IST** @ **₹44.73/unit** (spot 23440.00) |
| Exit | **2026-09-11 10:23:30 IST** @ **₹51.73/unit** (spot 23453.00) |
| Hold | 8.3 min |
| Premium Δ | +15.63% per unit |
| P&L calc | (exit−entry)×qty = (51.73−44.73)×585 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+4090.12** |

### Trade #3 — LOSS ₹-1490.77

| Field | Value |
|---|---|
| Strategy | `vwap_trend` |
| Contract | CE 23450 (`NIFTY 23450 CE 15 SEP 26`) |
| Confidence | 100 |
| Size | 9 lot(s) × 65 qty/lot = **585 units** |
| Entry deploy | **₹29,430.38** (9×65×₹50.31/unit) |
| Entry | **2026-09-11 10:23:50 IST** @ **₹50.31/unit** (spot 23453.00) |
| Exit | **2026-09-11 10:25:52 IST** @ **₹47.76/unit** (spot 23443.90) |
| Hold | 2.0 min |
| Premium Δ | -5.07% per unit |
| P&L calc | (exit−entry)×qty = (47.76−50.31)×585 |
| Exit reason | `adverse_momentum` |
| Marking | option_candle->option_candle |
| **P&L** | **₹-1490.77** |

### Trade #4 — WIN ₹+2037.75

| Field | Value |
|---|---|
| Strategy | `vwap_trend` |
| Contract | CE 23450 (`NIFTY 23450 CE 15 SEP 26`) |
| Confidence | 98 |
| Size | 10 lot(s) × 65 qty/lot = **650 units** |
| Entry deploy | **₹29,071.25** (10×65×₹44.73/unit) |
| Entry | **2026-09-11 10:32:30 IST** @ **₹44.73/unit** (spot 23447.60) |
| Exit | **2026-09-11 10:37:56 IST** @ **₹47.86/unit** (spot 23459.80) |
| Hold | 5.4 min |
| Premium Δ | +7.01% per unit |
| P&L calc | (exit−entry)×qty = (47.86−44.73)×650 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+2037.75** |

### Trade #5 — WIN ₹+2044.58

| Field | Value |
|---|---|
| Strategy | `vwap_trend` |
| Contract | CE 23450 (`NIFTY 23450 CE 15 SEP 26`) |
| Confidence | 98 |
| Size | 9 lot(s) × 65 qty/lot = **585 units** |
| Entry deploy | **₹28,957.50** (9×65×₹49.50/unit) |
| Entry | **2026-09-11 10:44:00 IST** @ **₹49.50/unit** (spot 23460.00) |
| Exit | **2026-09-11 10:45:38 IST** @ **₹52.99/unit** (spot 23457.00) |
| Hold | 1.6 min |
| Premium Δ | +7.06% per unit |
| P&L calc | (exit−entry)×qty = (52.99−49.50)×585 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+2044.58** |

### Trade #6 — WIN ₹+12688.00

| Field | Value |
|---|---|
| Strategy | `vwap_trend` |
| Contract | CE 23450 (`NIFTY 23450 CE 15 SEP 26`) |
| Confidence | 98 |
| Size | 10 lot(s) × 65 qty/lot = **650 units** |
| Entry deploy | **₹30,322.50** (10×65×₹46.65/unit) |
| Entry | **2026-09-11 11:07:00 IST** @ **₹46.65/unit** (spot 23459.00) |
| Exit | **2026-09-11 11:15:18 IST** @ **₹66.17/unit** (spot 23501.60) |
| Hold | 8.3 min |
| Premium Δ | +41.84% per unit |
| P&L calc | (exit−entry)×qty = (66.17−46.65)×650 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+12688.00** |

## P&L running balance

| After trade | Cumulative P&L | Equity |
|---|---|---|
| #1 vwap_pullback CE | ₹+5084.62 | ₹30084.62 |
| #2 vwap_trend CE | ₹+9174.74 | ₹34174.74 |
| #3 vwap_trend CE | ₹+7683.97 | ₹32683.97 |
| #4 vwap_trend CE | ₹+9721.72 | ₹34721.72 |
| #5 vwap_trend CE | ₹+11766.30 | ₹36766.30 |
| #6 vwap_trend CE | ₹+24454.30 | ₹49454.30 |

## Exit reason breakdown

- `adverse_momentum`: 1 trades, P&L ₹-1490.77
- `momentum_trail`: 5 trades, P&L ₹+25945.07

_Generated by `scripts/day_backtest.py` · export `backtest_2026-09-11_NIFTY.json`_
