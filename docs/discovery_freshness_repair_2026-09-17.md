# Discovery and quote freshness repair - September 17, 2026

## Production evidence before repair

Baseline release: f8015ff, Railway deployment
8fe31f9e-2853-4aac-8ed8-30d4a124da6f. No newer main commit or competing repair PR
was present when this isolated worktree was created.

- At 08:28 CT the mover API displayed DAIC +103.98% from a roughly 29-minute-old
  observation despite a newer +102.30% observation in the ledger.
- A later read-only comparison showed CORZ +6.64% on the deployed selection,
  versus +5.98% on its newest provider update. This was historical-peak
  selection, not a forecast or a better live quote.
- Old selection admitted 180 rows representing only 119 distinct symbols.
  Repeated snapshots consumed the per-screen display budget before deduplication.
- The batch market-session endpoint used cache insertion time as price age.
  A synthetic hour-old quote in a ten-second-old cache was labelled live.
  Reference-only opening/closing prices could also inherit a live label.
- Read-only entitlement check: existing Alpaca credentials returned SIP HTTP
  403 and IEX HTTP 200. No credentials, subscription or Railway variables changed.

## Changes

- Select latest provider/screen/symbol observations before budgeting. Retain the
  latest invalid row rather than reviving an older passing quote.
- Bound intraday reads by the existing freshness window; retrieve large raw JSON
  only after selection. Daily reference observations retain their separate bound.
- Choose each symbol's latest update before validity and 5% display filtering.
  Missing, nonfinite, future-dated and expired evidence cannot become a mover.
- Return daily observations in `historical_alerts`, not ranked against current
  intraday moves. Carry source/receipt timestamps, move basis, and ledger IDs.
- Expose upstream cap truncation, not just truncation after ranking. Explicitly
  decline full-market-coverage claims.
- Batch market-session rows distinguish `cache_age_seconds` from observation
  `freshness_seconds`, prioritize stale observations within the same fetch budget,
  and mark reference-only and unknown-clock prices as non-live.

These are observational semantics repairs. They do not modify prediction models,
feature schemas, model thresholds, the official watchlist, frozen experiments,
quorum, risk gates, trading permissions, or historical outcomes.

## Validation

- Before fixes: 12 discovery and 6 market-session regression cases failed.
- After fixes: 2,077 unit tests and 47 real PostgreSQL integration tests passed.
  PostgreSQL ran on a newly initialized localhost-only temporary database, not
  production. Four new integration tests execute the actual selection SQL.
- Production-ledger reads were transaction-read-only. After optimizing selection,
  two observed query times were 111 ms and 57 ms. Earlier old-query measurement
  was 416 ms; these are individual observations, not a benchmark guarantee.
- Optimized live selection returned 142 rows / 141 symbols with no upstream cap
  truncation in that snapshot. It returned 16 current movers and explicitly
  excluded 23 stale and 2 invalid observations. Fewer alerts is not an accuracy
  improvement; it is removal of unsupported current-move claims.
- Three added read-only API smoke tests verify deployed data contracts. Exact
  release/CI/deployment results must be verified after merge and recorded on the PR.

## Remaining mission work

The accurate premarket/regular-session prediction mission is NOT completed.

- No new trained, approved intraday model or demonstrated 70% prediction accuracy.
- Main stock coverage remains 107 symbols; outside-watchlist discovery is advisory.
- The discovery scheduler still only logs; no delivered-notification path added.
- Squeeze batch counters conflate successful empty responses, failed requests and
  invalid baselines. The morning 23/107 count was metric objects, not necessarily
  23 verified-fresh quotes. Fix their per-timeframe status contract next.
- Squeeze scorecards discard bar observation clocks; scan freshness is not quote
  freshness. Preserve source/feed provenance and reject stale evidence at issuance.
- Per-symbol previous-close fallbacks still use positional daily bars and can use
  the wrong session. They need the calendar-aware prior-session contract.
- Historical/realtime feed entitlements, mixed-feed volume comparability and
  async batch timeout/single-flight ownership require separate verified repairs.
- Corporate-action jumps in external screener percentages are not independently
  validated by this change. No +500% screener print is endorsed as a squeeze.
