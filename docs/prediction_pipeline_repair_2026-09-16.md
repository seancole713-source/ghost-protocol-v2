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
