# Squeeze evidence repair - September 17, 2026

## Verified defects

Baseline: `6572468`, PR #202, verified live before this repair. Six new regression
cases failed on that baseline:

- Daily HTTP 200 plus intraday HTTP 429 was classified as no intraday trading.
- A missing prior-session baseline was also classified as no intraday trading.
- A five-hour-old bar reached signal evaluation and could reach issuance.
- Successful empty responses for the whole batch triggered per-symbol retries.
- Market-open state caused an hour-old scan to be described as non-stale.
- Direct candidate construction accepted prices without observation clocks.

## Implemented contract

`squeeze_bar_evidence_v1` is a data-evidence contract, not a model or an accuracy
claim. Each symbol has an explicit status: ready, successful empty selected-feed
response, request failure, skipped work, invalid baseline, invalid quote, or stale
quote. Both timeframe request summaries retain completion, feed, pages and error
reason. Partial pagination cannot establish that absent symbols have not traded.

- Intraday and baseline volumes must use the same feed. The exact preceding
  exchange session supplies the reference close; today's partial daily bar is
  excluded. The single-symbol fallback now uses the same implementation instead
  of stitching together a quote and positional previous-close/volume fallbacks.
- Unknown/future clocks, nonfinite or negative bar values, duplicate intraday
  timestamps and incompatible session data cannot become candidates.
- Five-minute bar timestamps remain explicitly `5Min_bar_start`, not live trade
  timestamps. Data expires after 600 seconds from that timestamp (one five-minute
  bar plus one publication interval). Consumers recheck age before issuance and
  alerts; no signal/confidence/accuracy threshold is lowered.
- Scorecards, watches and the existing outcome JSON payload retain price clock,
  feed, reference date, volume basis, completeness and contract version. Existing
  historical outcomes are not rewritten or silently given the new contract.
- Pagination is bounded by pages, repeated-token detection and an overall
  30-second batch deadline. Rate limiting stops further chunks and never fans
  out into per-symbol requests. The batch is off the async loop. A timed-out
  worker remains owned; subsequent scans cannot queue duplicate work behind it.
- Snapshot staleness comes from scan age, separately from quote age and market
  hours. Rejection counts and reasons survive snapshot persistence. The console
  and cockpit account for invalid/stale evidence, not just HTTP failures.

The Alpaca pagination, feed, raw-adjustment and response semantics were checked
against its [historical bars reference](https://docs.alpaca.markets/us/reference/stockbars)
and [market-data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq).

## Read-only production-data probe

At **2026-09-17 14:13:50 UTC**, the repaired fetch path read all 107 official
watchlist symbols using existing credentials, with no prediction/alert/DB writes:

| Evidence state | Symbols |
| --- | ---: |
| Recent, complete, same-feed bars | 100 |
| Successful empty IEX response | 3 |
| Stale quote excluded | 4 |

The probe plus four public status requests took 3.65 seconds. This is one
observation, not an uptime/latency guarantee or an independent accuracy sample.
The then-deployed old scan showed 104 metric objects, two failures and one
no-print; these are nearby but not atomic samples and must not be interpreted as
a four-symbol before/after performance improvement.

## Validation and deployment

- Regression tests cover transport failures, interrupted/repeated pagination,
  successful empty batches, unknown/future/nonfinite/stale bars, same-feed volume,
  valid candidate provenance, persistence, disabled-batch fallback, and retained
  worker ownership after observation timeout.
- Full unit and isolated PostgreSQL suites, Ruff, compile checks and both changed
  HTML pages' JavaScript syntax are validated before release. Final counts and
  exact production commit/deployment verification are recorded on the PR.
- A read-only deployed API smoke test reconciles all coverage counters and checks
  candidate provenance. A non-completed scan is explicitly not coverage proof.

## Remaining work / no readiness claim

- These are recent selected-feed observations, not forecasts before the move.
  There is still no demonstrated 70% predictive accuracy or newly approved model.
- IEX does not establish consolidated full-market coverage. Historical/realtime
  SIP entitlement tracking is still shared in `core/prices.py`; not changed here.
- Other consumers of `core.prices.get_intraday_session` still need the positional
  previous-close review. The repaired scanner no longer uses that mixed path.
- Raw-bar corporate actions can still create mechanical gaps and distort volume
  baselines; these need independently verified event/adjustment handling before
  interpreting a jump as squeeze evidence. This release does not claim to solve
  corporate-action validation or change the Hunter's frozen calibration protocol.
- The heuristic squeeze confidence/EV logic is unchanged and is not demonstrated
  calibrated accuracy. Model admission, frozen experiments, quorum, trading
  permissions and existing thresholds remain unchanged.
- Validate continued production scans and the actual forward forecasting model,
  including premarket and regular-session outcome horizons. This turn's probe
  happened during the regular session; premarket is tested synthetically, not
  falsely claimed as a new live premarket observation.
