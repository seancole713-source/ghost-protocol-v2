# Corrected-fleet qualification, September 16, 2026

This is a predeclared exploratory historical evaluation, not a registered
forward experiment or a claim of future trading accuracy. Prior research and
AAPL replay results are already known; this study is not an untouched holdout
relative to all previous project research.

## Fixed scope

- All 107 code-defined watchlist symbols, both directions: 214 hypotheses.
- One model configuration, current production parameters and corrected sector
  history. No geometry, feature-group or estimator hyperparameter sweep.
- The isolated process tightens the production staged target from 55% to the
  requested 70%. Production configuration is not changed.
- Five-year daily IEX snapshot, ending before September 16, 2026; only completed
  sessions enter training. Configured peers and SMH sector context are included.
- Snapshot bytes are SHA-256-bound to the manifest before model fitting.
- Reuse production backtesting, peer pooling, feature audit, purged walk-forward,
  calibration, precision proof and candidate packaging. Cache identical
  labeled rows to avoid recomputing peer histories for every hypothesis.
- Reject missing, invalid or stale current data. Large daily moves require a
  corporate-action audit before a candidate could qualify; they are not silently
  deleted or automatically assumed to be splits.
- Apply the unchanged admission gates, the 70% precision proof and an additional
  Bonferroni family-adjusted, two-sided Wilson lower bound of at least 70% on
  the gate's effective counts. This additional check cannot relax any gate.
- Report every attempted hypothesis, exclusions, errors, calibration failures
  and unfavorable results. No rerun or alternative threshold after seeing results.

## Safety and interpretation

The evaluator has no live credentials. Database connections and HTTP requests
are blocked during fitting; it reads the local frozen snapshot only. Training
runs locally, sequentially, with numerical-library thread limits, not inside
the production web service. No model, pick, registration or activation is
written to production. A hypothetical qualifying candidate would still need
new independent forward evidence and the normal approval process.

This daily model does not become an intraday/premarket model by passing a
historical test. Price-feed scope, corporate actions, execution costs and
serially correlated labels remain limitations. Effective-count Wilson bounds
are the current conservative screening method, not a proof of independence.

Command (run in a fresh interpreter with the repository on PYTHONPATH):

```sh
python scripts/validate_frozen_fleet.py \
  --manifest docs/research/corrected_fleet_20260916_manifest.json \
  --bars /private/tmp/ghost-fleet-validation-20260916/bars.json \
  --output /private/tmp/ghost-fleet-validation-20260916/results
```

The raw vendor dataset remains local, not redistributed in Git. The manifest,
method, runtime versions, complete per-hypothesis metrics and final report are
retained for audit.
