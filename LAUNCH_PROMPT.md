# 🚀 GHOST PROTOCOL — LAUNCH PROMPT

> **Trigger phrase:** *"ready launch prompt and execute report"* (or *"run the launch prompt"*).
> **Audience:** Any agent — Claude / browser-control / general — picking up Ghost Protocol with no prior context.
> **Output:** A single launch-readiness report containing **all three verdict formats** (binary GO/NO-GO, graded scorecard, severity-ranked punch list).
> **Mode:** Heavy. **Every phase every time.** No fast variant. No skipping.
> **Mission-critical context:** The user is deciding whether a real human can **trust Ghost Protocol with real money**. Optimism without evidence is failure. False reassurance is worse than honest red.

---

## 0. NON-NEGOTIABLE RULES

These bind every agent that runs this prompt. Violating any of them invalidates the report.

1. **No fabrication.** Every claim, status, and percentage must be backed by an artifact you actually retrieved this run: an endpoint payload, a log line, a git SHA, a DB row, a DOM screenshot, a rendered byte. If you cannot retrieve evidence, the answer is `INSUFFICIENT EVIDENCE` — never inferred.
2. **No "looks good."** Vague positives (works fine, looks healthy, seems clean, probably correct) are banned. Every assertion must be either a measurable fact ("`up_prob=0.5487` at `2026-06-22T22:46Z`") or an explicit `NOT VERIFIED` flag with the reason.
3. **No conflation of layers.** Local tests passing ≠ deployed production verified. Code committed ≠ deployed. Page renders ≠ data correct. Always state which layer you verified.
4. **Distrust the work log.** `PROJECT_STATE.md` and prior commit messages are starting points, not truth. Verify against production endpoints and live data. Note any divergence between docs and live state as a finding.
5. **Two independent sources for any "is it working" claim.** Backend payload + frontend render. Or: model state + alert delivery. Single-source claims are downgraded.
6. **Honesty layer is sacred.** If the engine is failing — `28.6% WR`, negative expectancy, coin-flip direction edge — the report must say so plainly. Investor-friendly framing is not the agent's job.
7. **Stop and ask** if you find divergence between this prompt's assumptions and reality (e.g. wrong repo, MCP disconnected, prod down) — don't write a report against the wrong system. Use `AskUserQuestion` to confirm scope.

---

## 1. PRE-FLIGHT — Establish ground truth before any test runs

Before any phase, **prove these five facts** and put them at the top of the report. If any fail, halt and ask the user.

| # | Fact | How to prove |
|---|------|-------------|
| 1 | **Repo identity** | `git remote -v` → `seancole713-source/ghost-protocol-v2` |
| 2 | **Working branch** | `git rev-parse --abbrev-ref HEAD` + HEAD SHA |
| 3 | **Production URL is reachable** | GET `/health` → 200 + parse `score` |
| 4 | **Production deploy version** | GET `/api/_version` → record `git_sha_short`, `_pr_version`, `app_version` |
| 5 | **MCP tools available** | List `mcp__Ghost__*` tools present; if absent, every phase below downgrades to "NOT VERIFIED — no MCP" |

Record in the report: **Repo · Local HEAD · Prod SHA · Prod _pr_version · Time · MCP state.**

---

## 2. PHASES — Run all of them, in this order

### PHASE 1 · DEPLOY CHAIN INTEGRITY *(does the code in git match what users see?)*

1. **Local ↔ remote ↔ prod alignment**
   - `git fetch origin && git log -1 origin/main --format="%h %s"`
   - GET `/api/_version` → compare `git_sha_short` to `origin/main` short SHA.
   - **PASS** if they match. **FAIL** if not — record the drift and stop (everything below is judged against the wrong code).
2. **Stale-container check**
   - GET `/cockpit` raw HTML → grep for the *expected* facelift markers: `Today's Top Movers`, `loadMoversBoard`, `id="movers-board"`. If absent: deploy is stale despite "Active" status (the documented Nixpacks cache trap).
3. **No-cache headers**
   - Verify `/cockpit` response carries `Cache-Control: no-store, no-cache, must-revalidate, max-age=0`.
4. **Build metadata stamped**
   - `<meta name="ghost-build" content="…">` present and matches `/api/_version` SHA.

**Report:** SHA match (Y/N), facelift markers present (Y/N), `meta ghost-build` value, no-cache headers (Y/N). Any FAIL = **P0 blocker.**

---

### PHASE 2 · DATA FRESHNESS *(is Ghost looking at live markets or stale numbers?)*

For each of these endpoints, record `ts` / `last_scan_ts` and compute `now - ts` in seconds. Anything older than 2× expected cadence is **stale**.

| Endpoint | Expected freshness |
|---|---|
| `/api/wolf/price` | ≤ 60 s during market hours |
| `/api/wolf/gate-status` | ≤ 60 s (or last market-scan tick) |
| `/api/squeeze/picks` | ≤ 180 s (3 min radar) |
| `/api/cockpit/context` → `stats.post_v32` | ≤ 5 min |
| `/api/wolf/pnl` | ≤ 5 min |
| `/api/wolf/pick-journal` | every fired pick should appear ≤ 60 s after fire |
| `/api/v3/train/last` | `state` + `finished_at`; flag if `state == "exception"` |

Also: GET `/api/diag/data-sources` and verify the 5-tier chain (Alpaca SIP → IEX → Polygon → yfinance → Stooq) is configured. Note any 403/429 patterns from logs as **P1** (rate-limit fragility = silent degradation).

**Report table:** endpoint · `last_ts` · age (s) · expected · status (FRESH / WARN / STALE / NO DATA).

---

### PHASE 3 · PREDICTION ↔ LIVE MATCH *(do the alerts and gate signals correspond to real prices in real time?)*

1. **Spot price sanity** — GET `/api/wolf/price` → `price`. Compare to a second independent source the agent can reach (yfinance via tool, Alpaca direct, or the gate-status `live_prediction.regime` price if present). Match within ±0.5% during RTH, ±2% pre/post-market.
2. **Open-pick freshness** — For every pick from `/api/picks` with `outcome: null`:
   - Compute `now - predicted_at` (hours).
   - Confirm `entry_price`, `target_price`, `stop_price` are real numbers.
   - Confirm `expires_at > now`.
3. **Pick → live drift** — For each open pick, compute `(live_price - entry_price) / entry_price * 100` and confirm it matches `/api/wolf/gate-status` if surfaced there. UI must agree with backend.
4. **Telegram alert ↔ DB pick parity** — If alerts are configured (`ALERTS_ENABLED=1`): cross-check the most recent alert subject/body to the matching `predictions` row by `predicted_at` window. Any alert without a DB row, or DB pick with no alert, is **P1**.
5. **Squeeze radar ↔ picks dedup** — Every symbol in `/api/squeeze/picks` with `kind=squeeze_active` should either (a) already appear in `/api/picks.active`, or (b) be blocked by an explicit gate visible in `/api/wolf/gate-history`. Orphan squeeze candidates that never enter the journal = **P1**.

**Report:** count of picks checked, drift table, any orphans/inconsistencies as P0/P1.

---

### PHASE 4 · IS GHOST ACTUALLY LEARNING? *(or is it static?)*

1. **Model lineage** — GET `/api/v3/status` and `/api/v3/train/last`. Record:
   - Current model `trained_at` timestamp.
   - Most recent training run `state`, `accuracy`, `passed`.
   - Time since last successful train. **Stale > 14d** = retrain expected (per `load_model` 14-day TTL).
2. **Schema guard verified** — Check `meta.feature_schema` on the loaded model. Confirm `load_model` rejects mismatched schemas (sample: ask the agent to read the relevant code path and quote the check). This must be in code; if not, the self-heal isn't real.
3. **Self-heal on deploy** — Inspect `_startup_train` in `wolf_app.py`. Confirm it gates on `_has_loadable_v3_model()` (a real load check), **not** `_has_any_v3_model()` (row existence). Cite the line. If still on row-existence, the engine can silently dormant after a schema bump = **P0**.
4. **Calibration is live** — `/api/wolf/gate-status` → `live_prediction.calibrated` must be `true` and `calibration_method` set. If `false`/`null` on a recent model, calibration is dead = **P0**.
5. **Walk-forward gates were honest** — pull `last_train_details` from `ghost_state` (via `/api/v3/train/last` if surfaced, or admin endpoint). For the WOLF row: `wf_fold_count >= 3`, `wf_acc_mean >= 0.60`, `wf_acc_min >= V3_WF_ACC_MIN_OVERRIDES["WOLF"]` (or default), and `purge > 0` so the walk-forward is leakage-free. If any condition isn't met but the model still persisted: **P0**.
6. **Pick journal accrues new outcomes** — GET `/api/wolf/pick-journal?limit=10`. Confirm at least 1 resolved pick has `resolved_at > model.trained_at` (the model has been graded on its own post-train picks). Otherwise the learning loop is theoretical, not realized.
7. **Attribution diverging?** — GET `/api/wolf/attribution`. Confirm the feature win-vs-loss deltas are computed from ≥ 2 wins and ≥ 2 losses. If deltas are based on n=0/0 the learning signal is noise.

**Report:** last train ts, schema-guard verified (Y/N + line cite), self-heal verified (Y/N + line cite), `calibrated` value, fold details, resolved-since-train count, attribution n.

---

### PHASE 5 · BACKEND CORRECTNESS *(does the code do what its name says?)*

For each, the agent must run the test OR cite the test that already exists:

1. **Picks endpoint is WOLF-only sane** — `/api/picks` must not return random non-watchlist symbols, must not leak crypto-typed rows for WOLF symbol.
2. **Route registration regression** — `/api/v3/train` must map to `v3_train` (the trainer), not `_v3_train_collect_symbols` (a helper). Read `wolf_app.py` and verify the decorator placement OR cite the test `test_v3_train_route_maps_to_trainer_not_helper`.
3. **Kill conditions wired** — `/api/wolf/kill-status` must return all four conditions (win_rate, brier, consecutive_losses, expectancy) with current samples and status. Any condition `status: "red"` AND `triggered: true` while `engine_pause.paused: false` = **P0** (kill condition tripped but not enforced).
4. **Objective gate honesty** — `/api/wolf/gate-status` → `objective.mode`, `bootstrap_min_conf`, `min_samples`. Cross-check against `core/prediction.py` mode defaults. Note any env override.
5. **WolfContext sanity** — `/api/wolf/context` returns a populated structure (not all nulls). Empty = data chain broken upstream.

---

### PHASE 6 · FRONTEND / UI ACCURACY *(does the dashboard tell the truth?)*

This is the human-facing layer. **Visual + DOM + console must all agree with the backend payloads.**

1. **Render check** — Load `/cockpit` in a headless or real browser. Capture full-page screenshot for desktop (1440×900) **and** mobile (Pixel 7 / 390×844). Both saved as artifacts.
2. **DOM diff vs payload** — For each visible number on screen, fetch the source endpoint and confirm bit-for-bit match (allowing rounding only when the UI explicitly rounds):
   - Ghost Score gauge ↔ `/api/wolf/ghost-score.score`
   - Win rate strip ↔ `/api/cockpit/context.stats.post_v32.win_rate_pct`
   - Direction edge ↔ `/api/wolf/daily-forecast-scorecard.summary.direction_hit_rate_pct`
   - Expectancy ↔ `/api/wolf/pick-journal.metrics.expectancy_pct`
   - "If followed" return ↔ `/api/wolf/pnl.total_return_pct`
   - Open picks list ↔ `/api/picks.active[]` (count + each row)
   - Big/Steady tiers ↔ `/api/squeeze/picks.picks[]` filtered/sorted by `peak_move_pct`
   - WATCHING state, if shown, ↔ `gate-status.live_prediction.reason`
3. **Console hygiene** — DevTools Console must be **0 errors, 0 page errors, 0 CSP violations, 0 failed API responses**. Any non-zero = **P1**; CSP violation = **P0**.
4. **Toggle works** — Click "Show full dashboard". Every legacy section must reveal. Click again → all collapse. Capture before/after screenshots.
5. **Responsive** — Desktop ≥ 1280px, tablet 768px, mobile 390px. No horizontal scroll. Charts legible. Tiers stack.
6. **Honesty layer prominent** — The "research mode / not deployment-ready" pill, win rate, direction edge, expectancy, if-followed return all **visible without scrolling** on a 1080p desktop. Hiding bad numbers = **P0** (deceptive UX).
7. **Charts render** — Chart.js loads from `https://cdn.jsdelivr.net` (verify in Network tab); equity curve and outcome donut draw without errors when revealed.
8. **WebSocket** (`/ws/cockpit`) — Open or 404? If open, does it push updates? If absent, document.

**Report:** screenshot links/paths, payload↔DOM table, console error count, CSP violation count, responsive grades (D/T/M).

---

### PHASE 7 · ALERTS & NOTIFICATIONS *(is Ghost reaching the user out-of-band?)*

1. **Channels configured** — Telegram (`TELEGRAM_BOT_TOKEN` set?), Email (`SMTP_HOST` set?), SMS (`TWILIO_ACCOUNT_SID` set?). Record availability per `health` or env-presence check.
2. **Last alert delivered** — Find the most recent `predicted_at` with a fired pick. Confirm Telegram/email/SMS was attempted (log line) and not exception'd.
3. **Dedup intact** — Same `predicted_at` must not trigger multiple alerts. Inspect the daily-cap counter if surfaced.
4. **Withdraw / kill notifications** — If a kill condition tripped or a pick was withdrawn, was the operator notified?

---

### PHASE 8 · SECURITY & AUTH *(can a stranger break Ghost?)*

1. **Public surface is slim** — `/health` and `/api/health` must NOT leak: `telegram_configured`, `price_feeds.*`, `tasks`, `confidence_floor`, `dedup_blocked`, `predictions_freshness_min`. Cite test `test_health_public_is_slim`.
2. **Admin gated** — `/admin/health`, `/api/diagnostics`, `/admin` (without cookie), `/api/admin/*` → 404 or 403 unauth.
3. **Cron-gated POSTs reject without secret** — `/api/v3/train/sync`, `/api/run-predictions`, `/api/morning-card`, `/api/cron/signal-check`, `/api/clean-garbage` → 403 without `x-cron-secret`.
4. **Security headers** — Every response carries `Content-Security-Policy` (with `cdn.jsdelivr.net` allowed for Chart.js), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy`, `Strict-Transport-Security`.
5. **Docs disabled** — `/docs`, `/redoc`, `/openapi.json` → 404 unless `DOCS_ENABLED=1`.
6. **Rate limit** — `RATE_LIMIT_ENABLED=1` in prod; verify 429 trips above `RATE_LIMIT_RPM`. `/api/health` exempt.
7. **No secrets in HTML** — grep cockpit.html / admin.html source for `sk_`, `pk_`, `BEGIN PRIVATE KEY`, raw `CRON_SECRET=`. None should be present.

---

### PHASE 9 · TRUST-THE-MONEY GATE *(would a real human lose real money?)*

This phase is the report's reason for existing. Be ruthless.

1. **Realized win rate** — `/api/cockpit/context.stats.post_v32.win_rate_pct`. Above 55%? Above 60%? Above 70% (the kill threshold)? Below 50% = **engine is currently losing money on average**, even after fees.
2. **Direction edge over coin-flip** — `daily-forecast-scorecard.summary.direction_hit_rate_pct`. Above 53% with n ≥ 30 trials? If 50% ± noise, the model has **no demonstrated edge** — record this as a P0 trust issue regardless of UI quality.
3. **Expectancy positive** — `pick-journal.metrics.expectancy_pct` > 0 over the lookback window? If negative, the strategy loses money in expectation. P0.
4. **Brier calibrated** — `metrics.brier` < 0.25 = decent calibration; > 0.35 = badly miscalibrated; > 0.50 = "confidence has no relationship to outcome." Report the value plainly.
5. **"If you followed every pick"** — `/api/wolf/pnl.total_return_pct`, `profit_factor`, `max_drawdown_pct`. If `total_return_pct < 0` and the user is being shown predictions as actionable, the UI must say so loudly.
6. **Falsification / post-falsification mode** — `pick-journal.verdict.falsification.status`. If `ABANDON_80_CLAIM` or any "research mode" flag is set, the cockpit must visibly reflect that the 80% claim is **retired**, not just buried.
7. **Bootstrap vs steady-state** — `gate-status.symbol_stats.phase`. In `bootstrap` with `combined_total < min_samples`, every fire is on probation. Real money sizing on bootstrap fires = high risk; report so.
8. **Sample size honesty** — How many resolved high-conviction picks total? Any win-rate computed on n < 20 is **noise**, not signal.
9. **Operating-point sanity** — `confidence_floor`, `bootstrap_min_conf`. If either was loosened from documented design (e.g., floor dropped from 0.75 → 0.55), flag the drift and ask whether it was deliberate.
10. **Kill conditions ready to fire** — From Phase 5: if any condition is in `insufficient`, note that the safety net is **dark until sample sizes accrue**. The system can lose money badly during cold-start without auto-pause.

---

### PHASE 10 · TESTS, COMPILE, AND CI *(does the codebase even build?)*

1. `make test-compile` → must be clean.
2. `make test` (if pytest available) → record `N passed, M skipped, K deselected`. Any failure = **P0**.
3. CI on `main` HEAD → confirm `test` job last conclusion = `success`. Read `mcp__github__list_workflow_runs` if accessible.
4. Open PRs against `main` — list any. Note if any have CI failures (review-comments left there are someone else's intent).

---

### PHASE 11 · DOCS & LEDGER ALIGNMENT *(does what we tell agents match reality?)*

1. **`PROJECT_STATE.md` vs live** — list 3 specific claims in the doc and verify against live endpoints. Any divergence = P2 doc-rot.
2. **`LAUNCH_PROMPT.md` mode used** — record which version of this prompt was run (commit SHA + path) so the report is reproducible.
3. **Open PRs / branches with the words `launch`, `cockpit`, `display`** — list them; flag if anything is mid-flight that the operator should know about before trusting the verdict.

---

## 3. OUTPUT — Required structure of the report

The agent MUST produce a single report containing **ALL THREE** of these sections, in this order. No abridging, no "see above."

### Section A · BINARY VERDICT
One word, then one paragraph.

> **GO** / **NO-GO** / **NO-GO-WITH-CONDITIONS**
> One paragraph (≤ 6 sentences) explaining the verdict. Must cite Phase 9 numbers. May not be NO-GO without naming the specific Phase 9 finding(s) that drove it. May not be GO without explicitly stating that win rate, direction edge, expectancy, AND calibration are all in acceptable ranges *with sample size adequate*.

### Section B · GRADED SCORECARD
A table, one row per phase 1–11. Grade A / B / C / D / F. Evidence column required.

| Phase | Grade | Evidence (specific numbers, endpoints, line numbers, SHAs) | What would flip the grade up |
|---|---|---|---|

Grades are anchored:
- **A** — verified, all checks pass, sample size adequate.
- **B** — verified, minor issues that don't block trust.
- **C** — verified, real problems but not money-losing.
- **D** — verified, problems serious enough that a careful user would not trust this with real money.
- **F** — fails the phase outright OR insufficient evidence to grade.

### Section C · SEVERITY-RANKED PUNCH LIST
A flat list of every finding from every phase, sorted by severity. Format per finding:

```
[P0] <one-line title>
   Where: <endpoint / file:line / DOM selector>
   Evidence: <verbatim payload snippet, log line, or test result>
   Impact on trust: <one sentence>
   Fix sketch: <one sentence — what would resolve it>
```

Severity:
- **P0** — blocks launch. Engine is losing money, security hole, deploy broken, calibration off, kill conditions dark.
- **P1** — fix before launch. UI lies, stale data, missing alerts, doc-rot at critical paths.
- **P2** — fix soon. Cosmetic, perf, minor UX, low-priority data gap.
- **P3** — known limitation, acknowledged, not in scope to fix now.

### Section D · APPENDIX (mandatory)
- Repo + HEAD SHA + Prod SHA + Time + MCP state (from §1).
- Every endpoint queried + status code + age of data + 1-line summary.
- Screenshot file paths (desktop + mobile, default + dashboard-shown).
- `make test` exact tail output.
- Any divergence from `PROJECT_STATE.md`.
- One sentence the agent commits to: **"If this report is wrong, the consequence is a real human losing real money. I take that seriously and stake my verdict on it."**

---

## 4. RUN CHECKLIST FOR THE AGENT

Before emitting the report, confirm yes/no:

- [ ] Did I run **every** phase, not a subset?
- [ ] Did I verify against **live production**, not local mocks or memory?
- [ ] Did I include **both** backend AND frontend evidence for every "Ghost is working" claim?
- [ ] Did I cite **specific** numbers / SHAs / line numbers — no vague positives?
- [ ] Did I emit all three of Sections A, B, C, plus the Appendix?
- [ ] Did I refuse to mark anything GO if Phase 9 (Trust-the-money) didn't justify it?
- [ ] Did I report **any** divergence between `PROJECT_STATE.md` and live as a finding?
- [ ] Would I personally invest my own money on the strength of this report? If no, the verdict cannot be GO.

If any box is unchecked, the report is invalid. Redo the missing phase.

---

## 5. ESCALATION — When to stop and ask

Stop and use `AskUserQuestion` (do not guess) if any of these happen:

- Repo identity check fails (wrong repo or unrecognized origin).
- Production URL is unreachable from this environment.
- `git` is in a detached / dirty / conflict state you cannot characterize.
- A finding implies an immediate prod incident (engine paused, alerts misfiring, security hole live).
- A previously-shipped fix appears to have been reverted on `main`.
- The user's latest instructions conflict with what this prompt says to do.

The agent's failure mode is over-eagerness, not asking. When in doubt, ask.

---

## 6. WHY THIS PROMPT EXISTS

Ghost Protocol is being evaluated to handle a real human's real capital. Every prior round of testing in this codebase has had at least one place where "looks fine" turned out to be a deploy hiccup, a divergent branch, a stale model, a config drift, or a missing self-heal. This prompt exists so that **no agent picking up Ghost can declare it launch-ready without producing the receipts.** If the receipts say red, the report says red. If they say green, the report says green and cites them. There is no "I think it's working" tier.

---

*Last updated alongside the cockpit-facelift v2 (post-PR #74).*
*Run by saying:* **"ready launch prompt and execute report"**.
