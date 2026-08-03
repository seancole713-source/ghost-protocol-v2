# Ghost Protocol — 70% Precision Protocol v2

**Registered:** 2026-08-02
**Supersedes:** Contract-70 v1 (2026-07-16, 25-row revival rule)
**Status:** Active for all new claims; v1 preserved for existing verdicts only

## 1. What "70% Accurate" Means

Ghost claims **selective precision among qualified TP/SL picks**, not accuracy across every market scan. A prediction is "qualified" when it passes every live gate (precision, proven-skill, overconfidence, regime, objective, pause, kill, deduplication, and geometry) and fires with a real entry, target, and stop.

The accuracy denominator is:

```
WIN / (WIN + LOSS + EXPIRED)
```

- **WIN:** Price touches the target before the stop within the hold horizon.
- **LOSS:** Price touches the stop before the target, or target and stop are touched in the same bar (collision = LOSS).
- **EXPIRED:** Neither target nor stop is touched within the hold horizon. EXPIRED is a non-win.
- **DATA_INVALID:** Reported separately. Must not silently disappear. Capped at 10% of total predictions.

## 2. The Confirmatory Test (v2)

### 2.1 Fixed Sample

The confirmatory test uses exactly **50 forward actionable outcomes**. There is no early-success rule. The experiment may stop early only for futility: once more than 8 non-wins are recorded, 42/50 becomes mathematically impossible.

### 2.2 Passing Threshold

Minimum pass: **42 wins out of 50**. The exact unrounded 95% Wilson lower bound at 42/50 is approximately **0.7149**, which genuinely exceeds 0.70.

| Wins | N  | Exact Wilson Low | Pass? |
|------|----|------------------|-------|
| 41   | 50 | 0.6931           | No    |
| 42   | 50 | 0.7149           | Yes   |
| 43   | 50 | 0.7365           | Yes   |

### 2.3 Admission Rule

Admission compares the **unrounded** Wilson lower bound against 0.70. Display rounding to `0.7000` is not sufficient. The case `76/96` (exact low ≈ 0.6999) is a documented failure despite displaying as `0.7000`.

### 2.4 Diversity Requirements

- At least **20 distinct issuance dates** across the 50 outcomes.
- For universe-level models: no single symbol may contribute more than **20%** (10 of 50) outcomes.
- Per-symbol models are exempt from the concentration limit but still use at most one observation per symbol/trading date per exact artifact generation.

### 2.5 Calendar Deadline

The experiment must complete within **120 calendar days** of registration. An experiment that reaches the deadline without 50 outcomes is `INCOMPLETE`, not `FALSIFIED`.

## 3. Secondary Gates

Even with 42/50 wins, all of the following must pass:

| Gate | Requirement |
|------|-------------|
| Invalid rate | `DATA_INVALID / total_predictions <= 0.10` |
| Coverage | Actionable predictions >= 1% of eligible symbol-days |
| Brier score | <= 0.25 |
| Calibration gap | Absolute difference between mean probability and observed rate <= 0.10 per bin |
| Net expectancy | Positive after declared cost model (slippage + commission) |
| Profit factor | Gross wins / gross losses > 1.0 |
| Max drawdown | Normalized peak-to-trough <= 10% |
| Block bootstrap | Moving-block 95% lower bound >= 0.70 (temporal correlation safeguard) |

## 4. Two-Tier Proof

### 4.1 Artifact Proof (Storage Eligibility)

A single exact artifact (model bytes + contract + direction + schemas + threshold + universe/slice) completes the 50-outcome test. Passing authorizes **storage only** — the model may be written to `ghost_v3_model` as a `proven` tier artifact.

### 4.2 Policy Proof (Public Claim)

The complete set of artifacts allowed to fire constitutes the **active policy**. A separate 50-outcome forward test on the active policy must pass before Ghost may display:

> **"Proven at least 70% selective TP/SL precision for this frozen policy."**

Replacing any artifact, schema, threshold, slice, geometry, or contract resets the policy proof.

## 5. What Does NOT Count as Proof

- Historical/backtest evidence (candidate selection only, never proof)
- Lineage carryover (a new model SHA must prove independently)
- Mixed-artifact pools (every row must match the exact frozen identity)
- Post-hoc threshold or slice selection
- Optional stopping (checking repeatedly and declaring success when the bound crosses 0.70)
- Rounded Wilson values
- Dropped EXPIRED outcomes
- Duplicate symbol/date observations from the same artifact generation
- Evidence from non-TP/SL contracts (direction, volatility, cross-sectional, event)

## 6. Statuses

| Status | Meaning |
|--------|---------|
| `UNPROVEN` | No forward registration exists or no evidence yet |
| `COLLECTING` | Forward experiment in progress, fewer than 50 outcomes |
| `PROVEN` | 50 outcomes, >= 42 wins, all secondary gates pass |
| `DEGRADED` | Was PROVEN but recent evidence dropped below threshold |
| `FALSIFIED` | 50 outcomes, < 42 wins or secondary gate failure |
| `FUTILE` | 42/50 mathematically impossible given current non-wins |
| `INCOMPLETE` | 120-day deadline reached without 50 outcomes |
| `RETIRED` | Artifact replaced by a successor |

## 7. Legacy v1 Preservation

The existing 25-row revival rule (`REVIVAL_FORWARD_MIN_N = 25`, `REVIVAL_WILSON_LOW = 0.70`) in `core/contract_70_verdict.py` is preserved as **v1 behavior** for the verdict registered 2026-07-16. It is not used for any new claim. The v1 verdict may transition to `RETIRED` when a v2 policy proof succeeds.

## 8. Non-Negotiable Constraints

- `GHOST_ACCURACY_CONTRACT` must remain `70`. No environment override may weaken the target.
- Existing precision, proven-skill, overconfidence, regime, objective, pause, kill, and deduplication gates remain in force regardless of proof status.
- `V3_STOP_VOL_MULT` defaults to `0.65`. Geometry variants are research-only; activation requires a new production contract.
- `NO_FORWARD_CANDIDATE` and failed experiments are acceptable, truthful outcomes. The protocol cannot guarantee market predictability.
