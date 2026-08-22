# Ghost Protocol v2 — Independent Forensic Review

**Date:** 2026-08-22
**Reviewed at:** live `main` = `c73c318` (deploy v130-era), reconciled against the audited working tree
**Method:** 8 independent forensic agents (market data, signal engine, scoring/gates, squeeze, API/DB, background jobs, frontend, security/tests/deploy), each treating all prior agent work and PR descriptions as unverified and reading the code as the sole authority. All 5 pages were rendered headless; the app was booted and every endpoint the UI calls was exercised; the failing test set was reproduced; the highest-severity cross-agent claims were re-verified by hand before inclusion here.
**Scope correction:** the audit brief describes a *crypto sniper cockpit* (CoinGecko, VIP coins, XRP, wallets, Redis/SQLite). That is Ghost's **former** identity. The system today is a **US-equities prediction/advisory** service — FastAPI + raw psycopg2 Postgres, XGBoost engine, Alpaca/yfinance/Polygon/IEX/Finnhub data, a paper wallet (no real order execution), a squeeze radar, and Railway single-service deploy. This review audits what exists. Crypto-era remnants are noted where found.

---

## 1. Executive Summary

Ghost Protocol v2 is a genuinely sophisticated system with real, hard-won engineering discipline — and a measurement layer that cannot yet be fully trusted. The statistical *core* is sound: the Wilson lower-bound math is correct, the purged walk-forward has no fold leakage, labels are chronology-bound with no lookahead, and fundamentals are point-in-time by filed date. The gate stack that decides whether a pick fires is fail-closed and well-ordered. Prior forensic findings have been converted into named regression tests. None of this is a facade.

But the numbers the system *presents* — squeeze win/loss records, displayed confidence, RVOL, premarket gaps, "70% proven" — are compromised at multiple independent points, and the safety rails around them have gaps:

- **The squeeze daily win/loss ledger is biased by construction** (SQ-3): targets are set to the session high at alert time and then graded against the *full day's* OHLC including pre-alert action, so spike-and-fade alerts auto-grade as wins. The subsystem scored lowest of all (42/100).
- **Missing data is repeatedly converted into a bullish or high-confidence signal** rather than an "unknown": fabricated session volume produces RVOL up to ~156× exactly when volume data is absent (MD-3/SQ-4); Super Ghost synthesizes stop/target levels and scores them as real bullish evidence (SC-1); macro features cache fabricated neutrals for 24h (MD-7).
- **The phantom-price sanity check is bypassed on the two hottest price paths** (MD-1/MD-2/MD-4/SQ-7), so a wrong-security or 10×-off quote can flow into premarket gaps, the model's feature overlay, and persisted evidence.
- **Displayed confidence is not the calibrated number.** Raw model probability — documented by the codebase itself as non-discriminating above ~0.55 — still reaches Telegram cards as "% confident" and drives position-size tiers (SE-2/SC-5).
- **Three genuinely unauthenticated write endpoints** exist (`/api/studios/bookings`, `/api/my-picks` POST/DELETE, and a stored-XSS chain through the un-escaped studios page) (AD-1/ST-1/ST-2/FE-1).
- **Operational safety rests on unenforced conventions:** single-instance-by-omission (no scheduler leader lock), no market-holiday calendar (found independently by 4 agents), auto-deploy on push with CI as a parallel non-gate, and half the scheduled jobs swallowing exceptions so health reports green regardless of reality.

**The advisory-only design is the saving grace.** Nothing here places a real order, which caps the blast radius of every finding. But the product Ghost sells is *trustworthy advisory numbers with claimed provenance*, and several of those numbers can currently be stale, phantom, fabricated, or optimistically mis-graded while presenting as fresh and measured.

**Overall independent engineering score: 58 / 100** — "impressive and disciplined, but not yet production-trustworthy as a decision aid." The recent checklist-confidence and issuance-concurrency work (PRs #159/#160) is a real step up and already fixed several issues an earlier version of this review would have flagged; the remaining work is concentrated in data-integrity seams, not the architecture.

---

## 2. Overall System Assessment

| Dimension | Score | One-line justification |
|---|---:|---|
| Architecture | 66 | Clean subsystem separation, single fire-path, but a monolithic 3.5k-line `wolf_app.py` and in-process-only assumptions. |
| Market Data | 58 | Real breakers/batching/phantom-check, undone by bypasses at the hot paths and fabricated fallbacks. |
| Data Integrity | 48 | The measurement layer — grading, RVOL, prev-close, staleness — is compromised at several independent points. |
| Signal Engine | 64 | Leakage discipline is strong; the bugs are in feature parity, display, and calendars, not the math. |
| Ghost Score / Scoring | 60 | Super Ghost violates its own "unknown-never-bullish" contract; new checklist layer is honest. |
| AI / Fusion | 62 | Minimal LLM surface; AI brief is advisory and provenance-gated. Small, low-risk. |
| Portfolio / Wallet | 63 | Paper-only; N+1 reads, app-side dedupe for missing constraints, hardcoded personal positions in source. |
| API | 70 | Well-gated overall; one unauth write, route-shadowing, inconsistent error envelopes. |
| Backend | 62 | Solid pooling/retry, but connection leak on commit-failure and loop-transaction pattern in 4 modules. |
| Frontend | 62 | Contracts resolve; honesty gaps on the flagship page (error-as-"No", fake-live badge). |
| UI/UX | 60 | Four unrelated design systems; timezone-less timestamps; half-implemented ARIA. |
| Performance | 58 | Boot thundering herd, duplicate polling, full-table scans on hot public endpoints. |
| Security | 68 | Excellent crypto hygiene; deliberately-public writes, proxy-identity gap, secret reuse. |
| Testing | 72 | 1,500+ fast behavioral tests, exceptional stat rigor; zero contract/concurrency coverage. |
| Deployment | 55 | Works on a knife's edge of implicit assumptions; nothing enforced. |
| Maintainability | 60 | Good docs/ledger culture; heavy churn hotspots and duplicated logic. |

Churn hotspots (proxy for chronic pain): `core/squeeze_hunter.py` (15 rewrites), `core/signal_engine.py` (14), `wolf_app.py` (12), `ghost_console.html` (12). The squeeze subsystem's rewrite count matches its lowest score — repeated patching of a subsystem whose measurement foundation was never made correct.

---

## 3. Architecture Review

**Fire path (the only pick-saving route):** `run_prediction_cycle` (`core/prediction.py:1514`), reached via the scheduler market-scan job and three cron-gated endpoints. Kill enforcement runs before every save. Single INSERT site. This is the strongest part of the architecture — there is exactly one way a pick becomes real, and it is guarded.

**Subsystems & ownership:** market data (`core/prices.py`, `squeeze_monitor.py`) → signal engine (`signal_engine.py`, training+serving) → scoring (`super_ghost.py`, `ghost_score_spec.py`, new `catalyst_checklist.py`) → gates (`precision_gate.py`, `proven_skill_gate.py`, kill switch in `prediction.py`) → persistence (raw psycopg2 via `core/db.py`) → 30 scheduler jobs (`scheduler.py`) → 5 HTML pages.

**Single-source-of-truth violations:** two `_SIP_FORBIDDEN` state dicts (MD-13); triplicated Wilson implementation (`precision_gate`, `watcher`, `binomial_stats`); two win-rate denominators (SE-4); `HOLD_BARS` frozen at import in one module and re-read per-call in another (SE-13). None catastrophic, all maintenance landmines.

**What happens when a subsystem fails:** inconsistent. Some paths fail closed (auth, kill switch, reference-quote validation); many fail *soft into a fabricated value* (macro neutrals, session volume, synthesized stops) — the recurring anti-pattern of this codebase.

---

## 4. Market Data Review — score 58

Providers: spot = Alpaca trade → yfinance → Alpaca IEX; intraday = cached 5-min bars → Alpaca → yfinance; extended = raw Alpaca quote + yfinance. Breakers with rate-limit auto-open, multi-symbol batching, a phantom cross-check, and DB-backed prev-close cache are all real. The failings are at the seams (full findings in §25).

Headline: **MD-1/MD-2/MD-4 (P1/P2)** — the `_reject_phantom` guard is bypassed on `get_extended_session` and the `get_intraday_session` cache-hit overlay, and is circularly defeated by the unchecked `_iex_spot` fallback (the very IEX feed the guard exists to police). **MD-3 (P1)** — fabricated `session_vol = avg_vol * 0.4` yields RVOL up to ~156× premarket. **MD-7 (P2)** — macro features cache fabricated neutrals 24h with no availability flag. **MD-9 (P2)** — no market-holiday calendar.

PR #160 note: it added price *provenance labels* to `get_extended_session` but **not** the phantom check — MD-1 stands on live main.

---

## 5. Data Freshness Review

Ghost has partial freshness machinery (`price_as_of_ts`, `data_stale`, `snapshot_stale`, `last_scan_ts`) and some consumers honor it (`market_sessions`, `bull_run_checklist`). But:

- The documented all-providers-failed **stale-cache serve is dead code** (MD-8): `_cache_get` deletes the expired entry before the stale branch can read it, so `with_staleness=True` can never return a labeled stale price — it returns `None`.
- **No consumer of `get_stock_price` sees data age** (portfolio, war-room, super-ghost price checks call the bare function).
- The **flagship `/picks` page asserts freshness it lacks** (FE-4): the "Live" dot reflects only `/api/_version` reachability, the timestamp is the client clock advancing over frozen data, and each tab loads once and never refreshes.
- **Prev-close has three day-boundary bugs** (MD-5/MD-6/MD-15): first-premarket-bar-open used as "yesterday's close," positional `dbars[-2]` with no session-date check, and a 24h TTL with no session key — all shift gap%/change% by a full day during exactly the premarket window the squeeze radar cares about.

LIVE / DELAYED / STALE / UNAVAILABLE is **not** cleanly distinguished end-to-end. This is the review's second-weakest area (Data Integrity 48).

---

## 6. Signal Engine Review — score 64

**Verified clean** (stated because the brief demands proof, not assumption): Wilson bound hand-checked (7/10 → 0.3968 ✓); purged expanding walk-forward with per-fold sign maps and PIT peer masks — no fold leakage; labels use entry=bar close, forward bars strictly after entry, same-bar TP/SL collision → LOSS (conservative), incomplete horizons excluded; fundamentals filed-date PIT; `_normalize_daily_ohlcv` rejects NaN/inf/impossible/duplicate bars.

**Headline defects:** **SE-1 (P1)** train/serve feature skew — `above_ema200`/`ema_trend_bullish` fit on ~121 training bars (never hitting the n≥200 branch) but served on ~250, so the model learned a different feature than it's given; the schema guard can't see window length. **SE-2 (P1)** confidence has a hard 0.75 floor and ×4 slope over a probability the code itself calls non-discriminating, and that number sizes positions. **SE-3/ST-5 (P2)** `CalibratedClassifierCV(cv="prefit")` breaks silently on sklearn ≥1.6, falling back to *raw* probabilities with all gates still passing (production pinned safe at 1.5.2; one bump away). **SE-4 (P2)** two win-rate truths — gates count EXPIRED as non-win, user/health surfaces don't.

---

## 7. Ghost Score Review — score 60

Two scoring surfaces beyond the model: the WOLF-only Ghost Score v1.0 (`ghost_score_spec.py`) and the 25-point Super Ghost checklist (`super_ghost.py`).

**SC-1 (P1):** Super Ghost synthesizes missing stop/target as `current*0.95`/`current*1.10` and then scores the synthetic plan as real bullish evidence — a symbol with nothing but a price collects ~5 "available" bullish items (edge 0.41 vs the 0.18 UP threshold) **and** inflates its own coverage/data-quality toward the 18-of-25 A/B gate. Missing data literally makes a stock look more bullish and better-covered — a direct violation of the module's own "unknown never becomes bullish" contract.

**SC-3 (P2):** guidance events are counted twice (guidance-momentum item **and** catalyst item, from overlapping event sets). **SC-4 (P2):** non-directional facts (liquidity, unlocked loss-limit, exposure present) are scored as positive bullish edge, so every liquid stock starts ~+2.2 bullish before any real signal. **SC-12 (P3):** Ghost Score's volume component is direction-agnostic, so a high-volume selloff adds the maximum "bullish" points.

Reproducibility (SC-10): identical snapshots do **not** yield identical scores — live event/risk/DB-profile reads leak in even when a snapshot is supplied.

---

## 8. AI / Fusion Review — score 62

The LLM surface is small and appropriately fenced. `generate_ai_brief` (`super_ghost.py`) is advisory narration, gated behind MCP/OAuth auth on the `?ai=1` path, and does not override hard market-data constraints. Evidence-integrity primitives (`evidence_integrity.py`) explicitly treat "narrative text, AI output, and operator submissions as claims, never evidence" and require a CONFIRMED machine-verifiable chain before a signal moves — this is the right posture and it is enforced in the checklist path. No prompt-injection-to-signal path was found. Low risk; not a source of the system's problems.

---

## 9. News / Sentiment Review

`news_events.py` classifies with source-reliability weighting (filings > wire > aggregators) and stores source/timestamp/asof. Gaps: EDGAR non-WOLF symbol mapping is a silent dead end (MD-14 — any non-WOLF CIK returns `[]`, indistinguishable from "no filings"); earnings-surprise pulls yfinance **outside** the breaker (MD-12), so a Yahoo 429 storm both keeps hammering and degrades to `earnings_surprise=0.0` cached 1h with no distinction from "no surprise." Short-interest freshness is unbounded (SQ-11): `shortPercentOfFloat` is FINRA bi-monthly data ~2 weeks stale at publish, the `dateShortInterest` as-of field is never read, and squeeze fuel (40 pts SI + 25 DTC) can run on 4–6-week-old data.

---

## 10. VIP / Watchlist Review

No crypto "VIP coins" remain in the live path (crypto-era `asset_type` handling lingers in `prediction.py` but stock-gated). The equities watchlist is env/config driven (`config/symbols.py`). No hardcoded price assumptions found in the watchlist itself — but see ST-17: a **hardcoded personal brokerage position list** (symbols, quantities, P&L) lives in `core/portfolio_routes.py` and a gated admin route **DELETEs the entire `user_portfolio` table** before reseeding from that stale 2026-06-04 snapshot.

---

## 11. Portfolio / Wallet Review — score 63

Paper wallet only — **no real broker, no order execution** (confirmed). `wallet_summary` returns `total_value`/`starting_balance`/`total_pnl` correctly. Risks: N+1 reads (AD-12 — per-position price fetch + fresh pool checkout; my-picks worst case does 30 `build_super_ghost` builds in one request); no `UNIQUE(symbol)` on `user_portfolio` so duplicate lots are deduped in Python on every read (AD-14); the public my-picks writes (ST-1) let anyone mutate the watchlist and trigger the super-ghost DoS amplifier.

---

## 12. Alert System Review

Telegram alerts have a DB-backed dedupe and dead-letter queue. But: **BG-2 (P1)** the send path (`requests.post` timeout=10 + `time.sleep` backoff ×3) runs **synchronously on the event loop** from the async squeeze scan — one Telegram outage freezes the entire app ~40s. **BG-8 (P3)** weekly summary claims its dedupe slot without checking send success (a transient outage silently loses the week — the exact bug PR #80 fixed for the morning card, still live in the weekly path). **SQ-13** duplicate ledger rows per restart because in-memory cooldown resets but the DB dedupe returns True. **BG-4** no leader election means deploy overlap can double every alert.

---

## 13. Background Worker Review — score 58

30 scheduler jobs; the scheduler itself is better than most (overlap guard, per-task timeout with shield-on-timeout, DB-persisted dedupe keys, idempotent `ON CONFLICT` in the newest jobs). But:

- **BG-1 (P1):** double morning-card on a morning-window restart — the startup-recovery executor call and the scheduler's first tick (every task `last_run=0` → all due at boot) both run the cycle; the loser sends a contradictory SILENCE card.
- **BG-3 (P2):** boot thundering herd (all 30 jobs fire on tick one → provider 429 storms), and the morning card has no hour gate in the job itself, so it sends "at whatever time the container booted" and drifts.
- **BG-5 (P2):** ~half the jobs wrap exceptions in swallowing shims, so `scheduler.status()` reports `healthy:true` forever and the health endpoint only checks morning-card age. **A job failing 100% of the time is invisible.**
- **BG-6 (P2):** `create_task` results are never stored (weak refs) — the whole background system can be GC'd or die silently with no health signal for up to a day.

---

## 14. API Review — score 70

~244 routes, 4 auth tiers, constant-time secret compares, fail-closed defaults, production boot guard. Frontend/backend contracts for all 5 pages resolve to real endpoints with matching field names. Defects:

- **AD-1 (P1):** `POST /api/studios/bookings` — **verified fully unauthenticated** (`Header` imported but never used); anyone can insert arbitrary revenue rows.
- **AD-2 (P2):** `db_conn.__exit__` has no try/finally — a commit/rollback failure permanently leaks a pool slot (max 25).
- **AD-3 (P2):** pool sized 25 vs ~70-thread worst case; exhaustion surfaces as opaque **500** (PoolError isn't mapped to 503).
- **AD-4 (P2):** `/mcp/tools` is route-shadowed by `/mcp/{path_token}` and unreachable — the same bug class caught in PR #159's own review.
- **AD-5/AD-6 (P2/P3):** two more instances of the loop-transaction pattern (alert sends and squeeze resolution inside one transaction → one bad row rolls back the batch, and holds a connection across network I/O).
- **AD-7 (P3):** the rate limiter **exempts** the cron-secret endpoints — the "whole security boundary" is brute-forceable without throttle.
- **AD-8 (P3):** `/api/picks` pagination `total` counts WIN+LOSS only while the page includes EXPIRED → silent truncation.

---

## 15. Database Review

Raw psycopg2 + `ThreadedConnectionPool`. **DDL-at-runtime everywhere** (123 `CREATE/ALTER` sites, some inside public GET handlers — AD-10/ST-14): catalog-lock overhead on hot paths, first-hit races, no migration versioning or rollback, destructive self-healing at boot. Missing unique constraints where duplicates are *known* to occur (`user_portfolio`, `ghost_feature_snapshots` — AD-14). The connection-leak-on-commit-failure (AD-2) and the loop-transaction pattern (SQ-2/AD-5/AD-6/AD-18/SQ-14) are the two DB-layer patterns most likely to cause a production incident. Notably, the newest tables (`ghost_checklist_snapshots`) are the *best* modeled — partial unique index on `prediction_id`, cohort identity, `WHERE outcome IS NULL` update guards (PR #160).

---

## 16. Frontend Review — score 62

Every fetch resolves to a real endpoint; every field name traced matches. `picks.html`/`ghost_console.html` escape consistently. But `cockpit.html` (143 innerHTML sinks) and `admin.html` (170) have **no escape helper at all** (ST-7), under a CSP that allows `'unsafe-inline'`. Flagship-page honesty gaps (all in the page shipped by PR #159): **FE-2** EXPIRED narrated as stop-out; **FE-3** backend 500 renders a confident "No."; **FE-4** fake-live badge over never-refreshed data; **FE-5** squeeze alert boilerplate (`p.reason`/`p.note` don't exist on the rows). Dead nav link to `/legacy-picks` (FE-6). These are owned by this review's own prior work.

---

## 17. UI / UX Review — score 60

Four unrelated design systems across five pages (defensible for operator consoles, but `picks.html` shows unlabeled **client-local** time on a US-market product where `cockpit.html` correctly labels CT — FE-14). `studios.html` resets to the Overview tab every 60s (skeleton flash wipes the user's selected tab — FE-10). `picks.html` claims the WAI-ARIA tabs pattern but implements no arrow-key navigation or roving tabindex (FE-9). Error-as-empty-state on My Picks with no add affordance on the consumer page (FE-7).

---

## 18. Performance Review — score 58

Boot thundering herd (BG-3/AD-3). Duplicate polling on `/cockpit` (`loadSqueezePicks` fires ~every 45s on two overlapping schedules with no reentrancy guard — FE-8). Full-table `SELECT *` feeding `_norm_pred` on the hottest public endpoints, dragging JSONB blobs for 14 scalar keys (AD-13). Unbounded public journal metric scan (AD-13b). Per-request calibration full-table scan was **fixed** by PR #160 (short-TTL cohort cache). N+1 portfolio/my-picks reads (AD-12).

---

## 19. Security Review — score 68

**Strong:** `hmac.compare_digest` everywhere, fail-closed auth, boot guard, login throttle, security headers, gated destructive surface, clean current tree. **Weak:**

- **ST-1/ST-2 (P1):** two deliberately-public write endpoints (my-picks, studios), the latter an unauth **stored-XSS chain** (verified: `${b.source}`/`${b.status}` interpolated raw into innerHTML + ungated POST + `unsafe-inline` CSP).
- **ST-3 (P2):** rate-limit + login-throttle key on `client.host` with no `--proxy-headers`/`FORWARDED_ALLOW_IPS` — behind Railway's proxy this likely collapses **all clients into one bucket** (one abuser 429s everyone; 5 failed logins locks the operator out).
- **ST-4 (P2):** `GHOST_MCP_TOKEN` accepted as a **URL path segment** → written into access logs on every request.
- **ST-10 (P3):** `CRON_SECRET` is triple-purposed (cron header, admin password, cookie signing key) — one compromise is total, and rotation nukes all sessions + integrations at once.
- **ST-13/ST-16:** SSRF DNS-rebinding TOCTOU on the one user-URL fetch (OAuth CIMD); a real `GHOST_OAUTH_SECRET` remains in git **history** (rotated per ledger, but never rewritten).

---

## 20. Testing Review — score 72

1,517 selected tests, **1,495 pass / 5 permanent reds** reproduced here. Rigor on statistics (Wilson exact values incl. the 76/96→0.7000 rounding trap), breakers, dedupe, expiry honesty. **The five diagnostic questions:** wrong Wilson bound → *caught*; stale-but-plausible provider data → *not caught*; 429 → *partial*; two concurrent schedulers → *not caught*; frontend/backend field contract → *not caught* (the "UI tests" grep static HTML; the only real contract test is the **manual, post-deploy, prod-targeted** Playwright suite — `e2e.yml` is `workflow_dispatch`, confirmed).

**The 5 permanent reds are environment drift, but one pair masks a real defect:** 3 are FastAPI-version route-introspection changes (routes exist; the test idiom is version-bound); 2 are the sklearn `cv="prefit"` removal — which *is* the SE-3/ST-5 silent-uncalibration defect-in-waiting. **A permanently-red CI trains people to skim failure lists** — pin the dev environment (constraints + a conftest version assert) so a 6th, real red is visible.

---

## 21. Deployment Review — score 55

Works, on unenforced assumptions: **single-worker by omission** (Procfile has no `--workers`; nothing prevents a `replicas:2` click from duplicating 30 schedulers + splitting the rate limiter — ST-8/BG-4); **auto-deploy on push with CI as a parallel non-gate** (docs pushes trigger full rebuilds; a `cache_bust` comment in `nixpacks.toml` documents past **stale-container** incidents); **Python 3.13.13 + numpy 1.26.4** with no cp313 wheel → **source build** (verified) requiring gcc, so dev(3.11) ≠ CI ≠ prod(3.13); runtime DDL as the migration strategy with destructive boot-time self-healing; **no repo-declared health check** (`railway.toml`/`json` absent).

---

## 22. Code Quality Review

Above-average for a system this size: honest inline documentation of past failures, a disciplined `PROJECT_STATE.py` ledger, prior findings turned into named tests. Weaknesses: a 3.5k-line `wolf_app.py` monolith; the recurring "fail-soft-into-fabricated-value" idiom; duplicated logic (2× SIP state, 3× Wilson, 2× win-rate denominator); dead code (66 unreachable lines after a `return` in `debug_signal`, the `wol f_price` typo key, dead `conditionBadge`/`(found?…)` branches).

---

## 23. Technical Debt Review

The load-bearing debt, ranked by how likely it is to cause a wrong number:

1. **No market-holiday calendar** — surfaced independently by 4 agents (MD-9, BG-9, SQ-15, SE-5). Weekday-only gates mean holiday scans, phantom session rows, one-day-off expiries.
2. **Loop-transaction-with-network-I/O** pattern in 5 modules — silent batch rollback + connection starvation.
3. **DDL-at-runtime** as the entire schema strategy.
4. **In-process-only concurrency assumptions** (scheduler, caches, rate limiter) with nothing enforcing single-instance.
5. **Exception-swallowing wrappers** that fake scheduler health.

---

## 24. Imperfection Review (Category B — works, but should improve)

Beyond the bugs: four-design-system inconsistency; timezone-less trading timestamps; half-implemented ARIA; duplicate polling; error-as-empty-state; identity-function XSS fallback in admin; dead nav/links; `_REFERENCE_MAX_AGE_S` dead threshold (SQ-12); `cs_*` cross-sectional feature columns permanently 0.0 in live training (SE-8 — 8 columns of "signal" the model has never seen); `hour_of_day` feature dependent on which feed answered (SE-14).

---

## 25. Findings by Severity

**P1 — Critical (13).** Financial-record corruption, misinformation, or a broken core guarantee.

| ID | Location | One-line |
|---|---|---|
| SQ-3 | `squeeze_outcomes.py:250` + `squeeze_monitor.py:230` | Squeeze WIN/LOSS graded against full-session OHLC incl. pre-alert; spike-and-fade auto-wins. |
| MD-3 / SQ-4 | `squeeze_monitor.py:800,872` | Fabricated `session_vol=avg_vol*0.4` → RVOL up to ~156× when volume is missing. |
| SC-1 | `super_ghost.py:694` | Missing stop/target synthesized and scored as real bullish evidence + coverage. |
| SE-1 | `signal_engine.py:1101,2594` | Train/serve feature-semantics skew on EMA200-class features. |
| SE-2 | `conformal_calibration.py:109` | Confidence 0.75 floor + ×4 slope over a non-discriminating prob; sizes positions. |
| MD-1 / MD-2 | `prices.py:398,377` | Phantom-check bypassed on extended session; re-admitted via unchecked `_iex_spot`. |
| SQ-1 | `squeeze_hunter.py:721` | `breakout_pct` == daily gain; pollutes calibration under an un-bumped version. |
| SQ-2 | `squeeze_hunter_ledger.py:578` | Resolver rolls back the whole batch on one bad row while reporting `resolved>0`. |
| BG-1 | `wolf_app.py:1401,1408` | Double morning-card on morning-window restart (contradictory Telegram cards). |
| BG-2 | `squeeze_monitor.py:554` | Synchronous Telegram send freezes the event loop ~40s during an outage. |
| AD-1 / ST-2 / FE-1 | `routes_studios.py:40` | Unauthenticated booking write + stored-XSS through un-escaped `studios.html`. |
| ST-1 | `portfolio_routes.py:358` | Public my-picks POST/DELETE + `build_super_ghost` DoS amplifier. |

**P2 — High (≈24).** MD-4,5,6,7,9,10; SE-3,4,5,6; SC-3,4,6,7; AD-2,3,4,5; BG-3,4,5,6; ST-3,4,5,6,7,8; SQ-5,6,7,8; FE-2,3,4. (Full 9-field detail per finding is preserved in the per-subsystem agent outputs under the session task directory; each row above/below carries its ID for lookup.)

**P3/P4 — Medium/Low (≈60).** Enumerated across §§4–24 by ID.

**Total: ~100 distinct findings across 8 subsystems.**

---

## 26. Recommended Fix Order

**Tier 0 — external exposure (do first, hours):**
1. Auth-gate `POST /api/studios/bookings` and escape `studios.html` (AD-1/ST-2/FE-1).
2. Set `MY_PICKS_REQUIRE_AUTH=1` or gate POST/DELETE (ST-1).
3. Set `FORWARDED_ALLOW_IPS`/`--proxy-headers` so rate-limit + login-throttle see real client IPs (ST-3).
4. Stop accepting `GHOST_MCP_TOKEN` in the URL path / redact `/mcp/*` in access logs (ST-4).

**Tier 1 — measurement integrity (this is the product):**
5. Grade squeeze outcomes against **post-alert** bars only; require target > alert-time high (SQ-3).
6. Never fabricate session volume — skip the symbol / force RVOL=0 (MD-3/SQ-4).
7. Run `_reject_phantom` on the extended + intraday-cache-hit + `_iex_spot` paths (MD-1/2/4/SQ-7).
8. Stop scoring synthesized stop/target as evidence; unknown stays unknown (SC-1).
9. Make the squeeze hunter resolver propagate errors (per-row savepoints) and count `resolved` only post-commit (SQ-2).

**Tier 2 — safety rails:**
10. Add an NYSE holiday/half-day calendar consulted everywhere (MD-9/BG-9/SQ-15/SE-5).
11. Scheduler advisory lock (single leader) + delete exception-swallowing wrappers + surface real job health (BG-4/BG-5/ST-8).
12. `db_conn.__exit__` try/finally; map PoolError→503; raise pool size or cap threads (AD-2/AD-3).
13. Route every win-rate query through one denominator helper (SE-4); display the **calibrated/live-recal** number, not raw `up_prob`, and size positions off realized bin rates (SE-2/SC-5).
14. Make sklearn calibration failure **loud** (health-critical), and version-branch to `FrozenEstimator` (SE-3/ST-5).

**Tier 3 — hygiene:** fix the loop-transaction pattern repo-wide; pin the dev environment so CI has zero permanent reds; consolidate DDL into a boot migration; gate deploys on CI.

---

## 27. High-Risk Areas

1. **The squeeze subsystem** (42/100) — every measurement it produces is suspect until SQ-1/2/3/4/6 are fixed.
2. **Price-provenance seams** — phantom bypass + prev-close day-boundary bugs + fabricated volume feed the model's own feature overlay and persisted evidence.
3. **The kill switch's manual-resume grace** (SC-7) — an unbounded, silent bypass; serial daily resumes keep a losing engine firing with no alert.
4. **Single-instance-by-convention** — the first `replicas:2` or `--workers 2` breaks alerting, training, resolution, and rate limiting simultaneously.

---

## 28. Quick Wins (high value, low effort)

- Gate the two public writes + escape studios (Tier 0 #1–2).
- Delete the exception-swallowing scheduler wrappers — the scheduler already counts errors; this instantly makes health honest (BG-5).
- Fix `picks.html` FE-2/FE-3/FE-5: read `s.outcome`, render an error block when a fetch fails, compose the squeeze detail from fields that exist. (Small, and they're this review's own bugs.)
- Map PoolError→503 (AD-3) and add the try/finally to `db_conn` (AD-2).
- `settings.py` `KILL_WINRATE_FLOOR` says 0.70 but the effective default is 0.45 with Wilson-upper semantics (SC-8) — align the number with reality or wire Settings into `_kill_cfg`.

---

## 29. Architectural Improvements

- Extract the scheduler + monitors into a design that tolerates >1 instance (advisory-lock leader, or a dedicated worker process) — removes an entire class of risk.
- Introduce a real migration tool (alembic) and forbid DDL in request handlers.
- Establish **one** freshness contract (`{value, as_of_ts, source, status∈{live,delayed,stale,unavailable}}`) that every provider returns and every consumer must handle — this single change closes most of §5.
- Collapse the two win-rate denominators and the triplicated Wilson into shared helpers.
- Split `CRON_SECRET` into distinct cron / admin-password / cookie-signing secrets.

---

## 30. Production Readiness Assessment

**As an advisory research tool for its single operator, run on one Railway instance, with the operator understanding that the squeeze record and displayed confidence are not yet trustworthy: usable today, with eyes open.**

**As a system whose numbers a person would act on with real money: not ready.** The measurement layer must be made honest first (Tier 1), the three public writes closed (Tier 0), and the single-instance assumption either enforced or removed (Tier 2 #11). None of this requires re-architecture — the bones are good and the fire-path is genuinely well-guarded — but the gap between "impressive" and "trustworthy" is exactly the ~35 P1/P2 findings above, and most of them share five root causes: fail-soft-into-fabrication, no holiday calendar, loop-transactions, in-process-only assumptions, and raw-probability-as-confidence.

---

## Subsystem Scorecard (0–100)

```
Architecture      66   Portfolio/Wallet  63   Security       68
Market Data       58   API               70   Testing        72
Data Integrity    48   Backend           62   Deployment     55
Signal Engine     64   Frontend          62   Maintainability 60
Ghost Score       60   UI/UX             60
AI/Fusion         62   Performance       58        OVERALL:  58 / 100
```

---

## TOP 10 FAILURE RISKS — "if I ran Ghost with real money today"

1. **Act on a squeeze "win rate" that is inflated by construction** (SQ-3) — spike-and-fade alerts are graded as wins against the full-day high, so the historical record that makes a signal look good is measuring the wrong thing.
2. **Chase a fabricated volume spike** (MD-3/SQ-4) — a thin premarket name with any gap fires an alert on RVOL of "156×" that is really `0.4/elapsed` invented from average volume.
3. **Trade a phantom price** (MD-1/2/4/SQ-7) — a wrong-security or 10×-off Alpaca IEX quote flows into the premarket gap, the model's feature overlay, and the persisted reference price, unchecked.
4. **Size a position on a confidence number that doesn't mean what it says** (SE-2/SC-5) — raw `up_prob` in the 0.55–0.70 band, shown as "% confident" and mapped to 2%→5% sizing, despite the codebase documenting it as non-discriminating.
5. **Believe a bullish read that is really missing data** (SC-1) — Super Ghost turns absent stop/target/risk info into ~+4 bullish edge and higher coverage, so *less* information produces a *more* confident BUY.
6. **Miss that the engine silently stopped calibrating** (SE-3/ST-5) — one dependency bump flips the whole fleet to raw probabilities with every gate still green and no alarm.
7. **Keep a provably-losing engine firing** (SC-7/SC-8) — the kill floor is effectively ~28% realized (not the advertised 70%), and each manual resume grants a fresh silent 24h grace with no cap.
8. **Read a green dashboard while a subsystem is dead** (BG-5/BG-6/ST-6) — half the jobs swallow exceptions, `create_task` handles are dropped, and health only checks morning-card age.
9. **Have your business/watchlist data corrupted by anyone on the internet** (AD-1/ST-1/ST-2/FE-1) — three unauthenticated write endpoints, one a stored-XSS chain.
10. **A single deploy or scaling change doubles alerts, training, and resolution** (BG-4/ST-8) — single-instance is an assumption, not an enforced invariant, and stale-container deploys are a documented past incident.

*Advisory-only framing bounds every one of these to bad information rather than an unwanted trade — but bad information is precisely the product's failure mode.*

---

*Prepared by an independent forensic review. All P1 claims and the highest-severity cross-agent findings were re-verified by hand against live `main`. Per-finding 9-field detail (problem / why-it-matters / root-cause / evidence / fix / verification) is preserved in the eight subsystem audit outputs.*
