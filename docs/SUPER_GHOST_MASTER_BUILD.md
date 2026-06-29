# Super Ghost — Max Build Master Map

**Status:** living blueprint. **Baseline:** PR84 (`_pr_version 84`), live on Railway.
**Machine-readable source of truth:** [`super_ghost_master_plan.json`](./super_ghost_master_plan.json)
(enforced by `tests/test_master_plan.py` — every requirement must map to a phase with a gate).

> **The one hard truth (read this first).**
> "Sit back and watch the money" is the *goal feeling*, not a promise this document can make.
> Markets are adversarial and partly random; no system guarantees profit or a fixed accuracy
> number, and anyone claiming otherwise is wrong or lying. What this build *can* honestly deliver:
> a platform that **only surfaces predictions when it has a measured, out-of-sample, after-cost
> edge**, explains them, sizes the risk, tracks every outcome, calibrates its own confidence, and
> says **NO EDGE** the rest of the time. The closest real thing to "sit back and watch" is a system
> that has *proven* its edge forward and keeps proving it — that is what every phase below drives toward.

---

## 0. What this map is

The MASTER DIRECTIVE was decomposed into **98 atomic requirements** (`R-…` IDs) spanning
architecture, models, features, data, labels, validation, continuous learning, explainability,
risk, UI, and engineering. Each requirement is traced to a **status** (`done` / `partial` /
`planned`) and a **phase** with an **acceptance gate**. Today: **12 done, 49 partial, 37 planned**.
That ratio is the honest measure of how much of "the max build" exists versus remains.

The map is intentionally **end-to-end**: data in → features → labels → models → ensemble →
calibration → risk → explanation → UI → production hardening → continuous learning loop. The end
state is a system you can leave running that keeps surfacing only validated edges.

---

## 1. North-star data flow (the whole machine on one page)

```text
        ┌─────────────────────────────────────────────────────────────────┐
        │ L1 DATA INGESTION  (market, options, fundamentals, SEC, macro,    │
        │     news, alt-data) — redundant providers, circuit-breaker gated  │
        └───────────────┬─────────────────────────────────────────────────┘
                        ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │ L2 STORAGE   Postgres (app+ledger) · ClickHouse/Timescale         │
        │   (analytics/bars) · Redis (hot cache/queues) · S3 Parquet (lake) │
        └───────────────┬─────────────────────────────────────────────────┘
                        ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │ L3 FEATURE FACTORY  point-in-time only, leakage-tested            │
        └───────────────┬─────────────────────────────────────────────────┘
                        ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │ L4 LABELS  direction · triple-barrier · return-bucket · meta      │
        │            across intraday / 1 / 3 / 5 / 10 / 20-day horizons     │
        └───────────────┬─────────────────────────────────────────────────┘
                        ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │ L5 MODEL ZOO (specialists)                                        │
        │  price-action GBM · TS-deep (TFT/PatchTST) · options-flow ·       │
        │  news/event (FinBERT+LLM) · fundamentals/earnings · regime(HMM) · │
        │  microstructure (DeepLOB) · graph (peers/sector/ETF)             │
        └───────────────┬─────────────────────────────────────────────────┘
                        ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │ L6 ENSEMBLE + META-LABELER  stack + regime-conditioned weights →  │
        │     decision: SHOW / WATCH / SKIP / NO EDGE                       │
        └───────────────┬─────────────────────────────────────────────────┘
                        ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │ L7 CALIBRATION  isotonic/Platt/conformal → honest probabilities   │
        └───────────────┬─────────────────────────────────────────────────┘
                        ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │ L8 RISK ENGINE  expected move · stop · target · R:R · P(target    │
        │     first) · max DD · position size · grade A+/A/B/C/D/F          │
        └───────────────┬─────────────────────────────────────────────────┘
                        ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │ L9 EXPLANATION  structured-data-only AI brief (never fabricates)  │
        └───────────────┬─────────────────────────────────────────────────┘
                        ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │ L10 UI  prediction · market · scanner · accuracy · if-followed ·  │
        │      research · risk · health                                     │
        └───────────────┬─────────────────────────────────────────────────┘
                        ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │ L11 CONTINUOUS LEARNING  log → resolve → calibrate → attribute →  │
        │   retrain challenger → champion/challenger gate → promote if OOS  │
        │   superior (else keep champion). Loops back into L5/L6/L7.        │
        └─────────────────────────────────────────────────────────────────┘
```

The **L11 loop closing back into the models** is what makes "sit back" meaningful: the system
improves itself from its own measured outcomes, and refuses to promote a model that is not
provably better out-of-sample.

---

## 2. Technology decisions (made for you, with the reasoning)

These are decided per the directive ("do not ask"). Rationale is summarized; full provider/method
research is in Agent 2's response and the prior `GHOST_ULTIMATE_VISION.md`.

| Concern | Decision | Why (short) |
|---|---|---|
| Tabular alpha | **LightGBM + XGBoost + CatBoost** | Strongest on heterogeneous tabular market/fundamental/sentiment features. |
| Time-series deep | **Temporal Fusion Transformer + PatchTST** (LSTM baseline) | SOTA interpretable multi-horizon + patched transformer; tested as *challengers*, never trusted blindly. |
| Microstructure | **DeepLOB-style CNN/LSTM** | Purpose-built for short-horizon move prediction from order-book data → entry timing + false-breakout filtering. |
| Financial NLP | **FinBERT + LLM event extractor** | Finance-specific language; LLM only summarizes/reasons over *structured* data, never the sole predictor. |
| Uncertainty | **Conformal prediction + isotonic/Platt** | Honest intervals + calibrated probabilities so confidence means something. |
| Validation | **Walk-forward + purged CV + embargo + forward paper** | Finance labels look ahead; ordinary CV leaks. Promotion requires OOS superiority. |
| App DB | **Postgres** (live) | Already running; perfect for app state + truth ledger. |
| Analytics DB | **ClickHouse or TimescaleDB** | Large time-series/feature/backtest queries. |
| Cache/queue | **Redis** (live) | Hot cache + job queues. |
| Lake | **S3-compatible Parquet** partitioned by date/symbol/source | Replayable raw history; no silent data rewrites. |
| Compute | **CPU for trees; GPU for transformers/DeepLOB/LLM embeddings/big backtests** | Cost vs need. |
| Serving | **FastAPI + scheduler + model registry + feature store** | Matches current stack; adds registry/feature-store. |

---

## 3. The phases (build order + the gate that proves each is done)

Each phase ships as its own PR(s) with tests and a hard **acceptance gate**. A phase is not "done"
because code exists — it is done when its gate is *measured true*.

### ✅ P0 — Foundation — **SHIPPED (PR79–PR84)**
Checklist engine, AI brief, market-regime adjustment, **truth ledger + resolver**, accuracy &
if-followed endpoints — all live and tested (470+ tests).

### ▶ P1 — Data Coverage Upgrade — **NEXT**
Raise live evidence from ~7/25 to **18+/25** for WOLF and **≥15/25** for every tracked symbol.
Build: SEC XBRL EPS/revenue extractor · Form 4 insider parser · 13F institutional parser · analyst
ratings/revisions feed · sector-ETF mapper · macro/Fed/CPI calendar + surprise classifier · options-
chain snapshot · guidance/news event classifier · coverage dashboard.
**Gate:** no prediction may be graded **A/B unless coverage ≥ 18/25**.

### P2 — Automated Prediction Logging
Schedule predictions (pre-open, open, midday, power hour, close) + event-driven (earnings, 8-K,
news shock). **Gate:** 100% of generated predictions have ledger rows.

### P3 — Expanded Outcome Resolver
Add target/stop **hit-time**, intraday **MFE/MAE**, gap behavior, earnings/news event windows,
slippage + spread models, and all horizons (intraday, 1/3/5/10/20d).
**Gate:** every prediction has complete outcome analytics.

### P4 — Research & Validation Lab
Walk-forward, purged-CV, embargo, triple-barrier labeling, market replay, Monte Carlo, transaction-
cost + slippage simulation. **Gate:** no model deploys without passing the lab OOS.

### P5 — Feature Factory (point-in-time)
All feature groups (technical, volume, volatility, order-flow, options, fundamentals, SEC, news,
macro, regime, cross-asset, cross-sectional, seasonality) with **leakage tests**; the feature store
serves train and live identically. **Gate:** zero future leakage; train/live parity proven.

### P6 — Model Zoo
Train + benchmark GBMs, TFT/PatchTST/LSTM, DeepLOB, FinBERT/event model, Bayesian/regime (HMM),
graph model; add champion/challenger registry + GPU training worker.
**Gate:** only models with **out-of-sample** improvement survive.

### P7 — Ensemble + Meta-Labeler
Stacked ensemble + regime-conditioned weights + meta-labeler (SHOW/WATCH/SKIP/NO EDGE).
**Gate:** false-positive rate down **and** profit factor up vs best single model OOS.

### P8 — Calibration + Self-Correction
Brier score, calibration curve, expected calibration error; auto-downgrade miscalibrated tiers/regimes.
**Gate:** predicted confidence ≈ realized win rate per tier (within tolerance).

### P9 — Institutional UI
Prediction, market, scanner, AI-reasoning, accuracy, if-followed, research, risk, health dashboards.
**Gate:** a non-professional understands prediction + risk + reason in **< 30 seconds**.

### P10 — Production Hardening
Monitoring, drift alerts, data-quality watchdog, feed failover, model rollback, analytics DB,
security audit, kill-switch enforcement. **Gate:** runs unattended with **no silent failure**.

---

## 4. Edge cases & failure modes the build MUST handle

These are the things that quietly destroy prediction platforms. Each is assigned to the phase that
owns it; none may be skipped.

1. **Look-ahead / data leakage** — the #1 way backtests lie. → P5 leakage tests + P4 purged-CV/embargo.
2. **Survivorship bias** — delisted/halted names vanishing from history. → P4 replay uses point-in-time universe; ledger already marks halted/delisted predictions *indeterminate* instead of fake-correct.
3. **Overfitting to one regime** — looks great in a bull run, dies in a selloff. → P4 regime testing + P8 per-regime calibration + P6 regime-conditioned weights.
4. **Confidence inflation** — "90%" that wins 55%. → P8 calibration + auto-downgrade.
5. **Cost/slippage erasing edge** — gross-positive, net-negative. → P3/P4 realistic cost + slippage sims; if-followed reported **after costs**.
6. **Stale / failed feeds** — a dead provider silently zeros features. → P10 data-freshness watchdog + P1 redundant providers + existing circuit breakers.
7. **Low-coverage false confidence** — grading A on 7/25 data. → P1 hard gate: no A/B under 18/25 (the system already says NO EDGE today, which is correct).
8. **Multiple-testing / p-hacking** — scanning thousands of setups guarantees lucky ones. → P4 minimum sample sizes + deflated metrics + P7 meta-label that must clear OOS, not in-sample.
9. **Label noise / horizon mismatch** — "up tomorrow" is noisy. → P4 triple-barrier labels across multiple horizons.
10. **Model drift** — yesterday's edge decays. → P11 loop + P10 drift alerts + champion/challenger.
11. **News duplication / generic feeds** — the same wire story counted 10×, or unrelated tickers. → P1 event classifier + dedup (today's AI brief already flagged generic CNBC repeats and rated trust *low*, which is the correct behavior).
12. **Single-symbol overfit** — WOLF-only tuning won't generalize. → P5/P6 multi-symbol training + cross-sectional features.
13. **Catastrophic-event gaps** — halts, circuit breakers, gaps through stops. → P3 gap modeling + P8 risk realism.
14. **Self-fulfilling/again-stale cache** — serving an old prediction as new. → P0 already separates cached deterministic vs fresh AI/log paths; P10 freshness SLOs.
15. **Security / abuse** — write/resolve endpoints must stay gated. → P10 audit; resolve/war-room already require MCP token.

---

## 5. Definition of done (the whole platform)

Ghost is "done" only when it can *prove*, out-of-sample and after realistic costs:

- A-grade predictions outperform B; B outperform C.
- NO-EDGE skips demonstrably avoid bad trades.
- Confidence is calibrated (predicted ≈ realized per tier).
- False-positive rate is low and tracked.
- If-followed performance is positive **after** slippage + costs.
- Performance survives multiple market regimes.
- Production runs unattended without silent failure.
- Every shown prediction carries probability, a risk plan, reasoning, and its own historical accuracy.

When all of those hold, the user's "sit back and watch" is as real as it can honestly be: the system
surfaces only edges it has already proven and keeps re-proving — and stays silent when it has none.

---

## 6. Immediate next slice

**PR85 — Data Coverage Upgrade (Phase 1).** Raise WOLF live coverage 7/25 → 18+/25 and enforce the
"no A/B grade under 18/25" gate. This is the single highest-leverage accuracy step, because every
downstream model and calibration layer is only as good as the evidence feeding it.

_Not financial advice. No guaranteed profit or accuracy. This document governs how the platform is
built and validated, not a promise of returns._
