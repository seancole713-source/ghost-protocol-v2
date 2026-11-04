# Local release repair — 2026-11-04

Base: PR #249 head `c5f4272a06274c22e3a71940e0d7daac1afabd76`.
Checkout: `/tmp/ghost-release-repair-20261104`, branch `codex/release-repair-20261104`.
Status: local implementation and verification only; no push, merge, deployment,
production writes, broker calls, credential changes, paid research or external-agent messages.

## Confirmed repairs

The cash summary previously omitted a missing broker-entry snapshot without an
issue even when the caller supplied a submitted order or actual loss. It now
counts these as unreconciled, exposes `missing_entry_orders`, and retains the
actual outcome in the row without inventing broker-derived P&L. Observe-only,
NO_FILL and genuinely shared results do not create missing-entry alarms.

A forecast ID no longer substitutes for a broker entry ID/client order ID.
Identity-less fills are unreconciled. A client order ID remains valid evidence.

Before deduplicating, the summary now compares normalized filled quantity and
average price for repeated entry IDs and repeated exit IDs, both within one row
and across rows sharing an entry. Any contradiction excludes that entry from
reconciled totals and reports an issue regardless of input order. Identical
snapshots continue to count once. These readout changes never rewrite outcomes.

## Deployment diagnosis

Read-only Railway logs for failed main deployment
`cea2e31f-07e8-4ec2-9993-8ca893e9f90f` show runtime preflight OK on Python 3.12.14,
then DEPLOY_FREEZE BLOCKED at 2026-10-30 09:55 EDT. It attempted existing main
`a6e3615`, not this PR. Existing successful deployment
`9e7c463c-fce3-49df-aee6-87737ec470e7` remained active. No freeze check or override
was changed. This intended rejection does not demonstrate a PR #249 boot fault.

## Browser investigation

The recorded October 12 mobile failure timed out clicking `#truth-toggle`
because attribution content intercepted pointer events. Static and delayed-load
offline probes did not reproduce it. No speculative CSS/product change was made.
A new offline Playwright test uses the shipped HTML/CSS, attribution renderer and
truth-toggle handler, with every network request blocked. It checks normal clicks
while attribution loads and three expanded/collapsed round trips after it loads,
on desktop Chromium and Pixel 7 Chromium. No force clicks or production calls.
This is a focused layout regression, not the full live orchestration/release gate.
Existing browser tests/assertions remain unchanged.

## Verification

- All three accounting faults reproduced against the original function. The new
  accounting suite against the baseline produced 8 failures/7 passes; repaired
  code passes all 15 accounting tests as part of the edge suite.
- `PYTHONDONTWRITEBYTECODE=1 /tmp/ghost-repair-20261030-venv/bin/python -m pytest -q --noconftest tests/test_edge_*.py -k 'not mcp_tool'`:
  **715 passed, 7 skipped, 3 deselected**, 12.09 seconds. No integration database
  configured/started and no production database used. This run does not replace
  the pinned Python 3.12.14 CI or PostgreSQL integration lane.
- `playwright test e2e/cockpit.layout.spec.ts --project=desktop-chromium --project=mobile-chromium --workers=1 --reporter=line`:
  **2 passed**, 3.0 seconds, entirely offline.
- Ruff on changed Python files and `git diff --check`: pass.
- Local interpreter: Python 3.13.5. Existing dependencies/browser reused; one
  browser worker; no SMD checkout, process, port or test service touched.

## Remaining release requirements

Independent review of the repair; pinned-runtime CI; exact-deployment browser
smoke (including the historical mobile case); authenticated passive release gates;
confirmation of deployment ownership and an authorized off-hours release. PR #249
had no submitted GitHub review at inspection. Its own unit/integration/edge CI was
green, while browser and release jobs were intentionally skipped on the PR.
The existing October 12 main CI also reported CRON_SECRET unavailable to the
health-audit gate; current secret configuration was not read or changed.

Current live revision, worker and performance observations belong to the parent
status assessment. No live-money promotion, predictive accuracy claim or tenant
isolation certification follows from these engineering repairs.
