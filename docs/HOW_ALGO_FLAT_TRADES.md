# How Algo-Flat Finds and Takes Trades

A complete guide to how the algo-flat trading engine decides **when to enter**, **which option to buy**, and **when to exit**. Written for operators and strategy tuning — traced from live code in `trading-engine/src/algoflat/`.

**Related:** [END_TO_END_TRADING_FLOW.md](./END_TO_END_TRADING_FLOW.md) (technical audit) · [ARCHITECTURE.md](../ARCHITECTURE.md)

---

## Table of contents

1. [The big picture](#1-the-big-picture)
2. [What data the engine watches](#2-what-data-the-engine-watches)
3. [The scan loop (every ~10 seconds)](#3-the-scan-loop-every-10-seconds)
4. [How setups are detected (spot-led)](#4-how-setups-are-detected-spot-led)
5. [All enabled strategies explained](#5-all-enabled-strategies-explained)
6. [How the router picks one trade](#6-how-the-router-picks-one-trade)
7. [Entry filters (validator + risk)](#7-entry-filters-validator--risk)
8. [Strike and lot sizing](#8-strike-and-lot-sizing)
9. [How exits work](#9-how-exits-work)
10. [Worked example: one trade from scan to exit](#10-worked-example-one-trade-from-scan-to-exit)
11. [Why most scans become NO_TRADE](#11-why-most-scans-become-no_trade)
12. [Spot vs option charts (important)](#12-spot-vs-option-charts-important)
13. [Config cheat sheet](#13-config-cheat-sheet)
14. [File map](#14-file-map)

---

## 1. The big picture

Algo-flat is an **intraday NIFTY weekly options** system. It does **not** trade NIFTY futures or equity directly — it buys **CE or PE** based on spot structure.

```
Market data (spot + options)
        ↓
   Every ~10s scan
        ↓
   Detect pattern on NIFTY spot candles
        ↓
   Score confidence (quality gate)
        ↓
   Pick best strategy (router)
        ↓
   Validate (spread, MTF, traps, time-of-day)
        ↓
   Size position (capital × confidence × Greeks)
        ↓
   BUY option (paper or live via Flattrade)
        ↓
   Monitor option premium on every quote (~2s)
        ↓
   SELL when exit rule fires (or 15:10 force exit)
```

**Core idea:** Entries are driven by **NIFTY spot** multi-timeframe structure (VWAP, bias, levels). Options are used for **strike selection**, **premium P&L**, and **liquidity/Greeks filters** — not as the primary chart for setup detection.

---

## 2. What data the engine watches

### Spot (NIFTY index / futures proxy)

| Timeframe | Source | Used for |
|-----------|--------|----------|
| **1m** | Flattrade TPSeries + live ticks | Session VWAP, 1m triggers, opening range, momentum bars |
| **3m** | Aggregated / broker | VWAP reclaim setup, pullback setup, bars-with-bias count |
| **5m** | Aggregated / broker | **5m bias** (bull/bear vs VWAP), structure (HH/HL), level breaks |
| **15m** | **Built from 5m** (not a separate feed) | Trend day, peak reversal fade, MTF patterns |

### Options (ATM band)

Each scan refreshes quotes for **ATM ± 2 strikes** (CE and PE):

- LTP, bid/ask, spread
- Volume, open interest
- Greeks (delta, gamma, IV) — computed from spot + premium
- Chain snapshot: PCR, max pain, OI build-up flags

### Prior day levels

Loaded at session start:

- Previous day high (PDH), low (PDL), close
- CPR top/bottom (central pivot range)
- Used for gap classification, PDH/PDL break, level rejection

### Scan cadence

| Event | Frequency |
|-------|-----------|
| Entry scan | Every **10s** (`scan_interval_seconds`) + quote-triggered (throttled) |
| Exit check | Every **option quote** (~2s REST poll or WebSocket) |
| Candle refresh | Each entry scan |
| Force exit | **15:10 IST** |

Config: `config/runtime_config.yaml`

---

## 3. The scan loop (every ~10 seconds)

Entry point: `TradingOrchestrator._scan_for_entry_inner()` in `trading/orchestrator.py`.

### Step-by-step

| Step | What happens | Skip if |
|------|--------------|---------|
| **1** | Refresh 1m/3m/5m candles from broker | Stale feed → NO_TRADE |
| **2** | Retarget ATM strike from live spot | — |
| **3** | Refresh option band quotes + chain snapshot | — |
| **4** | `FeatureEngine.compute()` — bias, setups, levels, MTF | Missing spot/VWAP |
| **5** | `RegimeClassifier` — is environment tradeable? | `trade_allowed = false` |
| **6** | Run all **enabled scanners** → candidate signals | None fire |
| **7** | `QualityGate` scores each signal → confidence 0–100 | — |
| **8** | `StrategyRouter` picks highest confidence | Best < 76 |
| **9** | `EntryConfirmationTracker` — signal must repeat 2 scans* | Still waiting |
| **10** | `RuleValidator` — spread, MTF, traps, TOD | Any rule fails |
| **11** | `RiskEngine` — lots, capital caps | Insufficient capital |
| **12** | `ExecutionEngine` — place BUY order | Broker reject |

\*Gap/ORB setups in first 15 minutes after 09:30 can skip the 2-scan wait. Confidence ≥ 90 can bypass confirmation.

### Regime gate (before any strategy runs)

`RegimeClassifier` labels the session:

| Regime | Meaning |
|--------|---------|
| `trending_up` / `trending_down` | Clear 5m bias vs VWAP |
| `sideways` | Mixed structure, tight range — **often blocks new entries** |
| `opening_range` | First ~45 min after 09:30 |
| `expiry_behaviour` | Weekly expiry day |
| `high_volatility` / `low_volatility` | 1m ATR vs average |

If `risk_score > 85` or sideways block is on → **no trades** regardless of setup.

---

## 4. How setups are detected (spot-led)

All pattern flags are computed in `FeatureEngine` → `features/setups.py` from **spot candles**.

### 5m bias (foundation for most trades)

```
BULLISH  = 5m close above session VWAP (with structure confirmation)
BEARISH  = 5m close below session VWAP
NEUTRAL  = ambiguous → most strategies skip
```

### VWAP reclaim (3m setup + 1m trigger)

Classic institutional entry:

1. **3m setup:** Price was on wrong side of VWAP, now reclaiming
   - Bull: `vwap_reclaim_bull` — was below, crossing back above
   - Bear: `vwap_reclaim_bear` — was above, crossing back below
2. **1m trigger:** Confirmation candle on 1m (`vwap_reclaim_cross_up` / `cross_down`)

→ Scanner buys **CE** if bull, **PE** if bear.

### VWAP pullback

Trend continuation after extension:

1. Price extended away from VWAP (min 12 points)
2. Pulls back toward VWAP without breaking bias
3. **1m trigger:** bounce in trend direction (`vwap_pullback_bounce_up` / `bounce_down`)

### VWAP trend

Sustained trend along VWAP side:

- Min 4 bars on same side of VWAP
- Distance from VWAP between 10–40 points
- 1m momentum required

### VWAP rejection

Fade at VWAP:

- Bear: wick above VWAP, close back below (rejection candle)
- Bull: wick below VWAP, close back above

### Levels map

Each scan builds a level map from PDH/PDL, CPR, opening range high/low:

- **Opening range breakout (ORB):** Spot breaks OR high (CE) or OR low (PE) with bullish/bearish bias
- **PDH/PDL break:** Spot breaks prior day high/low with 5m close confirmation
- **Level rejection / peak reversal fade:** Counter-trend fades at extended levels (uses 5m + 15m)

### Gap mode (first hour)

At open, gap = today open − prior close:

| Gap | Allowed setups (opening phase) |
|-----|-------------------------------|
| Gap up ≥ 35 pts | `gap_and_go` CE, `gap_fade` PE, ORB CE |
| Gap down ≤ −35 pts | `gap_and_go` PE, `gap_fade` CE, ORB PE |
| Flat | Normal strategy set |

**Trap rule:** On gap down, trend CE setups blocked while spot still above OR low (don't catch falling knife).

---

## 5. All enabled strategies explained

Current list: `config/strategy_config.yaml` → `router.enabled_strategies`

| Strategy | What it looks for | Typical side | When it works best |
|----------|-------------------|--------------|-------------------|
| **vwap_reclaim** | 3m reclaim + 1m cross of VWAP | CE/PE with bias | Trend resumption after dip/rally |
| **vwap_pullback** | Extended move, pullback to VWAP, bounce trigger | With trend | Mid-session trend days |
| **vwap_rejection** | Wick rejection at VWAP, close other side | Fade direction | Range / chop near VWAP |
| **vwap_trend** | Sustained bars on one side of VWAP | With trend | Strong trend sessions |
| **opening_range_breakout** | Break of first 15–30m range | CE above ORH / PE below ORL | Volatile opens |
| **gap_and_go** | Gap + price continuing in gap direction | CE on gap up / PE on gap down | Strong gap days |
| **gap_fade** | Gap + rejection at OR/VWAP | Opposite to gap | Gap exhaustion |
| **pdh_pdl_break** | Break prior day high/low with 5m confirm | With break direction | Level break days |
| **momentum_continuation** | 4 consecutive 1m closes in bias direction, beyond VWAP | With momentum | Strong intraday momentum |
| **trend_day** | 15m structure aligned with VWAP bias | With day trend | Trending days |
| **expiry_scalping** | Quick 1m scalps on expiry day only | Short hold | Expiry afternoon |
| **peak_reversal_fade** | Extended move + 5m/15m reversal signals | **Counter** bias (PE in bull) | Overextended moves |
| **level_rejection** | Price rejects PDH/PDL/OR level | Counter at level | Range boundaries |
| **oi_breakout** | OI confirms CE/PE + long build-up in chain | With OI direction | OI-led moves |
| **delta_momentum** | Option delta ≥ 0.45 aligned with bias | With delta | When ATM option has strong delta |

### How a scanner becomes an order

```
Feature flag true (e.g. setup = "bull")
    → Scanner emits CandidateSignal(setup_type, side=CE, token, tsym)
    → QualityGate adds confidence score
    → Router compares all signals
```

If **no** setup flag is true for any enabled strategy → `no_strategy_produced_signal` (most common outcome).

---

## 6. How the router picks one trade

File: `strategy/router.py`

1. **Session allowlist** — expiry day / gap mode may restrict which strategies can run
2. **Run each enabled scanner** — each returns a signal or nothing
3. **Quality gate** — scores each signal 0–100:

| Component | Max points |
|-----------|------------|
| Spot vs VWAP alignment | 20 |
| Option VWAP / LTP quality | 15 |
| Regime fit | 15 |
| Volume, OI, spread | varies |
| EMA, delta, gamma, theta, IV | varies |
| Strategy learner multiplier | scales final score |

4. **Pick highest confidence** signal
5. **Minimum threshold:** confidence ≥ **76** (`router.min_confidence`)
6. **Strategy learner** may demote setups with recent losses (`reports/strategy_learner.json`)

Output: one `CandidateSignal` or `NO_TRADE` with reason logged.

---

## 7. Entry filters (validator + risk)

### Entry confirmation (`entry_confirmation`)

- Default: signal must appear on **2 consecutive scans** (~20s apart)
- Prevents one-tick false signals
- Exempt: `gap_and_go`, `opening_range_breakout` in first 15 min; confidence ≥ 90

### Rule validator (`validator/engine.py`)

| Check | Purpose |
|-------|---------|
| **Entry window** | 09:30 – 15:00 IST only |
| **Time-of-day phases** | Stricter at open (conf ≥ 82, MTF ≥ 50) and late session (14:30–15:00) |
| **Expiry day** | Min confidence 82 on weekly expiry |
| **Trap avoidance** | Spot must align with VWAP for trend setups; block wrong-side entries |
| **MTF score** | Multi-timeframe pattern score ≥ 45 (higher at open/late) |
| **Level proximity** | Don't buy CE into resistance / PE into support |
| **Liquidity** | Spread ≤ 1.5%, volume ≥ 5000, OI ≥ 25000 (when broker sends data) |
| **Option chart confirm** | Optional CE/PE 1m structure check (when option M1 cache exists) |
| **Cooldown** | 2 min global cooldown after any exit; per-token cooldown |
| **Duplicate** | No second entry on same token |

Failed validation → logged to `validation_results` + `entry_skipped` → **no order**.

### Risk engine (`risk/engine.py`)

| Check | Default |
|-------|---------|
| Starting capital | ₹50,000 pooled (`account_capital_inr`) |
| Deploy per trade | 30–90% of available based on confidence |
| Max deployed | 90% of equity |
| Max concurrent | 2 positions |
| Max trades/day | Unlimited (configurable) |
| Max daily loss | Disabled (configurable) |
| Greeks multiplier | Scales size by delta/gamma/IV quality |

---

## 8. Strike and lot sizing

File: `contract_selector/strike_picker.py`

1. **Prefer ATM** strike for the signal side (CE or PE)
2. If ATM fails liquidity/Greeks → try **ATM ± 1 or ± 2**
3. Pick strike with best delta (target ~0.50), OI, volume, spread
4. **Lots** = `fit_lots_to_capital(confidence, premium, available capital)`
   - NIFTY lot size: **65**
   - Higher confidence → larger % of capital deployed

Example at confidence 85, premium ₹120, ₹50k available:
- Deploy ~60% → ₹30,000 budget
- Lots = floor(30000 / (120 × 65)) = **3 lots**

---

## 9. How exits work

Exits run on **every option quote** (~2s), not on the 10s scan.

File: `position/exit_rules.py` · Config: `config/position_exit_config.yaml`

### Exit priority (first match wins)

| # | Rule | Trigger |
|---|------|---------|
| 1 | **Force exit** | 15:10 IST — close everything |
| 2 | **Max loss INR** | If configured (currently disabled) |
| 3 | **Adverse momentum** | Premium down ~4–5% from entry (hard stop, bypasses min hold) |
| 4 | **Min hold** | Soft exits blocked for first 15 seconds |
| 5 | **Time stop** | 25 min in trade with no profit |
| 6 | **Early invalidation** | Never went +2% green → cut at ~5% loss |
| 7 | **Momentum trail** | Once +2% green → SL at breakeven; above +4% MFE → trail peak − 2% |
| 8 | **Trend reversal** | Spot crosses VWAP against position (only if underwater, after 3 min) |

### Exit confirmation

Soft exits (trail, time stop) need **2 consecutive ticks** confirming before SELL order.

### After exit

- Capital released
- **2-minute global cooldown** before next entry
- `StrategyLearner` records win/loss for setup demotion
- P&L written to `closed_trades`

---

## 10. Worked example: one trade from scan to exit

**Scenario:** Wednesday 10:45 IST, NIFTY trending down below VWAP.

### Scan 1 (10:45:00)

1. Candles refreshed. Spot 24,650, VWAP 24,680.
2. `bias_5m = BEARISH`, regime = `trending_down`, `trade_allowed = true`.
3. `vwap_pullback` setup = bear, trigger = `vwap_pullback_bounce_down`.
4. `VwapPullbackScanner` → signal: **PE**, NIFTY11AUG26P24650.
5. Quality gate → confidence **82**.
6. Router picks this (best of all scanners).
7. Entry confirmation → **wait** (scan 1 of 2).

### Scan 2 (10:45:10)

1. Same setup still valid → confirmation **passes**.
2. Validator checks:
   - In entry window ✓
   - MTF score 48 ✓
   - PE not into support ✓
   - Spread 0.8% ✓
3. Risk: 4 lots at premium ₹95 → deploy ₹24,700.
4. **BUY 4 lots PE** @ ₹95.25 fill.

### Monitoring (10:45 – 11:02)

- Every ~2s: option quote updates MFE/MAE.
- 10:52: Premium ₹98 (+3%) → breakeven trail armed.
- 10:58: Premium ₹102 (+7% MFE) → trail locks at peak − 2%.
- 11:02: Premium pulls to ₹99 → **momentum_trail** exit fires.

### Close

- **SELL 4 lots** @ ₹99.10
- P&L = (99.10 − 95.25) × 65 × 4 = **+₹1,001**
- Cooldown 2 min starts. Trade logged to `closed_trades`.

---

## 11. Why most scans become NO_TRADE

Typical NIFTY session funnel:

```
~2,000 scan ticks
  → ~1,800  no_strategy_produced_signal (no pattern on spot)
  → ~150    regime_blocks_trade (sideways / high risk / late)
  → ~40     confidence < 76
  → ~20     validator blocked (MTF, trap, TOD, spread)
  → ~5      entry_confirm waiting
  → ~2–10   actual entries (varies by day/volatility)
```

Common validator rejection reasons:

- `mtf_score_too_low` — multi-timeframe patterns don't align
- `tod_open_setup_not_allowed` — wrong setup for first 45 min
- `ce_below_vwap_trap` / `pe_above_vwap_trap` — spot on wrong side of VWAP
- `ce_into_resistance@24650` — buying CE near resistance level
- `cooldown_active` — just exited a trade
- `spread_wide` / `volume_low` — illiquid option

---

## 12. Spot vs option charts (important)

| Question | Answer |
|----------|--------|
| What drives **entry** signal? | **NIFTY spot** 1m/3m/5m candles + VWAP |
| What drives **exit** P&L? | **Option premium** (live CE/PE quote) |
| Are CE/PE charts used for setup? | **Partially** — option chart confirm exists but often skipped live (no option M1 cache unless band refresh ran) |
| What are options used for? | Strike pick, spread/OI filter, Greeks sizing, premium P&L |

This is why backtest and live can diverge: backtest uses 1m snap interpolation for option premium; live uses real tick timing and broker fills.

---

## 13. Config cheat sheet

| Setting | File | Default | Effect |
|---------|------|---------|--------|
| `scan_interval_seconds` | `runtime_config.yaml` | 10 | How often to scan for entries |
| `router.min_confidence` | `strategy_config.yaml` | 76 | Minimum score to trade |
| `enabled_strategies` | `strategy_config.yaml` | 15 strategies | Which patterns can fire |
| `entry_start_time` | `validator_config.yaml` | 09:30 | No entries before this |
| `entry_end_time` | `validator_config.yaml` | 15:00 | No new entries after this |
| `force_exit_time` | `risk_config.yaml` | 15:10 | Close all positions |
| `account_capital_inr` | `risk_config.yaml` | 50000 | Paper capital pool |
| `cooldown_after_exit_minutes` | `risk_config.yaml` | 2 | Wait after exit |
| `adverse_move_pct_from_entry` | `position_exit_config.yaml` | 5% | Hard stop on premium |
| `trail_step_arm_mfe_pct` | `position_exit_config.yaml` | 4% | When trailing starts |

---

## 14. File map

| Area | Path |
|------|------|
| Main loop | `trading-engine/src/algoflat/main.py` |
| Orchestrator (scan loop) | `trading-engine/src/algoflat/trading/orchestrator.py` |
| Features + setups | `trading-engine/src/algoflat/features/engine.py`, `features/setups.py` |
| Regime | `trading-engine/src/algoflat/regime/classifier.py` |
| Scanners | `trading-engine/src/algoflat/scanner/library.py` |
| Router | `trading-engine/src/algoflat/strategy/router.py` |
| Quality gate | `trading-engine/src/algoflat/quality/gate.py` |
| Validator | `trading-engine/src/algoflat/validator/engine.py` |
| Risk / sizing | `trading-engine/src/algoflat/risk/engine.py` |
| Execution | `trading-engine/src/algoflat/execution/engine.py` |
| Exits | `trading-engine/src/algoflat/position/exit_rules.py` |
| Positions | `trading-engine/src/algoflat/position/manager.py` |
| Backtest replay | `scripts/day_backtest.py` |

---

## Quick reference: one scan in 12 lines

```
1.  Refresh spot candles (1m/3m/5m) — abort if stale
2.  Compute VWAP, 5m bias, all setup flags, level map, MTF score
3.  Classify regime — skip if not trade_allowed
4.  For each enabled strategy → CandidateSignal if setup flag true
5.  Score each signal (quality gate) → confidence 0–100
6.  Router → pick best signal if confidence ≥ 76
7.  Entry confirmation → wait 2 scans (unless exempt)
8.  Validator → spread, MTF, traps, time-of-day, cooldown
9.  Risk → compute lots from confidence + capital + Greeks
10. Execution → BUY CE or PE at market
11. On every option quote → update MFE, check exit rules
12. SELL when trail/stop/time/15:10 fires → log P&L, start cooldown
```

---

_Last updated: August 2026 — aligned with `strategy_router_v2.9.1_option_chart_mtf` and `validator_v1.8.1_option_chart_mtf`._
