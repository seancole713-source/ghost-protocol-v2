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

## Follow-up independent review — supersedes initial repair completeness

Independent review of initial commit `760883e` found remaining failures at the
readout boundary, alias handling and differing exit sets. The initial commit
alone is not release-ready. These were reproduced, then repaired:

- `edge/readout.py` no longer collapses same-client entry snapshots with a dict
  comprehension. It retains all snapshots and attached exit evidence in the
  additive `broker_snapshots` field while retaining one display row per forecast.
  The representative displayed snapshot is deterministic; financial validation
  sees every snapshot, including broker-ID-only aliases reachable through an
  attested ID/client-ID pair or the submitted order ID.
- Accounting links typed broker/client aliases only within one supplied
  account/store snapshot. There is no global identity map. Multiple broker IDs
  tied to one client ID (or multiple client IDs tied to one broker ID) are an
  explicit conflict, not silently accepted. Entry and exit aliases cannot double
  count one order. Separate store calls do not influence one another. This is
  snapshot-local identity handling, not certification of application tenancy.
- Every duplicate entry snapshot must independently contain the same set of
  filled exits. Missing, partial, contradictory or different complete exit sets
  are unreconciled. The implementation does not union partial snapshots into
  invented complete evidence. Identical snapshots and numeric-equivalent fills
  remain deduplicated. Gross P&L and counts do not depend on input ordering.
- The browser regression now has an explicit Promise barrier. It asserts that
  attribution is waiting and has no rendered rows, performs the real click and
  collapse assertions, verifies the response is still blocked, then releases it
  and checks post-render click round trips. No timing delay is used as proof.

Reproduction against exact `760883e`: five of the six initial follow-up cases
failed, including full-readout conflicting snapshots and different exit sets.
A separate direct-helper alias regression confirmed two cash orders counted
instead of one. All 22 final accounting tests pass, including both-order checks,
six alias permutations and readout no-write assertions. The repaired browser test
passes desktop and mobile Chromium (2 tests, 2.4 seconds), with all network blocked.
No existing assertion was removed or weakened.

Final full edge-suite result follows below. The earlier 715-test result documents
only the initial repair and is not the final completeness claim.

Final verification after all follow-up changes:

- Edge suite: **722 passed, 7 skipped, 3 deselected**, 10.29 seconds.
- Skips: one optional `exchange_calendars` test, four optional EdgarTools tests,
  and two tests requiring an explicit disposable PostgreSQL database. Production
  dependencies were not installed or altered; pinned-runtime CI remains required.
- Accounting subset: **22 passed**, including full-readout alias and contradiction
  paths. Browser subset: **2 passed**, desktop/mobile with the explicit barrier.
- Ruff on all changed Python code and tests; diff whitespace checks: pass.

## Exit alias selection follow-up — supersedes head 1e45aff

Independent review found that role-based top-level exit selection still dropped
broker-only aliases before accounting. Exact `1e45aff` reproduced the supplied
case through `paper()`: entry 10 at $10; named time-exit 10 at $11; same exit
broker ID with no client ID at $8; no `tx_closes`. It reported +$10 and no issue.

Exit selection now indexes all retained top-level snapshots and nested legs.
Once any member is attested as a bracket, protective, flatten, time-exit or
position-close order, the complete snapshot-local alias group is passed to
accounting. A broker-only contradictory snapshot cannot disappear because it
lacks the naming prefix. Protective children and bracket legs are indexed too.
The fix preserves evidence rather than changing cash reconciliation thresholds.

Regressions exercise all six input permutations for named time-exit, protective
and flatten paths with contradictory prices and quantities; identical aliases
still count once. Additional full-readout cases cover protective-child and
bracket-child broker-only contradictions, plus a client-only position-close
alias linked through `tx_closes`. Every contradictory case is unreconciled and
excluded from completed P&L. Store fixtures are unchanged after each read.

Final verification: **727 edge tests passed, 7 skipped, 3 deselected** (10.32s);
**27 accounting tests passed** (0.11s); Ruff and diff checks pass. Skip reasons
remain the optional calendar/EdgarTools dependencies and the two disposable-DB
cases documented above. Browser code is unchanged from its verified barrier
version; no redundant browser run was performed for this Python-only repair.
No production, trading, credential, safety-gate or deployment-freeze changes.
