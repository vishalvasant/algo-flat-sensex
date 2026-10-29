# Algo-Flat Day Backtest Report — 2026-09-11 (NIFTY)

## Session overview

| Field | Value |
|---|---|
| Spot | 23405.0 → 23575.0 (Δ 170.0) |
| Session VWAP | 23465.28 |
| Pool equity | ₹25000 → ₹74403.14 |
| **Session P&L** | **₹49403.14** |
| Trades | 12 (10 wins / 2 losses) |
| Win rate | 83.3% |
| Peak deployed | ₹55965 (90.0% of equity) |
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

### Trade #4 — WIN ₹+2086.50

| Field | Value |
|---|---|
| Strategy | `vwap_pullback` |
| Contract | CE 23450 (`NIFTY 23450 CE 15 SEP 26`) |
| Confidence | 100 |
| Size | 10 lot(s) × 65 qty/lot = **650 units** |
| Entry deploy | **₹29,022.50** (10×65×₹44.65/unit) |
| Entry | **2026-09-11 10:32:00 IST** @ **₹44.65/unit** (spot 23447.60) |
| Exit | **2026-09-11 10:37:56 IST** @ **₹47.86/unit** (spot 23459.80) |
| Hold | 5.9 min |
| Premium Δ | +7.19% per unit |
| P&L calc | (exit−entry)×qty = (47.86−44.65)×650 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+2086.50** |

### Trade #5 — WIN ₹+3441.75

| Field | Value |
|---|---|
| Strategy | `vwap_trend` |
| Contract | CE 23450 (`NIFTY 23450 CE 15 SEP 26`) |
| Confidence | 100 |
| Size | 10 lot(s) × 65 qty/lot = **650 units** |
| Entry deploy | **₹31,005.00** (10×65×₹47.70/unit) |
| Entry | **2026-09-11 10:40:00 IST** @ **₹47.70/unit** (spot 23458.70) |
| Exit | **2026-09-11 10:45:38 IST** @ **₹52.99/unit** (spot 23457.00) |
| Hold | 5.6 min |
| Premium Δ | +11.10% per unit |
| P&L calc | (exit−entry)×qty = (52.99−47.70)×650 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+3441.75** |

### Trade #6 — WIN ₹+1144.00

| Field | Value |
|---|---|
| Strategy | `vwap_pullback` |
| Contract | CE 23450 (`NIFTY 23450 CE 15 SEP 26`) |
| Confidence | 100 |
| Size | 11 lot(s) × 65 qty/lot = **715 units** |
| Entry deploy | **₹32,032.00** (11×65×₹44.80/unit) |
| Entry | **2026-09-11 10:59:00 IST** @ **₹44.80/unit** (spot 23449.00) |
| Exit | **2026-09-11 11:02:44 IST** @ **₹46.40/unit** (spot 23450.60) |
| Hold | 3.7 min |
| Premium Δ | +3.57% per unit |
| P&L calc | (exit−entry)×qty = (46.40−44.80)×715 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+1144.00** |

### Trade #7 — WIN ₹+14805.27

| Field | Value |
|---|---|
| Strategy | `vwap_trend` |
| Contract | CE 23450 (`NIFTY 23450 CE 15 SEP 26`) |
| Confidence | 100 |
| Size | 11 lot(s) × 65 qty/lot = **715 units** |
| Entry deploy | **₹32,568.25** (11×65×₹45.55/unit) |
| Entry | **2026-09-11 11:06:00 IST** @ **₹45.55/unit** (spot 23448.40) |
| Exit | **2026-09-11 11:15:16 IST** @ **₹66.26/unit** (spot 23501.60) |
| Hold | 9.3 min |
| Premium Δ | +45.46% per unit |
| P&L calc | (exit−entry)×qty = (66.26−45.55)×715 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+14805.27** |

### Trade #8 — WIN ₹+1976.00

| Field | Value |
|---|---|
| Strategy | `vwap_trend` |
| Contract | CE 23500 (`NIFTY 23500 CE 15 SEP 26`) |
| Confidence | 100 |
| Size | 16 lot(s) × 65 qty/lot = **1040 units** |
| Entry deploy | **₹46,956.00** (16×65×₹45.15/unit) |
| Entry | **2026-09-11 12:21:00 IST** @ **₹45.15/unit** (spot 23493.30) |
| Exit | **2026-09-11 12:25:24 IST** @ **₹47.05/unit** (spot 23502.70) |
| Hold | 4.4 min |
| Premium Δ | +4.21% per unit |
| P&L calc | (exit−entry)×qty = (47.05−45.15)×1040 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+1976.00** |

### Trade #9 — WIN ₹+6669.00

| Field | Value |
|---|---|
| Strategy | `vwap_trend` |
| Contract | CE 23500 (`NIFTY 23500 CE 15 SEP 26`) |
| Confidence | 100 |
| Size | 19 lot(s) × 65 qty/lot = **1235 units** |
| Entry deploy | **₹50,017.50** (19×65×₹40.50/unit) |
| Entry | **2026-09-11 13:20:00 IST** @ **₹40.50/unit** (spot 23483.60) |
| Exit | **2026-09-11 13:30:56 IST** @ **₹45.90/unit** (spot 23516.00) |
| Hold | 10.9 min |
| Premium Δ | +13.33% per unit |
| P&L calc | (exit−entry)×qty = (45.90−40.50)×1235 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+6669.00** |

### Trade #10 — LOSS ₹-2795.00

| Field | Value |
|---|---|
| Strategy | `vwap_trend` |
| Contract | CE 23500 (`NIFTY 23500 CE 15 SEP 26`) |
| Confidence | 100 |
| Size | 20 lot(s) × 65 qty/lot = **1300 units** |
| Entry deploy | **₹55,185.00** (20×65×₹42.45/unit) |
| Entry | **2026-09-11 13:45:00 IST** @ **₹42.45/unit** (spot 23488.90) |
| Exit | **2026-09-11 13:45:40 IST** @ **₹40.30/unit** (spot 23488.90) |
| Hold | 0.7 min |
| Premium Δ | -5.06% per unit |
| P&L calc | (exit−entry)×qty = (40.30−42.45)×1300 |
| Exit reason | `adverse_momentum` |
| Marking | option_candle->option_candle |
| **P&L** | **₹-2795.00** |

### Trade #11 — WIN ₹+2197.65

| Field | Value |
|---|---|
| Strategy | `vwap_trend` |
| Contract | CE 23500 (`NIFTY 23500 CE 15 SEP 26`) |
| Confidence | 100 |
| Size | 21 lot(s) × 65 qty/lot = **1365 units** |
| Entry deploy | **₹53,644.50** (21×65×₹39.30/unit) |
| Entry | **2026-09-11 13:46:00 IST** @ **₹39.30/unit** (spot 23488.90) |
| Exit | **2026-09-11 13:48:52 IST** @ **₹40.91/unit** (spot 23501.60) |
| Hold | 2.9 min |
| Premium Δ | +4.10% per unit |
| P&L calc | (exit−entry)×qty = (40.91−39.30)×1365 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+2197.65** |

### Trade #12 — WIN ₹+12194.00

| Field | Value |
|---|---|
| Strategy | `vwap_trend` |
| Contract | CE 23500 (`NIFTY 23500 CE 15 SEP 26`) |
| Confidence | 100 |
| Size | 21 lot(s) × 65 qty/lot = **1365 units** |
| Entry deploy | **₹55,965.00** (21×65×₹41.00/unit) |
| Entry | **2026-09-11 13:49:20 IST** @ **₹41.00/unit** (spot 23502.00) |
| Exit | **2026-09-11 13:54:40 IST** @ **₹49.93/unit** (spot 23521.40) |
| Hold | 5.3 min |
| Premium Δ | +21.79% per unit |
| P&L calc | (exit−entry)×qty = (49.93−41.00)×1365 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+12194.00** |

## P&L running balance

| After trade | Cumulative P&L | Equity |
|---|---|---|
| #1 vwap_pullback CE | ₹+5084.62 | ₹30084.62 |
| #2 vwap_trend CE | ₹+9174.74 | ₹34174.74 |
| #3 vwap_trend CE | ₹+7683.97 | ₹32683.97 |
| #4 vwap_pullback CE | ₹+9770.47 | ₹34770.47 |
| #5 vwap_trend CE | ₹+13212.22 | ₹38212.22 |
| #6 vwap_pullback CE | ₹+14356.22 | ₹39356.22 |
| #7 vwap_trend CE | ₹+29161.49 | ₹54161.49 |
| #8 vwap_trend CE | ₹+31137.49 | ₹56137.49 |
| #9 vwap_trend CE | ₹+37806.49 | ₹62806.49 |
| #10 vwap_trend CE | ₹+35011.49 | ₹60011.49 |
| #11 vwap_trend CE | ₹+37209.14 | ₹62209.14 |
| #12 vwap_trend CE | ₹+49403.14 | ₹74403.14 |

## Exit reason breakdown

- `adverse_momentum`: 2 trades, P&L ₹-4285.77
- `momentum_trail`: 10 trades, P&L ₹+53688.91

_Generated by `scripts/day_backtest.py` · export `backtest_2026-09-11_NIFTY.json`_
