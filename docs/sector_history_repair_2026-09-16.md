# Sector training-history repair - September 16, 2026

## Verified defect

`backtest_symbol` requests the configured five-year target history but called
`_fetch_sector_series()` with its one-year default. Sector relative strength
then became zero before that shorter history began. Live scoring receives real
recent sector context. This silently deprived most training rows of a feature
that is enabled in the current production feature schema.

At 14:18 UTC, an entitled Alpaca IEX request returned 1,252 completed daily bars
each for AAPL and the configured SMH proxy, spanning September 20, 2021 through
September 15, 2026. Applying the actual 120-bar feature window, five-bar label
horizon and 20-bar sector lookback produced this comparison:

| Sector History | Training Rows | Missing Context | Nonzero Context |
| --- | ---: | ---: | ---: |
| Prior one-year request | 1,126 | 902 (80.11%) | 224 |
| Matching five-year request | 1,126 | 0 | 1,126 |

This measures missing context explicitly, not merely zero-valued returns.
Future sector values are never backfilled into earlier observations.

## Repair and validation

- Pass the configured target-history period to the existing sector fetcher.
- Keep live scoring, feature definitions, model identities, proof thresholds,
  labels and the disabled-sector path unchanged.
- Regression tests cover both two- and five-year configurations and the
  disabled path. The two enabled tests failed before the patch and passed after.
- Full local unit suite: 2,054 passed, 43 integration tests deselected for CI.
- Ruff and diff checks passed.

## Paired exploratory replay

The comparison was declared before fitting: AAPL, both directions, old versus
corrected context, identical frozen bars and production parameters, with no
peer pool in either arm. The existing purged `_train_one_direction` validator
was used. Database access was blocked; model bytes were discarded. This is not
a replay of the full production peer pool, a registered forward experiment,
or evidence of a 70% strategy. All results are reported, including regressions.

| Direction | Context | Holdout Accuracy | Walk-Forward Edge | Gate Brier |
| --- | --- | ---: | ---: | ---: |
| UP | One year | 58.58% | -4.11 pp | 0.2456 |
| UP | Five years | 52.66% | -2.32 pp | 0.2597 |
| DOWN | One year | 49.70% | -7.32 pp | 0.2788 |
| DOWN | Five years | 53.25% | -7.86 pp | 0.2701 |

All four fitted variants failed admission. The fix is justified by data
correctness, not a selected favorable performance number. No replay model was
stored, registered, activated or used for a real-money trade.

## Remaining model blockers

A bounded read-only audit of all 214 current research artifacts found genuine
validation failures, not a wrongly set tier alone. Their first admission
failures were holdout accuracy (157), edge (52), fold count (3), and walk-forward
accuracy (2). None had a valid precision proof even at the currently configured
55% staged target. The other 43 stored artifacts are old unknown-tier models.

The user-facing 70% objective remains unproven. Correct data and a successful
deployment are necessary infrastructure, not proof that a model is accurate.
Deployment and exact-commit CI verification are recorded on the repair PR.
