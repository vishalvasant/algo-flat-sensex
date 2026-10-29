# AutoTrade Engine Enhancement Proposal (v2)

This proposal enhances the current trading engine by introducing
state-based entries, adaptive strategy selection, premium behaviour
analysis, expiry-aware logic, and opportunity scoring.

## Key Changes

-   Regime describes market environment, **not** CE/PE direction.
-   Bullish markets may still generate valid PE reversals.
-   Bearish markets may still generate valid CE reversals.
-   Sideways markets should only trade after premium confirmation.
-   Expiry day should run its own dedicated regime.

## Regime Flow

Market Data → Regime Detection → Eligible Strategy Pool → State
Transition (Detect → Observe → Confirm → Execute) → Hard Validation
Gates → Opportunity Score → Confidence → Risk Engine → Execute →
Adaptive Learning

## Required New Modules

1.  State Transition Engine
2.  Premium Behaviour Engine
3.  Counter-Trend Opportunity Engine
4.  Sideways Opportunity Engine
5.  Expiry Mode Engine
6.  Session Learning Engine
7.  Opportunity Score Engine

## Hard Validation Gates

-   Liquidity
-   Spread
-   OI
-   Volume
-   Premium responsiveness
-   Structure validation
-   Risk availability

Fail any gate =\> NO TRADE.

## Counter Trend Rules

Allow CE in bearish markets or PE in bullish markets only after: -
exhaustion - failed breakout - liquidity sweep - divergence - premium
confirmation

## Sideways Rules

Trade only: - range rejection - confirmed breakout - premium expansion

Otherwise NO TRADE.

## Expiry Rules

Evaluate: - premium decay - gamma behaviour - strike migration - OI
flip - premium responsiveness

Never rely only on index direction.

## Adaptive Learning

Monitor today's strategy performance. Pause strategies with persistent
poor performance. Promote strategies with superior win rate.

## Expected Outcome

-   Higher trade quality
-   Fewer false entries
-   Better CE/PE selection
-   Expiry-aware behaviour
-   Lower overtrading
-   Adaptive decision making
