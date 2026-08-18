# Squeeze Hunter — Calibration Preregistration

**Registered:** 2026-08-18
**Status:** Methodology frozen BEFORE outcome data accrues. No probabilities are
fitted or published until the gates below are met.
**Scope:** `core/squeeze_hunter.py` + `core/squeeze_hunter_ledger.py` (the
read-only Squeeze Hunter / Explosion Radar).

This document freezes the calibration methodology *before* the resolver
accumulates outcomes, so the eventual fit cannot be tuned to the data it is
evaluated on (no hindsight bias). It does **not** claim any accuracy today.

---

## 1. What is being calibrated

The Squeeze Hunter currently publishes a **heuristic** projection:

- `p_plus_20_pct`
- `p_plus_50_pct`
- `p_plus_100_pct`
- `p_minus_20_pct`

These are fixed linear formulas with **no outcome data, holdout validation, or
confidence interval**. They are explicitly flagged `calibrated: false` and must
**not** be read as probabilities. This preregistration defines how — and only
when — they may become calibrated.

## 2. Labels (what counts as a hit)

For each evaluation, the resolver records realized outcomes over a **14-trading-day**
window from the evaluation's reference price:

| Label | Definition |
|-------|-----------|
| `hit_plus_20` | max favorable excursion ≥ +20% |
| `hit_plus_50` | max favorable excursion ≥ +50% |
| `hit_plus_100` | max favorable excursion ≥ +100% |
| `hit_minus_20` | max adverse excursion ≤ −20% |

Excursion is measured from the **live point-in-time reference price** (never
previous close). A missing reference price makes the evaluation **unresolvable**
and it is excluded from all calibration denominators.

## 3. Sampling policy (what enters the denominator)

- **One evaluation per symbol per session date** (the preregistered sampler).
- Public GET traffic is **read-only** and never writes samples.
- Only evaluations with a valid reference price and a **fully elapsed 14-day
  window** are eligible for calibration.
- Evaluations marked terminal (`missing_reference_price`,
  `historical_data_unavailable`) are excluded, not counted as misses.

## 4. Bins

Calibration is computed **per forecast label** (not a single blended score), and
optionally per explosion-score decile once sample size permits:

- Primary: whole-sample calibration per label.
- Secondary (only if `n ≥ 200` per decile): explosion-score deciles (0–10, …,
  90–100).

## 5. Minimum support (gates before any probability is published)

| Gate | Threshold |
|------|-----------|
| Minimum resolved evaluations | `n ≥ 100` |
| Minimum per-label hits | `hits ≥ 10` (else that label stays "insufficient data") |

Until these are met, the projection remains `calibrated: false` and the UI must
continue to show the heuristic disclaimer.

## 6. Calibration method

1. **Reliability diagram** — bin by explosion score, plot realized hit rate vs
   predicted probability.
2. **Brier score** — mean squared error between predicted probability and
   realized binary outcome, per label.
3. **Isotonic / Platt recalibration** — fit a monotonic mapping from heuristic
   score → calibrated probability on a **training split only**.
4. **Wilson 95% lower bound** — reported alongside every calibrated probability
   so small-sample uncertainty is never hidden.

## 7. Holdout rules (no leakage)

- **Walk-forward only.** A calibration fit uses only evaluations whose
  `issued_ts` precedes the evaluation being scored.
- **No lookahead.** The resolver's `evidence_available_ts` must be ≥ the
  evaluation's `issued_ts`; a resolution can never use information that was not
  available at issuance.
- **Frozen scoring version.** A calibration is tied to `HUNTER_SCORING_VERSION`.
  A scoring change bumps the version and starts a fresh, empty calibration
  sample — old rows are never re-read as if from the new model.

## 8. Promotion gate (when a calibrated probability may replace the heuristic)

A calibrated probability may be published **only** when ALL of:

1. `n ≥ 100` resolved evaluations for that label.
2. Brier score improves over the heuristic baseline (measured on the same
   holdout).
3. The Wilson lower bound of the calibrated hit rate is reported and the
   calibration is not overconfident (reliability slope within ±0.15 of identity).

Otherwise the heuristic projection stays, with `calibrated: false`.

## 9. What this explicitly does NOT do

- Does **not** claim 70% accuracy (that is a separate, already-falsified
  contract — see `docs/70_precision_protocol.md`).
- Does **not** fire picks, loosen gates, or alter the production baseline.
- Does **not** turn the heuristic into a "guaranteed" probability — even a
  calibrated probability is an estimate with a confidence interval.

## 10. Status

- **Issuance:** active (one evaluation per symbol per session date).
- **Resolution:** active (14-day window, terminal reasons for unresolvable rows).
- **Calibration:** **NOT STARTED** — waiting for `n ≥ 100` resolved evaluations.
