# Ghost Protocol — The Ultimate Build (Real-Money Vision)

Status: vision / blueprint. NOT financial advice. Trading real money can lose
100% of capital. Nothing here guarantees profit or "accuracy."

## 0. The one hard truth
Accuracy (win rate) is the wrong north star. A system can be 80% accurate and go
broke, or 40% accurate and compound for years. What makes money and survives:
positive expectancy after real costs, position sizing, not blowing up, and edge
proven out-of-sample AND forward.

Reframed objective: maximize risk-adjusted expectancy per trade, after costs,
that survives regime change — and trade only the highest-conviction subset.

Honest current state from the audit: live win rate ~30-33%, PnL win rate 25%,
profit factor 0.59, kill conditions red. Today there is no proven edge. The
ultimate build is the machine that finds, proves, and safely exploits an edge.

## 1. Accuracy levers (highest impact first)
1. Meta-labeling: a second model decides act/skip/size on top of the direction
   model. Biggest real-world precision booster; kills false positives.
2. Triple-barrier labels: take-profit / stop-loss / time barrier.
3. Trade selectivity over coverage: fire only when calibrated P(win) clears a
   high floor AND regime aligns AND post-cost expectancy > 0.
4. Cross-sectional ranking (long top decile / short bottom) over single-name
   absolute direction.
5. Calibration as a first-class output (conformal + Brier/reliability).
6. Regime gating: trend model stands down in chop.

## 2. Architecture
Data -> Features -> Labels -> Models -> Meta-label -> Risk/Gates -> Execution
-> Portfolio -> Monitor/Kill -> Governance. Each layer independently testable.
Point-in-time correct, survivorship-bias-free, leak-free, versioned schemas.

## 3. Anti-overfitting
Purged + embargoed walk-forward CV, deflated Sharpe, Probability of Backtest
Overfitting (PBO), brutal cost model (slippage/spread/fees/borrow), minimum
sample gates.

## 4. Risk guardrails (non-negotiable)
Per-trade risk 0.5-1% of equity with predefined stop; daily loss limit auto-halt;
max drawdown global halt with manual re-arm; size = min(vol_target,
fractional_Kelly, max_cap); no averaging down; time stops; idempotent orders +
broker reconciliation; an ARMED flag separate from deploy.

## 5. Staged path to real money (gates, not vibes)
- Phase 0: fix foundation (telegram_hunter alert path, register POST
  /api/wolf/war-room, make kill auto_pause actually pause, restore price-feed
  breakers, green the release gates). No money until this passes.
- Phase 1: rebuild signal quality (triple-barrier + meta-labeling + conformal +
  regime + cross-sectional option).
- Phase 2: rigorous validation (purged walk-forward, deflated Sharpe, PBO, costs).
- Phase 3: forward paper trading for a real sample (>= 3 months / 100+ trades).
- Phase 4: go-live gate on FORWARD paper after costs: profit factor > ~1.3,
  positive expectancy, drawdown < 10-15%, stable calibration, small
  live-vs-paper drift, minimum trade count.
- Phase 5: first real dollars at micro size, hard daily loss limit, verified kill.
- Phase 6: scale via fractional Kelly only as the live record confirms edge.

## 6. Realistic expectations
Liquid names are near-efficient; daily direction guessing is noisy. Durable edges
come from structure (events, cross-sectional relative strength, regime-conditioned
mean reversion, vol/options). Real edges are often small; compounding comes from
sizing, cost control, and survival.

## 7. First five concrete steps
1. Phase 0 foundation fixes.
2. Triple-barrier + meta-labeling behind a flag; A/B vs current labels.
3. Add purged walk-forward + deflated Sharpe + PBO to the training gate.
4. Forward-paper ledger with the Phase-4 go-live gate encoded as code.
5. ARMED live-trading guard + broker reconciliation before any real order.

Disclaimer: educational/engineering plan only. Not financial advice. You can lose
all your money trading. No system can guarantee accuracy or profit.
