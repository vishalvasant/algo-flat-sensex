# Algo-Flat Day Backtest Report — 2026-08-31 (NIFTY)

## Session overview

| Field | Value |
|---|---|
| Spot | 24202.7 → 24254.9 (Δ 52.2) |
| Session VWAP | 24212.76 |
| Capital | ₹25000 → ₹37571.00 |
| **Net P&L** | **₹12571.00** |
| Trades | 12 (7 wins / 5 losses) |
| Win rate | 58.3% |
| Peak deployed | ₹33884 (89.0% of equity) |
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

### Trade #1 — WIN ₹+877.50

| Field | Value |
|---|---|
| Strategy | `delta_momentum` |
| Contract | PE 24200 (`NIFTY 24200 PE 01 SEP 26`) |
| Confidence | 98 |
| Size | 2 lot(s) × 65 qty/lot = **130 units** |
| Entry deploy | **₹21,222.50** (2×65×₹163.25/unit) |
| Entry | **2026-08-31 09:39:00 IST** @ **₹163.25/unit** (spot 24195.00) |
| Exit | **2026-08-31 09:46:00 IST** @ **₹170.00/unit** (spot 24190.00) |
| Hold | 7.0 min |
| Premium Δ | +4.13% per unit |
| P&L calc | (exit−entry)×qty = (170.00−163.25)×130 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+877.50** |

### Trade #2 — WIN ₹+578.50

| Field | Value |
|---|---|
| Strategy | `delta_momentum` |
| Contract | PE 24200 (`NIFTY 24200 PE 01 SEP 26`) |
| Confidence | 100 |
| Size | 2 lot(s) × 65 qty/lot = **130 units** |
| Entry deploy | **₹22,158.50** (2×65×₹170.45/unit) |
| Entry | **2026-08-31 09:49:00 IST** @ **₹170.45/unit** (spot 24186.30) |
| Exit | **2026-08-31 09:59:00 IST** @ **₹174.90/unit** (spot 24182.00) |
| Hold | 10.0 min |
| Premium Δ | +2.61% per unit |
| P&L calc | (exit−entry)×qty = (174.90−170.45)×130 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+578.50** |

### Trade #3 — LOSS ₹-552.50

| Field | Value |
|---|---|
| Strategy | `opening_range_breakout` |
| Contract | PE 24200 (`NIFTY 24200 PE 01 SEP 26`) |
| Confidence | 87 |
| Size | 1 lot(s) × 65 qty/lot = **65 units** |
| Entry deploy | **₹11,596.00** (1×65×₹178.40/unit) |
| Entry | **2026-08-31 10:17:00 IST** @ **₹178.40/unit** (spot 24184.60) |
| Exit | **2026-08-31 10:27:00 IST** @ **₹169.90/unit** (spot 24191.00) |
| Hold | 10.0 min |
| Premium Δ | -4.76% per unit |
| P&L calc | (exit−entry)×qty = (169.90−178.40)×65 |
| Exit reason | `adverse_momentum` |
| Marking | option_candle->option_candle |
| **P&L** | **₹-552.50** |

### Trade #4 — WIN ₹+523.25

| Field | Value |
|---|---|
| Strategy | `opening_range_breakout` |
| Contract | PE 24200 (`NIFTY 24200 PE 01 SEP 26`) |
| Confidence | 73 |
| Size | 1 lot(s) × 65 qty/lot = **65 units** |
| Entry deploy | **₹11,095.50** (1×65×₹170.70/unit) |
| Entry | **2026-08-31 10:33:59 IST** @ **₹170.70/unit** (spot 24182.20) |
| Exit | **2026-08-31 10:40:00 IST** @ **₹178.75/unit** (spot 24175.50) |
| Hold | 6.0 min |
| Premium Δ | +4.72% per unit |
| P&L calc | (exit−entry)×qty = (178.75−170.70)×65 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+523.25** |

### Trade #5 — WIN ₹+81.25

| Field | Value |
|---|---|
| Strategy | `delta_momentum` |
| Contract | PE 24150 (`NIFTY 24150 PE 01 SEP 26`) |
| Confidence | 98 |
| Size | 1 lot(s) × 65 qty/lot = **65 units** |
| Entry deploy | **₹9,035.00** (1×65×₹139.00/unit) |
| Entry | **2026-08-31 10:37:00 IST** @ **₹139.00/unit** (spot 24174.70) |
| Exit | **2026-08-31 10:40:00 IST** @ **₹140.25/unit** (spot 24175.50) |
| Hold | 3.0 min |
| Premium Δ | +0.90% per unit |
| P&L calc | (exit−entry)×qty = (140.25−139.00)×65 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+81.25** |

### Trade #6 — LOSS ₹-555.75

| Field | Value |
|---|---|
| Strategy | `opening_range_breakout` |
| Contract | PE 24200 (`NIFTY 24200 PE 01 SEP 26`) |
| Confidence | 76 |
| Size | 1 lot(s) × 65 qty/lot = **65 units** |
| Entry deploy | **₹11,901.50** (1×65×₹183.10/unit) |
| Entry | **2026-08-31 11:10:00 IST** @ **₹183.10/unit** (spot 24181.00) |
| Exit | **2026-08-31 11:15:00 IST** @ **₹174.55/unit** (spot 24188.70) |
| Hold | 5.0 min |
| Premium Δ | -4.67% per unit |
| P&L calc | (exit−entry)×qty = (174.55−183.10)×65 |
| Exit reason | `adverse_momentum` |
| Marking | option_candle->option_candle |
| **P&L** | **₹-555.75** |

### Trade #7 — LOSS ₹-1183.00

| Field | Value |
|---|---|
| Strategy | `vwap_reclaim` |
| Contract | CE 24200 (`NIFTY 24200 CE 01 SEP 26`) |
| Confidence | 100 |
| Size | 13 lot(s) × 65 qty/lot = **845 units** |
| Entry deploy | **₹22,519.25** (13×65×₹26.65/unit) |
| Entry | **2026-08-31 11:44:00 IST** @ **₹26.65/unit** (spot 24205.50) |
| Exit | **2026-08-31 11:48:00 IST** @ **₹25.25/unit** (spot 24206.60) |
| Hold | 4.0 min |
| Premium Δ | -5.25% per unit |
| P&L calc | (exit−entry)×qty = (25.25−26.65)×845 |
| Exit reason | `adverse_momentum` |
| Marking | option_candle->option_candle |
| **P&L** | **₹-1183.00** |

### Trade #8 — LOSS ₹+0.00

| Field | Value |
|---|---|
| Strategy | `momentum_continuation` |
| Contract | CE 24250 (`NIFTY 24250 CE 01 SEP 26`) |
| Confidence | 100 |
| Size | 18 lot(s) × 65 qty/lot = **1170 units** |
| Entry deploy | **₹21,703.50** (18×65×₹18.55/unit) |
| Entry | **2026-08-31 11:51:00 IST** @ **₹18.55/unit** (spot 24220.00) |
| Exit | **2026-08-31 11:53:00 IST** @ **₹18.55/unit** (spot 24223.50) |
| Hold | 2.0 min |
| Premium Δ | +0.00% per unit |
| P&L calc | (exit−entry)×qty = (18.55−18.55)×1170 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+0.00** |

### Trade #9 — WIN ₹+12483.25

| Field | Value |
|---|---|
| Strategy | `momentum_continuation` |
| Contract | CE 24300 (`NIFTY 24300 CE 01 SEP 26`) |
| Confidence | 100 |
| Size | 23 lot(s) × 65 qty/lot = **1495 units** |
| Entry deploy | **₹20,033.00** (23×65×₹13.40/unit) |
| Entry | **2026-08-31 11:54:00 IST** @ **₹13.40/unit** (spot 24260.20) |
| Exit | **2026-08-31 11:59:00 IST** @ **₹21.75/unit** (spot 24256.00) |
| Hold | 5.0 min |
| Premium Δ | +62.31% per unit |
| P&L calc | (exit−entry)×qty = (21.75−13.40)×1495 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+12483.25** |

### Trade #10 — WIN ₹+201.50

| Field | Value |
|---|---|
| Strategy | `momentum_continuation` |
| Contract | CE 24250 (`NIFTY 24250 CE 01 SEP 26`) |
| Confidence | 100 |
| Size | 1 lot(s) × 65 qty/lot = **65 units** |
| Entry deploy | **₹1,859.00** (1×65×₹28.60/unit) |
| Entry | **2026-08-31 11:55:00 IST** @ **₹28.60/unit** (spot 24260.00) |
| Exit | **2026-08-31 11:59:00 IST** @ **₹31.70/unit** (spot 24256.00) |
| Hold | 4.0 min |
| Premium Δ | +10.84% per unit |
| P&L calc | (exit−entry)×qty = (31.70−28.60)×65 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+201.50** |

### Trade #11 — WIN ₹+624.00

| Field | Value |
|---|---|
| Strategy | `momentum_continuation` |
| Contract | CE 24300 (`NIFTY 24300 CE 01 SEP 26`) |
| Confidence | 100 |
| Size | 24 lot(s) × 65 qty/lot = **1560 units** |
| Entry deploy | **₹32,448.00** (24×65×₹20.80/unit) |
| Entry | **2026-08-31 12:04:00 IST** @ **₹20.80/unit** (spot 24268.00) |
| Exit | **2026-08-31 12:06:00 IST** @ **₹21.20/unit** (spot 24255.60) |
| Hold | 2.0 min |
| Premium Δ | +1.92% per unit |
| P&L calc | (exit−entry)×qty = (21.20−20.80)×1560 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹+624.00** |

### Trade #12 — LOSS ₹-507.00

| Field | Value |
|---|---|
| Strategy | `pdh_pdl_break` |
| Contract | CE 24300 (`NIFTY 24300 CE 01 SEP 26`) |
| Confidence | 90 |
| Size | 26 lot(s) × 65 qty/lot = **1690 units** |
| Entry deploy | **₹33,884.50** (26×65×₹20.05/unit) |
| Entry | **2026-08-31 13:27:00 IST** @ **₹20.05/unit** (spot 24264.90) |
| Exit | **2026-08-31 13:36:00 IST** @ **₹19.75/unit** (spot 24260.10) |
| Hold | 9.0 min |
| Premium Δ | -1.50% per unit |
| P&L calc | (exit−entry)×qty = (19.75−20.05)×1690 |
| Exit reason | `momentum_trail` |
| Marking | option_candle->option_candle |
| **P&L** | **₹-507.00** |

## P&L running balance

| After trade | Cumulative P&L | Equity |
|---|---|---|
| #1 delta_momentum PE | ₹+877.50 | ₹25877.50 |
| #2 delta_momentum PE | ₹+1456.00 | ₹26456.00 |
| #3 opening_range_breakout PE | ₹+903.50 | ₹25903.50 |
| #4 opening_range_breakout PE | ₹+1426.75 | ₹26426.75 |
| #5 delta_momentum PE | ₹+1508.00 | ₹26508.00 |
| #6 opening_range_breakout PE | ₹+952.25 | ₹25952.25 |
| #7 vwap_reclaim CE | ₹-230.75 | ₹24769.25 |
| #8 momentum_continuation CE | ₹-230.75 | ₹24769.25 |
| #9 momentum_continuation CE | ₹+12252.50 | ₹37252.50 |
| #10 momentum_continuation CE | ₹+12454.00 | ₹37454.00 |
| #11 momentum_continuation CE | ₹+13078.00 | ₹38078.00 |
| #12 pdh_pdl_break CE | ₹+12571.00 | ₹37571.00 |

## Exit reason breakdown

- `adverse_momentum`: 3 trades, P&L ₹-2291.25
- `momentum_trail`: 9 trades, P&L ₹+14862.25

_Generated by `scripts/day_backtest.py` · export `backtest_2026-08-31_NIFTY.json`_
