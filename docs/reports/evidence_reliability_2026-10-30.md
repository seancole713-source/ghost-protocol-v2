# Evidence reliability repair - 2026-10-30

Scope: repair demonstrated operational and measurement faults after the October
16-29 observation window. This is not a strategy promotion, a model retrain, or
proof of a predictive edge. Existing forecasts, terminal outcomes, experiment
hashes, entry rules, release gates, and consensus requirements are unchanged.

## Repairs

- SEC replay cross-check: EdgarTools' colon-header parser does not parse the
  tagged SEC header used by a real Apple filing. Select its tagged submission
  parser for that format. Add the real public header as a regression fixture.
  Parser errors and unavailable cross-checks now block trust rather than vanish
  from the denominator. Parser version is 2; stored replay v1 is not rewritten.
- Cash accounting: the paper readout reconciles unique broker order IDs and exit
  quantities. Outside-rule losses remain in financial P&L while remaining excluded
  from rule accuracy. Shared experimental outcomes are not additional cash trades.
  Missing or contradictory fill evidence is exposed, never treated as a win.
- Research costs: PostgreSQL advisory locks serialize budget reservations and
  charges, including the first writer. Calls need reserved headroom. Unexpected
  failures retain the reservation because upstream spend may be unknown.
- Reviewer diagnostics: distinguish authentication, transport, malformed response,
  and budget failures from successful independent-model review. The operations
  readout exposes fallback, low candidate price coverage, estimated budget overruns,
  and cached credential status. Pipeline notifications are once per issue/day.
- Quote timestamps: timestamps more than five seconds in the future are stale,
  not fresh. This does not relax any freshness or release threshold.
- Codex worker 1.1: research requests cited prose before a tool-free JSON formatting
  step. Provider-attested URL citations remain mandatory; bare URLs and uncited
  search results are rejected. Reject incomplete Responses API results. Do not
  spend on formatting when citations are already absent. Retain the cited draft
  alongside its formatted envelope; set `store: false` on both requests.
- CI: both integration workflows provision disposable PostgreSQL 17 services.
  Pull requests run integration tests rather than depending on a missing repository
  secret. No production database credential is required or used. Edge-only CI
  installs the production EdgarTools/pyarrow pins so the real parser test runs.

## Validation and observed production state

- Local full unit and PostgreSQL suites pass; exact final counts and CI status are
  recorded in the PR. Local interpreter is Python 3.13.5, not production's 3.12.14;
  GitHub CI uses `.python-version` to check the production interpreter.
- PostgreSQL tests exercise concurrent first-row budget admission, concurrent
  charging, and rollback. Unit tests cover missing evidence, duplicate orders,
  partial exits, outside-rule P&L, redaction, caching, and fail-closed behavior.
- OpenAI credentials for both services passed real model calls after the operator
  replaced them. The active main container still needs a successful deployment
  to load its replacement credential.
- Enabled the previously disabled Codex worker with one task/hour and four/day.
  Deployment `6fef2c87-d7a3-4e06-8d8c-6dbb46e8c14f` reached SUCCESS and the live
  workflow reported two workers online. The original JSON-only worker's first
  FDXF attempt searched but had zero native citations and correctly refused to
  submit. A non-submitting live canary of worker 1.1 on the same task returned four
  URL citations and a completed formatted envelope. That is an API-path test,
  not verification of every claim or all future tasks.
- The Contract 70 registration endpoint was called with its conservative defaults.
  It returned `registered: false`, `status: no_qualified_slice`. No forward window
  was invented, reset, or backfilled. The current high-confidence directional
  record is 378/668 (56.59%), Wilson lower bound 52.80%, session-block lower bound
  51.04%; the verdict remains `FALSIFIED_AT_CURRENT_DATA`.

## Boundaries and remaining work

1. Main deployment: preserve the market-hours freeze. On October 30 the normal
   release window opens at 16:30 America/New_York / 15:30 America/Chicago. Do not
   set `DEPLOY_FREEZE_OVERRIDE`. After reviewed CI passes and release is authorized,
   deploy the exact commit, verify its Railway deployment and `/api/_version`,
   then verify the operations and paper readouts. A successful worker deployment
   is not a successful main-app deployment.
2. Budget reservations are conservative admission estimates, not a provider-enforced
   dollar cap. A single admitted call can exceed its estimate. The standalone
   worker has separate process-local task limits, which reset on restart. Its
   spend is not included in the Edge research budget; use provider billing limits
   and usage reconciliation for an account-wide limit.
3. Live SIP quotes were entitlement-denied while IEX worked. No paid subscription
   was bought, and IEX is not silently relabelled as consolidated live coverage.
   The 95% coverage figure is an operational warning, not a new trading gate.
   Borrow availability/fee coverage is still insufficient for squeeze promotion.
4. The SEC full replay must run as a separately recorded new evaluation with
   parser v2. A fixture pass does not repair or validate the old failed replay.
5. Changing infrastructure does not change the observed strategy performance.
   The October 16-29 paper sample contains 78 unique completed broker orders,
   gross P&L -$187.63 before fees and operating costs, including $68.74 of
   outside-rule losses. These correlated trades are not 78 independent proof
   samples. No live-money promotion or 70% guarantee is justified.
6. Database-bloat maintenance remains a separate controlled operation with backups,
   lock/disk checks, and an off-hours window. No destructive cleanup, `VACUUM FULL`,
   or reset of production outcomes was performed.

## Reproduce locally

Use a disposable test database, never `DATABASE_URL` from production:

```sh
python -m pytest -q -m 'not integration'
GHOST_INTEGRATION_TESTS=1 TEST_DATABASE_URL=postgresql://.../ghost_ci_test make test-integration
python -m pytest -q --noconftest tests/test_edge_*.py -k 'not mcp_tool'
git diff --check
```

Public fixture source: https://www.sec.gov/Archives/edgar/data/320193/000032019324000120/0000320193-24-000120-index-headers.html

Responses API citation contract: https://developers.openai.com/api/docs/guides/tools-web-search
