# ghost-protocol-v2
<!-- redeploy 1774306638121 -->

## Test Commands

- `make test` — runs fast suite (excludes integration tests)
- `make test-integration` — runs DB integration tests (requires `TEST_DATABASE_URL` and `GHOST_INTEGRATION_TESTS=1`)
- `make test-all` — runs all tests with integration tests skipped unless enabled
- `make test-compile` — runs repository-wide Python compile checks
- `make test-e2e` — runs Playwright smoke tests (`/cockpit`, API consistency, desktop + mobile)
- `make verify-live` — validates `/health` and `/api/health` parity on `BASE_URL` (defaults to production URL)
- `make verify-health-audit` — enforces zero critical unresolved findings from `POST /api/health/audit`
- `make check-error-signatures` — fails on new runtime error signatures vs `.github/error-signatures-baseline.json`
- `make release-gates` — runs live parity, go/no-go, health-audit gate, and error-signature gate

## npm Quality Scripts

- `npm run lint` — `ruff` checks for `tests/` and `scripts/`
- `npm run type-check` — `mypy` checks for `tests/` and `scripts/`
- `npm run test` — maps to `make test`
- `npm run build` — maps to `make test-compile`
- `npm run test:e2e` — Playwright smoke tests
- `npm run verify:live` — live health route parity check

## Health Audit Endpoints

- `POST /api/health/audit`
  - Runs deep reliability scan with structured findings:
    - `status`, `location`, `evidence`, `impact`, `auto_fix`, `fix_result`
  - Persists each run into `health_audit_runs`
  - Optional query/body flag: `auto_fix` (default `true`) for safe self-heal hooks

- `GET /api/health/audit/history?limit=20`
  - Returns recent persisted health-audit run summaries for recurrence tracking

## CI Workflows

- `.github/workflows/ci.yml`
  - Runs on push to `main` and pull requests
  - On all events: installs dependencies, runs `make test`, then `make test-compile`
  - On push/manual release runs: executes integration tests, Playwright smoke (with artifacts), deploy go/no-go, health-audit critical gate, and error-signature alert gate

- `.github/workflows/integration.yml`
  - Manual trigger (`workflow_dispatch`) and release-candidate tag pushes (`rc-*`)
  - Requires repository secret: `TEST_DATABASE_URL`
  - Runs `make test-integration` with `GHOST_INTEGRATION_TESTS=1`

- `.github/workflows/e2e.yml`
  - Manual trigger (`workflow_dispatch`)
  - Optional input: `base_url` (defaults to production URL)
  - Runs Playwright smoke tests on desktop + mobile projects
  - Uploads `playwright-report` and `test-results` artifacts