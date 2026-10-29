# Algo-Flat vs NIFTY Weekly Options Scenario Test Suite — Verification Report

**Source spec:** `AutoTrade_NIFTY50_Weekly_Options_Market_Scenario_Test_Suite.md` (70 scenarios)  
**Target system:** algo-flat (`/algo-flat`) — live engine + `scripts/day_backtest.py`  
**Purpose:** Map each scenario to what algo-flat **actually has in code/config today**, so you can verify behaviour one by one.

**How to use this doc**

1. For each scenario row, run the **How to verify** step (backtest day, paper session, or unit test).
2. Fill in **Your result** columns after observation.
3. Status legend:
   - **COVERED** — Dedicated strategy or gate exists; expected behaviour is designed in.
   - **PARTIAL** — Related logic exists but not a full match to the scenario.
   - **GAP** — Missing, disabled, or too weak for the scenario intent.
   - **N/A** — Out of scope or not applicable to current design.

**Important caveats (read before verifying)**

| Topic | Algo-flat reality |
|-------|-------------------|
| Entry driver | **NIFTY spot** 1m/3m/5m + VWAP (not CE/PE chart setups for entries) |
| Option role | Strike, premium P&L, spread/OI/Greeks filters, some premium checks |
| `liquidity_sweep` | Detected in features but **not** in `enabled_strategies` |
| `option_entry_veto` | **Disabled** in config — premium veto layer often skipped live |
| Option chart confirm | Exists but live often has **no option M1 cache** → skipped in live-parity |
| Spot vs futures | FUT-first bias; cash/futures divergence guard in `main.py` |
| Sideways | `block_sideways: false` — sideways can still trade (penalty only) |
| Risk brakes | Code supports daily loss / consecutive loss caps — **check your deployed `risk_config.yaml`** |

**Config references:** `config/strategy_config.yaml`, `config/validator_config.yaml`, `config/risk_config.yaml`, `config/position_exit_config.yaml`

---

## Summary scorecard (pre-verification)

| Category | Scenarios | COVERED | PARTIAL | GAP |
|----------|-----------|---------|---------|-----|
| A Strong Bullish | 01–05 | 3 | 2 | 0 |
| B Strong Bearish | 06–09 | 3 | 2 | 0 |
| C Bull + PE | 10–11 | 1 | 2 | 0 |
| D Bear + CE | 12–13 | 1 | 2 | 0 |
| E Sideways | 14–18 | 1 | 3 | 1 |
| F False Breakouts | 19–21 | 1 | 2 | 0 |
| G VWAP | 22–24 | 2 | 1 | 0 |
| H Momentum | 25–27 | 2 | 1 | 0 |
| I Premium behaviour | 28–32 | 2 | 3 | 0 |
| J OI + Price | 33–36 | 0 | 4 | 0 |
| K Expiry day | 37–43 | 2 | 4 | 1 |
| L Regime change | 44–47 | 1 | 3 | 0 |
| M Extreme / data | 48–52 | 2 | 2 | 1 |
| N Risk controls | 53–55 | 1 | 2 | 0 |
| O Exits | 56–60 | 3 | 2 | 0 |
| P Combined | 61–70 | 1 | 7 | 2 |
| **Total** | **70** | **24** | **40** | **6** |

*Counts reflect **design intent in codebase**, not backtest pass rate. You must still run each scenario to confirm.*

---

## Verification record template (copy per scenario)

```text
Scenario ID:
Date tested:
Data source: [ ] live paper  [ ] day_backtest snap  [ ] unit test
Regime detected:
Strategy selected:
Direction: CE / PE / NO TRADE
Confidence:
Entry time / premium:
Exit time / reason:
P&L:
Expected behaviour met? Y / N / Partial
Notes:
```

---

# A. Strong Bullish Markets

| ID | Scenario | Status | Algo-flat mechanism | How to verify |
|----|----------|--------|---------------------|---------------|
| 01 | Clean bullish trend | **COVERED** | `vwap_trend`, `momentum_continuation`, `delta_momentum`; 5m bias BULLISH; regime `trending_up` | Backtest a strong up day (e.g. snap with HH/HL). Expect CE via trend/momentum. |
| 02 | Bullish trend + pullback | **COVERED** | `vwap_pullback` — extension + pullback to VWAP + 1m bounce trigger; `entry_confirmation` 2 scans | Find day with pullback to VWAP in uptrend; check Decision Logs for `vwap_pullback` CE after 2 scans. |
| 03 | Bullish breakout | **PARTIAL** | `opening_range_breakout`, `pdh_pdl_break`, `oi_breakout`; level break needs 5m `breakout_confirmed` | ORB/PDH break days; volume/OI not as rich as spec implies. |
| 04 | Breakout failure | **PARTIAL** | `early_invalidation` (~5% loss if never +2% green); `adverse_momentum` hard stop; not resistance-specific | Enter ORB CE on fake break day; check exit `early_invalidation` or `adverse_momentum`. |
| 05 | Bullish index + weak CE | **PARTIAL** | `premium_responsive` (expiry/sideways setups only); `option_chart_confirm` spot/premium divergence — **often skipped live**; `option_entry_veto` **disabled** | **GAP risk on trend days:** weak CE may still pass if only spot-led gates fire. Verify on live paper. |

**Your results:** 01 [ ] 02 [ ] 03 [ ] 04 [ ] 05 [ ]

---

# B. Strong Bearish Markets

| ID | Scenario | Status | Algo-flat mechanism | How to verify |
|----|----------|--------|---------------------|---------------|
| 06 | Clean bearish trend | **COVERED** | `vwap_trend`, `momentum_continuation`, `delta_momentum`; bias BEARISH | Bear trend snap day → expect PE. |
| 07 | Bearish pullback | **COVERED** | `vwap_pullback` bear + VWAP rejection path; 2-scan confirm | Downtrend with rally to VWAP → PE after rejection trigger. |
| 08 | Bearish breakdown | **PARTIAL** | `pdh_pdl_break` (PDL), ORB low break, `oi_breakout` | Support breakdown day; same 5m confirm limits as 03. |
| 09 | Breakdown failure | **PARTIAL** | Same exit stack as 04 | Fake breakdown → PE entry → check early exit reasons. |

**Your results:** 06 [ ] 07 [ ] 08 [ ] 09 [ ]

---

# C. Bullish Market but PE Opportunity

| ID | Scenario | Status | Algo-flat mechanism | How to verify |
|----|----------|--------|---------------------|---------------|
| 10 | Bull trap / bearish reversal | **PARTIAL** | `peak_reversal_fade`, `level_rejection`, `vwap_rejection` — **counter-bias** (PE in bull); needs extension + reversal signals | Extended bull day with fade at high; expect PE from fade/rejection setups. |
| 11 | Bullish liquidity sweep | **PARTIAL** | `detect_liquidity_sweep_at_level` in features; **`liquidity_sweep` not enabled**; nearest: `level_rejection`, `peak_reversal_fade` | Sweep at PDH/OR high; check if level_rejection fires PE. |

**Your results:** 10 [ ] 11 [ ]

---

# D. Bearish Market but CE Opportunity

| ID | Scenario | Status | Algo-flat mechanism | How to verify |
|----|----------|--------|---------------------|---------------|
| 12 | Bear trap / bull reversal | **PARTIAL** | `peak_reversal_fade`, `level_rejection`, `vwap_rejection` — CE counter-bias | Bear day sweep of lows + reclaim → CE from fade setup. |
| 13 | Short-covering reversal | **GAP** | No explicit "short covering" model; OI down + price up only nudges **quality score**, not a dedicated reversal strategy | Would need OI velocity + structure rule; today **not first-class**. |

**Your results:** 12 [ ] 13 [ ]

---

# E. Sideways Markets

| ID | Scenario | Status | Algo-flat mechanism | How to verify |
|----|----------|--------|---------------------|---------------|
| 14 | Dead sideways | **PARTIAL** | Regime `sideways` + high `risk_score` can block; but `block_sideways: false` — **may still trade** allowed sideways strategies | Low ATR range day → expect mostly NO_TRADE; **verify it doesn't overtrade**. |
| 15 | Sideways + CE premium expansion | **PARTIAL** | `delta_momentum`, `oi_breakout` allowed in sideways; premium expansion alone **not** a setup flag | Sideways day with CE OI build; check if `oi_breakout` or `delta_momentum` fires. |
| 16 | Sideways + PE premium expansion | **PARTIAL** | Same as 15 for PE | Same on PE side. |
| 17 | Range high rejection | **COVERED** | `level_rejection`, `vwap_rejection`, `peak_reversal_fade` at levels | Range day fade at top → PE. |
| 18 | Range low rejection | **COVERED** | Same at range low → CE | Range day bounce at bottom → CE. |

**Your results:** 14 [ ] 15 [ ] 16 [ ] 17 [ ] 18 [ ]

---

# F. False Breakouts

| ID | Scenario | Status | Algo-flat mechanism | How to verify |
|----|----------|--------|---------------------|---------------|
| 19 | Fake upside breakout | **PARTIAL** | `entry_confirmation` 2 scans; validator MTF/level traps; exits `early_invalidation` / `adverse_momentum` | Fake ORB up → may enter then exit fast; check if entry should have been blocked. |
| 20 | Fake downside breakout | **PARTIAL** | Same for PE | Fake ORB down. |
| 21 | Double fake breakout | **PARTIAL** | `cooldown_after_exit` 2 min; `strategy_learner` session pause after losses; **no explicit "double fake" state** | Choppy range day → count trades; check overtrading vs spec. |

**Your results:** 19 [ ] 20 [ ] 21 [ ]

---

# G. VWAP

| ID | Scenario | Status | Algo-flat mechanism | How to verify |
|----|----------|--------|---------------------|---------------|
| 22 | Clean VWAP reclaim | **COVERED** | `vwap_reclaim` — 3m setup + 1m cross trigger; dedicated scanner | Classic reclaim day → CE after cross above VWAP. |
| 23 | False VWAP reclaim | **PARTIAL** | 2-scan confirm helps; false reclaim may still enter once → `vwap_rejection` or fade on failure | Whipsaw VWAP → check CE then PE on rejection. |
| 24 | VWAP chop | **PARTIAL** | Cooldown + confirm reduce repeats; **no chop detector** | Repeated VWAP crosses → count entries; expect some overtrading risk. |

**Your results:** 22 [ ] 23 [ ] 24 [ ]

---

# H. Momentum

| ID | Scenario | Status | Algo-flat mechanism | How to verify |
|----|----------|--------|---------------------|---------------|
| 25 | Strong momentum continuation | **COVERED** | `momentum_continuation` — 4 consecutive 1m closes in bias direction beyond VWAP | Strong trend bar sequence → momentum CE/PE. |
| 26 | Momentum exhaustion | **PARTIAL** | `peak_reversal_fade` at extension; no explicit "divergence exhaustion" on trend setups | Late extension → fade PE/CE; trend setups may still chase (**live loss risk**). |
| 27 | Late momentum entry | **PARTIAL** | Quality gate distance/extension; `min_vwap_extension` on pullbacks; **no "move already done" hard block** | End-of-move signal → check if entry still fires (undesired). |

**Your results:** 25 [ ] 26 [ ] 27 [ ]

---

# I. Option Premium Behaviour

| ID | Scenario | Status | Algo-flat mechanism | How to verify |
|----|----------|--------|---------------------|---------------|
| 28 | Bullish futures + CE responds | **COVERED** | Normal path — CE aligned with bias; quality gate rewards OI/volume | Trend up + CE expanding → CE entry expected. |
| 29 | Bullish futures + CE flat | **PARTIAL** | `option_chart_confirm.block_spot_premium_divergence` — **if option M1 available**; `premium_responsive` only on select setups; veto **disabled** | **Critical verify:** spot up, CE flat → should NO TRADE or fail validator. |
| 30 | Bearish futures + PE responds | **COVERED** | Mirror of 28 for PE | Trend down + PE up → PE entry. |
| 31 | Bearish futures + PE flat | **PARTIAL** | Same gap as 29 for PE | Spot down, PE flat → should block. |
| 32 | Premium divergence | **PARTIAL** | `option_chart_confirm` divergence check; `premium_responsive` tracker | Run `test_option_chart_confirm.py`; live may skip without option M1. |

**Your results:** 28 [ ] 29 [ ] 30 [ ] 31 [ ] 32 [ ]

---

# J. OI + Price

| ID | Scenario | Status | Algo-flat mechanism | How to verify |
|----|----------|--------|---------------------|---------------|
| 33 | Price up + OI up | **PARTIAL** | `chain_intel` → `long_build_up`, `oi_confirms_ce`; boosts quality + `oi_breakout` setup | Not full OI semantics — PCR band heuristics only. |
| 34 | Price up + OI down | **PARTIAL** | `short_covering` flag in chain; **no dedicated short-cover strategy** | Score nudge only. |
| 35 | Price down + OI up | **PARTIAL** | `short_build` / put buildup flags | Heuristic via PCR OI delta. |
| 36 | Price down + OI down | **PARTIAL** | `unwind` flag in chain_intel | Unwind logged; not a standalone entry rule. |

**Requirement from spec:** OI must combine with price + volume + structure — **PARTIAL**: structure yes, OI is simplified PCR-based, not full chain analytics.

**Your results:** 33 [ ] 34 [ ] 35 [ ] 36 [ ]

---

# K. Weekly Expiry-Day Scenarios

| ID | Scenario | Status | Algo-flat mechanism | How to verify |
|----|----------|--------|---------------------|---------------|
| 37 | Expiry strong trend | **COVERED** | `expiry_scalping`, trend setups; `expiry_day_sizing` cap; regime `expiry_behaviour` | Tuesday expiry trend day backtest. |
| 38 | Expiry sideways decay | **PARTIAL** | `expiry_mode` restricts to fade/rejection/scalp; `premium_responsive` on expiry setups; **may still scalp** | Sideways expiry → expect few trades; verify no churn. |
| 39 | Expiry sudden CE expansion | **PARTIAL** | Breakout + confirm; expiry min confidence 82 | Mid-expiry breakout CE. |
| 40 | Expiry sudden PE expansion | **PARTIAL** | Same for PE | Mid-expiry breakdown PE. |
| 41 | Expiry fake breakout | **PARTIAL** | Same false-breakout exits as 19 | Expiry spike then collapse. |
| 42 | Expiry rapid ATM migration | **COVERED** | `retarget_atm` each scan; strike picker re-resolves | Large move on expiry → tsym/strike should roll ATM. |
| 43 | Premium spike without index move | **GAP** | No dedicated "IV spike without spot" filter; partial via spread/premium_responsive on expiry setups | Theta/IV spike trap — **weak coverage**. |

**Your results:** 37 [ ] 38 [ ] 39 [ ] 40 [ ] 41 [ ] 42 [ ] 43 [ ]

---

# L. Regime Changes

| ID | Scenario | Status | Algo-flat mechanism | How to verify |
|----|----------|--------|---------------------|---------------|
| 44 | Bull → Bear | **PARTIAL** | Regime reclassified each scan; new PE setups can fire; `flip_on_trend_reversal` for exits (flip entry optional) | Afternoon reversal day; CE exits then PE signals. |
| 45 | Bear → Bull | **PARTIAL** | Mirror of 44 | Morning bear → afternoon bull. |
| 46 | Trend → Sideways | **PARTIAL** | Regime → sideways; penalty −18 confidence; **not hard block** | Trend stops → verify trend strategies demoted/stop firing. |
| 47 | Sideways → Trend | **PARTIAL** | Regime shift + ORB/breakout allowlist | Compression breakout day. |

**Your results:** 44 [ ] 45 [ ] 46 [ ] 47 [ ]

---

# M. Extreme / Dangerous Conditions

| ID | Scenario | Status | Algo-flat mechanism | How to verify |
|----|----------|--------|---------------------|---------------|
| 48 | Very high volatility | **PARTIAL** | Regime `high_volatility`; `high_vol_atr_mult`; wider adverse stop variant; sizing caps | Event day; check lot size and SL %. |
| 49 | Very low volatility | **PARTIAL** | Regime `low_volatility`; often sideways → fewer signals | Ultra-quiet day → mostly NO_TRADE. |
| 50 | Sudden vol spike | **PARTIAL** | 2-scan confirm; no vol spike cooldown | Spike candle → verify no chase on first tick. |
| 51 | Data anomaly | **COVERED** | `candles_stale` → NO_TRADE; LTP jump filter 15%; spread/liquidity validator; missing fields rejected | Kill feed / wide spread → no entry. |
| 52 | Liquidity disappears | **PARTIAL** | `max_spread_pct` 1.5%; min volume/OI when present; **no depth-based gate** | Wide spread snapshot → validator fail. |

**Your results:** 48 [ ] 49 [ ] 50 [ ] 51 [ ] 52 [ ]

---

# N. Consecutive Loss / Risk Controls

| ID | Scenario | Status | Algo-flat mechanism | How to verify |
|----|----------|--------|---------------------|---------------|
| 53 | Three consecutive losses | **PARTIAL** | `max_consecutive_losses` in risk_config (**check deployed value**); local dev may have `3`, server default was `0` | Paper session with forced losses → entries blocked after N. |
| 54 | One strategy loses repeatedly | **COVERED** | `strategy_learner` demote + `pause_after_session_losses: 2` pauses setup for session | Same setup 5 losses → setup skipped in router. |
| 55 | Rapid loss cluster | **PARTIAL** | Global cooldown 2 min after each exit; daily loss cap if enabled | 3 losses in 15 min → check daily loss + consecutive caps. |

**Your results:** 53 [ ] 54 [ ] 55 [ ]

---

# O. Exit Scenarios

| ID | Scenario | Status | Algo-flat mechanism | How to verify |
|----|----------|--------|---------------------|---------------|
| 56 | Immediate +10% / +20% | **COVERED** | `momentum_trail` — breakeven at +2% MFE; step trail arm +4% MFE, lock peak−2% | Fast winner → trail exit reason. |
| 57 | Slow +3% → +8% | **COVERED** | Trail holds while MFE builds; `min_hold` 15s before soft exit | Gradual winner → shouldn't exit at +3% unless adverse. |
| 58 | Adverse then recovery | **PARTIAL** | `adverse_momentum` ~4–5% may exit before recovery; `weak_entry_adverse_pct` 2.5% | −7% dip then +15% → check if stopped out too early. |
| 59 | Profit then reversal | **COVERED** | Momentum trail locks profit after +4% MFE | +30% then fade → trail exit with profit. |
| 60 | Near target reversal | **PARTIAL** | No fixed % target — trail only (peak−2%); no "28% protect" rule | Large winner reversal → trail should capture most. |

**Your results:** 56 [ ] 57 [ ] 58 [ ] 59 [ ] 60 [ ]

---

# P. Combined Realistic Scenarios

| ID | Scenario | Status | Algo-flat mechanism | How to verify |
|----|----------|--------|---------------------|---------------|
| 61 | Bull + VWAP pullback + CE weak | **PARTIAL** | Pullback may signal CE; premium divergence **should** block if chart confirm active | **Key live gap** — verify 29/32 on same day. |
| 62 | Bull + failed breakout + PE | **PARTIAL** | `peak_reversal_fade` / `level_rejection` after failed high | Failed breakout afternoon PE. |
| 63 | Bear + support sweep + CE | **PARTIAL** | Counter-bias fade at PDL / level rejection | Sweep low + CE reclaim. |
| 64 | Sideways + VWAP chop + decay | **PARTIAL** | Should be mostly NO_TRADE; sideways not hard-blocked | Full chop day → trade count should be low. |
| 65 | Compression → breakout | **PARTIAL** | ORB / PDH break after range; session_matrix `sideways_range` → ORB allowlist | Squeeze then break day. |
| 66 | Expiry sideways → reversal | **PARTIAL** | Expiry fade/scalp + level rejection | Expiry afternoon reversal. |
| 67 | Expiry trend → reversal | **PARTIAL** | Trail exit + fade setup on opposite side | Expiry trend exhaustion. |
| 68 | Fake breakout → real breakout | **PARTIAL** | Cooldown then fresh ORB signal allowed | Two-break day; second should be allowed. |
| 69 | Bull spot / bear futures divergence | **PARTIAL** | `_spots_diverge` guard in `main.py` (~1.5%); FUT-first bias | Rare; verify CE not fired on divergence. |
| 70 | Bear spot / bull futures divergence | **PARTIAL** | Same guard | Mirror; PE/CE selection sanity. |

**Your results:** 61 [ ] 62 [ ] 63 [ ] 64 [ ] 65 [ ] 66 [ ] 67 [ ] 68 [ ] 69 [ ] 70 [ ]

---

# Gap list — scenarios needing work before "full pass"

| Priority | Scenario IDs | Gap | Suggested layer |
|----------|--------------|-----|-----------------|
| P0 | 05, 29, 31, 61 | Weak CE/PE when premium doesn't follow spot | Enable `option_entry_veto` or enforce `option_chart_confirm` live with option M1 refresh |
| P0 | 14, 64 | Dead sideways still trades | Set `block_sideways: true` or tighten sideways allowlist |
| P1 | 11, 17, 18 | Liquidity sweep not a first-class strategy | Enable `liquidity_sweep` in router or merge into `level_rejection` |
| P1 | 13, 34 | Short-covering not modeled | OI velocity + reversal setup in `chain_intel` |
| P1 | 33–36 | OI semantics simplified | Richer OI change per strike, not PCR-only |
| P2 | 43 | IV spike without spot move | IV change gate on expiry |
| P2 | 21, 24 | Overtrading in chop | Chop detector or raise confirm to 3 scans in sideways |
| P2 | 53, 55 | Risk caps deployment-dependent | Ensure `max_daily_loss` + `max_consecutive_losses` on production |

---

# How to run verification (commands)

### 1. Single-day backtest (honest — realistic)

```bash
cd algo-flat
CONFIG_DIR=./config PYTHONPATH=trading-engine/src:./scripts \
  trading-engine/.venv/bin/python scripts/day_backtest.py \
  --date YYYY-MM-DD --underlying NIFTY --from-snap --capital 50000
```

### 2. Week backtest

```bash
... scripts/day_backtest.py --week --week-start 2026-08-04 --from-snap --capital 50000
```

### 3. Live paper observation

- Dashboard → Decision Logs (`strategy_decision`, `entry_skipped`)
- Trade Blotter → closed trades + exit reasons
- Compare to scenario expected direction

### 4. Unit tests for specific gates

```bash
cd algo-flat/trading-engine
../.venv/bin/python -m pytest tests/unit/test_option_chart_confirm.py -q
../.venv/bin/python -m pytest tests/unit/test_levels.py -q
../.venv/bin/python -m pytest tests/unit/test_strategy_router.py -q
```

---

# Strategy → scenario quick map

| Strategy (enabled) | Best matches scenarios |
|--------------------|------------------------|
| `vwap_reclaim` | 22, 23, 44, 45 |
| `vwap_pullback` | 02, 07, 61 |
| `vwap_trend` | 01, 06, 25 |
| `vwap_rejection` | 17, 18, 23 |
| `momentum_continuation` | 01, 06, 25, 27 (risk) |
| `opening_range_breakout` | 03, 08, 19, 20, 65 |
| `gap_and_go` / `gap_fade` | Open-gap variants |
| `pdh_pdl_break` | 03, 08 |
| `peak_reversal_fade` | 10, 12, 26, 62, 66, 67 |
| `level_rejection` | 11, 17, 18, 63 |
| `oi_breakout` | 33, 15, 16 (partial) |
| `delta_momentum` | 01, 06, 28, 30 |
| `expiry_scalping` | 37–41 |
| `trend_day` | 01, 06, strong trend days |

---

# Evaluation worksheet (fill after all tests)

| Metric | Your value |
|--------|------------|
| Scenarios tested (of 70) | |
| COVERED confirmed | |
| PARTIAL confirmed | |
| GAP confirmed | |
| Win rate by regime | |
| Unnecessary trades (14, 21, 24, 64) | |
| Missed opportunities (05, 29, 31) | |
| False entries | |
| Late entries (27) | |
| Premature exits (58) | |

---

# Related docs

- [HOW_ALGO_FLAT_TRADES.md](./HOW_ALGO_FLAT_TRADES.md) — step-by-step trade flow
- [END_TO_END_TRADING_FLOW.md](./END_TO_END_TRADING_FLOW.md) — technical pipeline audit

---

_Generated for manual verification. Does not replace running the 70 scenarios — statuses are code-map estimates until you fill "Your results" checkboxes._
