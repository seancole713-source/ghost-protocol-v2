# ghost-protocol-v2
<!-- redeploy 1774306638121 -->

Ghost Protocol v2 is a production crypto/stock prediction service running on Railway. It exposes a FastAPI application (`wolf_app.py`) with a hardened health audit endpoint, automated release gates, and Playwright smoke tests.

---

## Release Gate Process

Every push to `main` and every `rc-*` tag triggers a chain of release gate checks that must all pass before the deployment is considered safe.

### Gate chain (in order)

| Gate | Script | What it checks |
|------|--------|----------------|
| 1 | `scripts/go-no-go.sh` (Gate 1) | `/health` and `/api/health` return 200 with valid schema |
| 2 | `scripts/go-no-go.sh` (Gate 2) | `/api/stats` and `/api/cockpit/context` are consistent |
| 3 | `scripts/go-no-go.sh` (Gate 3) | `/api/diagnostics` returns valid payload |
| 4 | `scripts/go-no-go.sh` (Gate 4) | `/api/coverage` reports model status |
| 5 | `scripts/go-no-go.sh` (Gate 5) | `POST /api/health/audit` returns valid audit schema |
| 6 | `scripts/go-no-go.sh` (Gate 6) | 403 error payload is deterministic (`ok`, `error_code`, `stage`, `ts`) |
| 7 | `scripts/verify_health_audit.py` | Full audit endpoint validation (auth, schema, history, response time) |
| 8 | `scripts/check_error_signatures.py` | Error payloads match `.github/error-signatures-baseline.json` |
| 9 | `scripts/go-no-go.sh` (Gate 8) | `/cockpit` page loads with expected markers |

### Running gates locally

```bash
# Run the full bash gate orchestrator
BASE_URL=https://ghost-protocol-v2-production.up.railway.app \
CRON_SECRET=your_secret \
bash scripts/go-no-go.sh

# Run individual Python gate scripts
BASE_URL=https://... CRON_SECRET=your_secret python scripts/verify_health_audit.py
BASE_URL=https://... CRON_SECRET=your_secret python scripts/check_error_signatures.py

# Or use Make targets
make gates BASE_URL=https://... CRON_SECRET=your_secret
make verify-audit BASE_URL=https://...
make check-signatures BASE_URL=https://...
```

### Make targets

```
make test              Run unit tests (pytest)
make type-check        Run mypy on gate scripts
make go-no-go          Run bash gate orchestrator
make verify-audit      Run verify_health_audit.py
make check-signatures  Run check_error_signatures.py
make gates             Run all gate scripts
make all-checks        Run tests + type-check + gates
```

---

## Health Audit Endpoint

`POST /api/health/audit`

Requires `x-cron-secret` header matching `CRON_SECRET` env var (if set).

**Success response** (`200`):
```json
{
  "ok": true,
  "audit": {
    "run_ts": 1700000000,
    "stage": "production",
    "overall_status": "PASS",
    "summary": {
      "total_checks": 12,
      "passed": 12,
      "warned": 0,
      "failed": 0,
      "coverage_pct": 100.0
    },
    "findings": [...],
    "auto_fix_log": []
  }
}
```

**Error response** (deterministic shape for all failure paths):
```json
{
  "ok": false,
  "error": "Forbidden",
  "error_code": "auth_failed",
  "stage": "production",
  "ts": 1700000000
}
```

The `error_code` values are:
- `auth_failed` — bad or missing `x-cron-secret`
- `audit_engine_failed` — internal error in the audit engine

---

## Error Signature Baseline

`.github/error-signatures-baseline.json` defines the expected shape of error responses. The `check_error_signatures.py` gate compares live responses against this baseline on every release. To update the baseline after an intentional schema change:

1. Edit `.github/error-signatures-baseline.json`
2. Bump `meta.version` and `meta.updated`
3. Open a PR — the gate will validate the new baseline against the live endpoint

---

## E2E Smoke Tests

Playwright tests live in `e2e/cockpit.smoke.spec.ts`. They use stable, deterministic selectors:
- `#tab-crypto`, `#tab-stocks`, `#tab-portfolio`, `#tab-results` — tab IDs
- `page.title()` — unique `<title>` element
- `.footer` — footer class

```bash
# Install dependencies
npm ci
npx playwright install --with-deps chromium

# Run smoke tests
BASE_URL=https://ghost-protocol-v2-production.up.railway.app npx playwright test e2e/

# Or via npm script
npm run test:e2e
```

---

## CI/CD

| Workflow | Trigger | Jobs |
|----------|---------|------|
| `.github/workflows/ci.yml` | Push to `main`, PRs | unit-tests → integration-tests → playwright-smoke → release-gates |
| `.github/workflows/integration.yml` | `rc-*` tags, `v*` tags, manual | unit-tests → rc-release-gates |

Release gates in CI require `CRON_SECRET` and optionally `BASE_URL` to be set as GitHub Actions secrets in the `production` environment.
