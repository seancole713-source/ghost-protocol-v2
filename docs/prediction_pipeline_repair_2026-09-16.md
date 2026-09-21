# Prediction pipeline repair - 2026-09-16

## Observed before changes

- Production commit: f374e5a34121a5228c004b33da5f6b8dc712e9d4.
- All 257 stored artifacts were unproven (214 research, 43 unknown); no
  official model was fireable. The engine was not kill-switch paused.
- Thirty recorded daily summaries contained 740 scans and zero saved picks.
- The dedicated research registry had zero active artifacts, predictions or
  resolutions. Its runner was disabled; it is separate from the shadow ledger.
- `/api/shadow-stats` returned HTTP 500: `name 'db_conn' is not defined`.
  PR #196 moved the query into a helper but left its import in the caller,
  breaking both the scoreboard and the confidence-calibration read path.
- The squeeze batch reader always chose `daily[-2]` as prior close. Before
  today's daily bar exists, this is two sessions ago. At 08:27 CT NOK's
  displayed move used September 14's 9.65 rather than September 15's 9.82.
  Independently read Alpaca IEX bars confirmed these dates and closes.
- Pre-market scores overlaid a live quote onto yesterday's daily OHLC bar;
  regular-session scores could consume an incomplete current-day bar.
- IEX-only observations are not market-wide coverage. Missing bars do not
  establish that a security has not traded elsewhere.

## Repair scope

- Restore the real shadow DB reader and remove schema-changing DDL from the
  scoreboard's read path. Add tests through the reader, not just aggregators.
- Align reference closes by exchange session date, including weekends and
  holidays. Exclude today's partial daily volume from the RVOL baseline.
- Build daily-model features only from the latest completed session; fail
  closed if that session is absent. Preserve extended quotes as context only.
  Handle early closes and the existing five-minute issuance delay.
- Expose existing immutable shadow forecasts at `/api/forecasts/research`
  and in a separate Research forecasts console tab. Keep original IDs,
  issuance times, horizons, reference prices and release blockers visible.
  Overdue unresolved rows are counted separately, not called open forecasts.
- Correct Ghost Ask's claim that a running pre-market scan can issue a daily
  model pick. Scanning and post-close issuance are different operations.
- Add a live evidence-API smoke test so server-health success cannot conceal
  another broken prediction scoreboard.

## Explicit limits

These repairs do not establish 70% accuracy, enable live-money trading, create
an intraday-trained model, activate an unregistered experiment, or turn existing
research rows into newly issued official picks. Model thresholds, weights,
artifact identities, kill gates and Railway variables are unchanged.

The new research view makes forecasts observable; it is not a substitute for
improving their measured performance. Today's pre-market ended before release.
Deployment verification must be recorded separately after the exact release
commit is live. Passing code tests is not a passing investment experiment.

## Pre-release validation

- 2,049 unit tests passed; 43 database integration tests require CI.
- Ruff, compilation and diff checks passed.
- Local fixture browser checks passed at 1280px and 390px, including API
  failure display, no horizontal overflow and no JavaScript errors.
- GitHub TEST_DATABASE_URL is already configured (verified by secret name,
  not by disclosing its value). Main-branch CI will run PostgreSQL integration.

## Production verification of PR #197

- PR #197 merged as f5b7e4bba99c0a1c3b9ee51c66884777ead8c227.
- Railway deployment 12c364b3-f537-4fe5-af01-3421a9ea64a6 reached SUCCESS.
- Exact version and evidence-API smoke passed in production.
- Research view returned 206 open forecasts and 32 overdue unresolved rows.
  Example: AAPL record 3405357, issued September 15, UP, five trading bars.
  These are existing forecasts, not newly issued September 16 predictions.
- A fresh regular-session squeeze scan had usable observations for 102/107
  symbols (two fetch failures, three lacking usable prints on the selected feed).
- An authenticated normal prediction cycle completed successfully but generated
  zero official picks. All 257 artifacts remain unproven. No gates were bypassed.
- PostgreSQL integration actually ran and passed, not skipped.
- Production browser run: 61 passed, 10 skipped, two new tests failed because
  the test targeted /picks instead of /console. The follow-up corrects that
  route, links /picks to /console#research, and replaces the existing false
  unpaused-equals-Available labels with model-readiness checks.
- A read-only production query identified all overdue rows: APGE (9) and SATS
  (23). The current Alpaca request returned APGE bars only through September 2
  and no SATS bars in the requested period. These rows were not force-resolved,
  deleted, or counted as successful predictions.

The accurate-intraday-prediction mission is NOT achieved by these repairs.
The live daily model remains research-only, and new daily evidence is issued
post-close. A validated intraday model and adequate feed coverage are separate
requirements, not something a confidence-label change can supply.
