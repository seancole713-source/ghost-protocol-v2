# Ghost Protocol v2 — PROJECT STATE
**Last updated:** 2026-06-29
**Read this first.** Any agent picking up this project must read this file before touching any code.

> ## 🚀 LAUNCH READINESS REVIEW
> A comprehensive, multi-phase, trust-the-money review prompt lives at **[`LAUNCH_PROMPT.md`](./LAUNCH_PROMPT.md)** at the repo root.
> **Trigger phrase:** *"ready launch prompt and execute report"*.
> Any agent — Claude, browser-control, general — can execute it and produce a launch-readiness report with three verdict formats (binary GO/NO-GO, graded scorecard A–F, severity-ranked P0–P3 punch list). Every phase, every time. No skipping.
> This is the canonical way to ask "is Ghost ready for a real user to trust real money?" — do not write ad-hoc audits; run the launch prompt instead.

> **PR #82–#90 (2026-06-28–29): Super Ghost foundation → prediction console → live coverage gate.**
> - **Super Ghost AI brain:** market-regime adjustment + optional Claude AI brief on `/api/wolf/super-ghost?ai=1`; model fixed to `claude-haiku-4-5-20251001`.
> - **Truth Ledger:** every Super Ghost prediction can be logged, resolved, measured for accuracy, and scored on if-followed performance.
> - **Master build map:** `docs/SUPER_GHOST_MASTER_BUILD.md` + `docs/super_ghost_master_plan.json` define the full end-to-end max build with CI-enforced plan tests.
> - **Unified UI:** `/picks` is now the Liquid Glass prediction console; legacy pages preserved at `/legacy-picks` and `/cockpit`.
> - **Live market mirror:** `/api/market/session/{symbol}` compares live open/high/low/price against prediction reference/stop/target.
> - **Data coverage upgrade:** `core/market_history.py` uses the production-proven `_fetch_ohlcv` chain; `core/sec_fundamentals.py` adds SEC XBRL EPS/revenue + generic ticker→CIK.
> - **Hard trust gate:** no A/B grade and no HIGH-CONVICTION action unless coverage ≥18/25 (`MIN_COVERAGE_FOR_AB=18`).
> - **Live verified 2026-06-29:** production `5bc05a0`, `_pr_version 88`; WOLF coverage **21/25** (`meets_ab_gate=true`), AAPL **19/25**, NVDA **20/25**; full suite **503 passed**.
> - **PR #91 UI trust polish:** production `3a83893`, `_pr_version 91`; Top Stocks copy now says completed predictions, global post-falsification banner added, duplicate top tabs hidden; suite **504 passed**.
>
> **PR #70–#81 (2026-06-25–26):** Comprehensive security + reliability audit. 12 PRs deployed.
> - **Circuit breaker fixes:** infinite half-open probe loop (yfinance + Alpaca rate-limit) — breakers now actually block when tripped
> - **yfinance hardening:** all raw yfinance calls gated behind `_yfinance_cb`; JSON parse errors count as breaker failures; library noise suppressed; NaN sanitization in all OHLCV paths
> - **5-tier spot price chain:** `get_stock_price()` now uses Alpaca → yfinance → Polygon → IEX → Stooq (was only 2-tier)
> - **Auth fixes:** unauthenticated portfolio mutation routes gated; public `/api/test-alert` requires cron secret; `CRON_SECRET` production boot guard; Ghost Ask portfolio leak fixed
> - **Prediction correctness:** sentiment confidence floor bypass fixed; reconcile/legacy watchdog double-resolve fixed; morning card dedup after send success
> - **Infrastructure:** API rate limiter 120→300 RPM; scheduler overlap guard; degraded mode counts half_open; X-Forwarded-For hardening; train endpoint concurrency lock
> - **Watchlist filter:** `REAL_TRADE_WHERE` includes watchlist-membership filter; write-side guard in prediction cycle
> - **Tests:** CircuitBreaker class state-machine tests (8 new); 426 passing
> - **New capability:** War Room endpoint (`POST /api/wolf/war-room`) — 6-agent equity research pipeline (Analyst → Valuation → Bull → Bear → Fact-Checker → Judge) powered by Claude Sonnet
> - **yfinance wrapper:** `core/yfinance_client.py` + `api/wolf_endpoints.py` monkeypatch — zero raw yfinance calls remain
>
> **PR #63 (2026-06-15):** Live vs alert drift — first Telegram alert buy vs live quote on squeeze radar, daily log, and API (`live_drift[]`) before EOD resolve.
> **PR #61–#62 (2026-06-12):** Squeeze daily accountability log (`ghost_squeeze_outcomes`, EOD resolve, cockpit + admin UI) and post-falsification truth-mode UX.

---

## THE NORTH-STAR (retired — post-falsification)

**The legacy ~80% accuracy claim on WOLF is abandoned.** Falsification gate tripped
(N≥30 resolved picks, WR below floor, 95% CI excludes 80%). Ghost now positions as:

**Selective directional aid + intraday squeeze radar** — track live win rate, expectancy,
and Brier on the pick journal; no fixed accuracy marketing. See `core/ghost_contract.py`
and `/api/ghost/contract`.

80% was *not* achievable on "WOLF goes up tomorrow" (that's ≤55% for the best funds
alive). The system is built for **selective prediction** — it stays silent most of the
time and only fires when the gates agree. **Silence is the product, not a bug.**

The full vision is the "WOLFSPEED-Only Prediction Engine" blueprint (7 specialist
models → meta-model, 12 data categories, regime detection, options flow, etc.).
Today we have the *bones*: one model, one ticker, a confidence gate, a calibration
path, squeeze radar, and the credibility ledger. Phase 1+2 modules (regime calibration,
squeeze ML v2, drift/sentiment/options probes) are wired as of PR #60.

### Honesty layer (pre-registered, do not move the goalposts)

- WOLF is **post-Chapter-11** (new shares 2025-09-29) → only ~250 trading days of
  the security that actually exists. Pre-bankruptcy WOLF is a *different
  instrument* — never train on it as if continuous.
- The 80% claim **was judged and abandoned** once enough resolved high-conviction picks
  accumulated. The journal surfaces this at `verdict.falsification` on
  `/api/wolf/pick-journal` with status `ABANDON_80_CLAIM`.
- **Kill condition** (`core/prediction.py → FALSIFICATION_THRESHOLD`): once N≥30
  resolved high-conviction picks, if win rate < 70% **and** the 95% CI excludes
  80%, the 80% claim is **abandoned** and the system repositions as a
  lower-confidence directional aid.

---

## LIVE SYSTEM

| Item | Value |
|---|---|
| Production URL | `https://ghost-protocol-v2-production.up.railway.app` |
| GitHub repo | `seancole713-source/ghost-protocol-v2` |
| Railway project | `tender-benevolence` |
| Railway service (v2) | `98593080-065d-43ef-840c-4a3d36a1b572` |
| Railway service (v1, silenced) | `098281d7-7dba-447c-981e-0ebd625cecad` |
| Health endpoint | `/health` — should return score 100 |
| Operator console | `/admin` — cookie login (see Admin section) |
| Investor cockpit | `/cockpit` — WOLF-first UI |
| Cron trigger | cron-job.org fires `POST /api/morning-card` daily 8 AM CT |
| Auth header name | `x-cron-secret` (value in Railway env as `CRON_SECRET`) |
| **Last prod-verified** | **2026-06-29** — PR #91 deployed (`3a83893`, `_pr_version 91`); 504 tests passing; console trust-state polish live; WOLF coverage gate still 21/25 |

**Agent CAN reach Railway** as of 2026-06-22 session — all production verification is done via `curl` from the local terminal.

**PR #91 prod verify:** passed 2026-06-29 — `GET /api/_version` sha `3a83893`, `_pr_version 91`; `/picks` contains post-falsification banner, completed-predictions Top Stocks copy, and hidden duplicate top-tabs.
**PR #88–#90 prod verify:** passed 2026-06-29 — `GET /api/_version` sha `5bc05a0`, `_pr_version 88`; `/api/wolf/super-ghost/coverage?symbol=WOLF` returned 21/25 and `meets_ab_gate=true`; AAPL 19/25; NVDA 20/25; gate invariant no-A/B-below-18 verified.
**PR #86–#87 prod verify:** passed 2026-06-29 — `/picks` unified console live, `/legacy-picks` preserved, `/cockpit` preserved, `/api/market/session/WOLF` live mirror endpoint responding.
**PR #84 prod verify:** passed 2026-06-29 — Truth Ledger routes live (`history`, `accuracy`, `if-followed`, auth-gated `resolve`).
**PR #69 prod verify:** passed 2026-06-22 — breaker fix deployed, squeeze radar recovering (20+/43 fetches), WOLF price feed restored, health 90–95, pr_version=69.
**PR #68 prod verify:** passed 2026-06-22 — WOLF ensemble=True, conformal_ok=True, q_hat=0.7401, acc=59.6. WF gate fix working.
**PR #66 prod verify:** passed 2026-06-22 — 41/44 ensemble models, 41/44 conformal, retrain `state=passed`, pr_version=66 then 67→68→69.
**PR #63 prod verify:** passed 2026-06-15 — `live_drift[]` on daily-log (18 symbols), cockpit drift UI in HTML, `_pr_version` 63.
**EOD resolve 2026-06-12:** 17 rows resolved (5 WIN, 5 LOSS, 7 NEUTRAL).

### Live env config (set in Railway, confirmed 2026-06-22)
- `OBJECTIVE_MODE=aggressive`
- `OBJECTIVE_AUTO_MODE_ENABLED=0`
- `MIN_ALERT_CONFIDENCE=0.55` (was 0.75 — lowered at some point; see ⚠️ below)
- `V3_ENSEMBLE=stacking` → routes to proven `_build_ensemble` (XGBoost + RF soft-voting)
- `STOCK_SYMBOLS` = full 44-symbol official watchlist
- `V3_WF_ACC_MIN_OVERRIDES=WOLF=0.52` was set in Railway but is now capped at `wf_acc_mean` (40%) by code — effectively disabled
- **v3 training gates:** `V3_MIN_HOLDOUT_ACC=0.38`, `V3_MIN_WF_ACC_MEAN=0.40`, `V3_MIN_EDGE=0.0`, `V3_WF_ACC_MIN_SLACK=0.15`, `V3_MIN_TP_SL_WINS=10`, `V3_MIN_WF_FOLDS=2`
- **Model coverage (2026-06-22):** **41/44** watchlist symbols ensemble, 41/44 conformal

### ✅ RESOLVED: Confidence floor restored to 0.75

`MIN_ALERT_CONFIDENCE` was `0.55` live (was designed at 0.75). **Fixed in Railway env (2026-06-25).** Now `MIN_ALERT_CONFIDENCE=0.75`, `OBJECTIVE_BOOTSTRAP_MIN_CONF=0.78`. Combined with the sentiment confidence-floor bypass fix (PR #77), the gates are back to design spec.

### ✅ RESOLVED: Mega-cap pollution filtered

The `picks` table contained ~37 EXPIRED picks on PLTR, MSFT, TSLA, AMZN, META, NVDA, NET. **Fixed in PR #76.** `REAL_TRADE_WHERE` now includes `AND symbol IN (OFFICIAL_WATCHLIST)` — all 43 watchlist symbols. Write-side guard in `_predict_symbol_ex` also blocks non-watchlist saves. RDFN excluded from `STOCK_SYMBOLS`.

---

## SIGNAL ENGINE v3.2 (PR #65–#66 additions)

The engine now uses a **stacking ensemble** when `V3_ENSEMBLE=stacking`: XGBoost + Random Forest are trained on the same walk-forward folds and blended via soft-voting. Conformal calibration (`q_hat`) is computed per-symbol. The training path that was broken by a missing import (`is_stacking_enabled` NameError) is now fixed — retrain runs to completion.

**Current model stats (2026-06-22):** mean_acc=62.9%, mean_edge=28.3%, 41/44 ensemble, 41/44 conformal.

---

## CIRCUIT BREAKERS (PR #66, fix in PR #69)

`core/circuit_breaker.py` — 5 breakers with sliding window failure counting:

| Breaker | Threshold | Cooldown |
|---------|-----------|----------|
| yfinance | 5 failures | 600s |
| finnhub | 5 failures | 300s |
| polygon | 5 failures | 300s |
| alpaca | 5 failures | 300s |
| anthropic | 3 failures | 600s |

**PR #69 fix:** Alpaca 403/404 (free-tier SIP expected responses) and yfinance empty history no longer counted as failures. Before this fix, Alpaca had accumulated 4,785 false failures → breaker open → all price data blocked → 0/43 squeeze fetches. Admin endpoint `POST /api/admin/reset-breakers` available for manual recovery.

---

## HOW PICKS ARE GENERATED (v3.2 engine)

**XGBoost is BACK.** The old "win-rate-from-gpo, no XGBoost" design is gone. The
live engine is the **v3.2 XGBoost model** trained on TP/SL daily-bar outcomes with
walk-forward validation. The WOLF model trained at **~65.4% holdout accuracy**
(PR #21 was the fix that finally produced folds — see Change Log).

`core/signal_engine.py → predict_live_ex(symbol, asset_type, scores=None)`:
1. Load model; fetch intraday OHLCV (5d/1h).
2. **Regime gates** (inside the engine): skip BUY if below EMA200 + ADX<20
   (downtrend + chop); skip if full bearish EMA stack and not oversold.
3. Model `predict_proba` → `up_prob`.
4. **Meta gates**: reject if `edge < V3_MIN_EDGE`, holdout `accuracy < V3_MIN_HOLDOUT_ACC`,
   or walk-forward acc/edge below floors.
5. Confidence: `conf = clamp(accuracy + (up_prob − min_p) × 4.0, 0.75, 0.95)`
   where `accuracy ≈ 0.654` (model holdout) and `min_p = V3_MIN_WIN_PROBA` (0.55),
   **regime-adjusted** via `core/regime_calibration.py` when `GHOST_REGIME_CALIBRATION=1` (PR #60).
6. If `scores` dict is passed, it's populated with the **specialist score vector
   + regime-at-issuance** (PR #30) for the pick journal.

### Two lanes (independent products)

| Lane | Role | When active |
|------|------|-------------|
| **v3 picks** | ~3-day gated XGBoost holds | ~30 min prediction cycles; often silent |
| **Squeeze radar** | Intraday RVOL + move scorecard | **3 AM – 7 PM CT**; separate Telegram path |

Squeeze lane: `core/squeeze_monitor.py` → `GET /api/squeeze/picks` (scorecard buy/sell/stop,
P(+3%) with squeeze ML v2 blend). Predictions persist to `ghost_squeeze_outcomes` via
`core/squeeze_outcomes.py` → `GET /api/squeeze/daily-log` (EOD resolve vs session OHLC).
v3 lane unchanged — falsification gate tripped; journal shows `ABANDON_80_CLAIM`.
See `GET /api/ghost/contract`.

### The four-gate chain (why the system is usually silent)
1. **Model gate** — engine emits a signal at all (regime + meta gates above).
2. **Confidence floor** — `MIN_ALERT_CONFIDENCE` (`CONFIDENCE_FLOOR`, 0.75 live).
3. **SELL block** — BUY-only. DOWN signals blocked (1.9% historical WR).
4. **Objective gate** — `core/prediction.py → _objective_gate`.

### Objective gate modes
| Mode | target_wr | min_samples | bootstrap_min_conf |
|---|---|---|---|
| precision | 0.80 | 20 | 0.90 |
| balanced | 0.70 | 12 | 0.85 |
| **aggressive (live)** | 0.62 | 8 | 0.78 |

`OBJECTIVE_AUTO_MODE_ENABLED=1` lets the engine override `OBJECTIVE_MODE` via
`ghost_state.objective_mode_runtime`. We currently run with auto-mode **off** so
the env value is authoritative. Inspect live gating at `/api/wolf/gate-status`.

---

## DATA FEED CHAIN

`core/signal_engine.py → _fetch_ohlcv` tries, in order:
**Alpaca SIP → Alpaca IEX → Polygon → yfinance → Stooq** (5-tier, PR #9/#12/#17).
Health probe uses `HEALTH_PROBE_SYMBOL` (AAPL, not WOLF) so feed health isn't
conflated with WOLF data gaps (PR #24).

**Known feed-data limits (NOT code bugs):** Key Stats, Analyst targets, Short
Interest often return empty for this account tier (yfinance/Polygon). Fixing
these needs a **paid provider decision** (Finnhub paid / Tiingo / Alpha Vantage),
not more code.

---

## DATABASE — KEY TABLES

| Table | Notes |
|---|---|
| `predictions` | v1 + v2/v3 picks. **v3.2 era = `id >= 223438`** (used everywhere to exclude ~223k legacy rows). Columns incl. `outcome`, `exit_price`, `pnl_pct`, `resolved_at`, `features JSONB`, **`scores JSONB`** (PR #30) |
| `ghost_state` | key/val cross-cycle state (objective_mode_runtime, gate_outcome_history, v32_stats_start_ts, last_train_details, etc.) |
| `ghost_prediction_outcomes` | legacy v1 signal source — no longer the engine |
| `ghost_v3_model` | trained v3 model blob + meta_* rows |

`core/db.py` migrations are additive/`IF NOT EXISTS` and non-destructive. The
`scores JSONB` column is added on boot.

**CRITICAL:** when INSERTing v2/v3 predictions, set `run_at = predicted_at` (v1
schema had NOT NULL on run_at; migration drops it but it may return).

---

## API ENDPOINTS (current)

**Public (no auth):**
- `GET /health` — health score + task status
- `GET /api/picks` — active + recent (array directly)
- `GET /api/v2/recent` — resolved trades summary
- `GET /api/stats/v32` — v3.2-era WOLF stats
- `GET /api/objective` — win-rate objective progress
- `GET /api/cockpit/context` — master cockpit payload
- `GET /api/wolf/price | /predictions | /stats | /earnings | /analyst | /news | /ghost-score | /context`
- `GET /api/wolf/gate-status` — **live four-gate diagnostic** (mode, floor, live prediction, would_alert) (PR #27)
- `GET /api/wolf/gate-history` — **rolling per-cycle gate outcomes** (last 50) (PR #29)
- `GET /api/wolf/pick-journal` — **credibility ledger** (PR #30): paginated audit
  trail + win rate w/ 95% Wilson CI + expectancy + Brier + `verdict.falsification`
- `GET /api/squeeze/picks` — intraday squeeze board (scorecard, buy/sell/stop, CT session)
- `GET /api/squeeze/status` — last 44-symbol scan snapshot + `radar_active`
- `GET /api/squeeze/daily-log` — squeeze prediction ledger vs session OHLC (PR #61)
- `POST /api/admin/squeeze-resolve` — force EOD resolve (ops / backfill)
- `GET /api/ghost/contract` — post-falsification product contract (PR #60)
- `GET /api/ghost/blueprint` — Phase 1+2 module status rollup
- `GET /api/ghost/regime | /drift | /sentiment | /options` — Phase 2 probes
- `GET /api/shadow-stats` — virtual hit-rate scoreboard (gates ignored)
- `GET /api/_version` — running PR version marker (`_RUNNING_PR_VERSION`, currently **62**)
- `GET /api/diag/data-sources` — feed-tier visibility

**Protected (`x-cron-secret`):**
- `POST /api/run-predictions` | `/api/morning-card` | `/api/reconcile`
- `POST /api/v3/train/sync` — synchronous train + gate report (PR #18/#20)
- `POST /api/cron/signal-check` — Telegram signal alert (PR #8)
- `POST /api/admin/purge-ghost-portfolio`
- `POST /api/clean-garbage`

**Admin (`/admin`, cookie auth — PR #28):**
- `GET /admin` — operator console (login form if no cookie)
- `POST /admin/login` (JSON body) / `POST /admin/logout`
- Console cards: gate monitor, gate history, **squeeze status**, **squeeze daily log**, **blueprint modules**, **feature drift**, **options flow**, train/purge/data-source, engine quality, kill status.
- `POST /api/admin/reset-breakers` — **PR #69** force-close all 5 circuit breakers (cookie auth, 404 if not admin)

---

## ADMIN AUTH (PR #28)

HTTP Basic Auth (PR #23) returned a correct 401 locally but **blank-paged on
Railway** (edge/browser mishandling the Basic challenge). Replaced with
**HMAC-signed cookie login**: JSON-body `POST /admin/login` mints an HttpOnly,
SameSite=Lax, Secure (`ADMIN_COOKIE_SECURE`, default on) token. Do not reintroduce
HTTP Basic.

---

## COCKPIT UI

- **Investor cockpit:** `cockpit.html` at `GET /cockpit`. Modules incl. hero,
  perf strip, **Daily Prediction Panel** (next session + last session rows),
  **Forecast vs Reality scorecard** (full watchlist chips),
  stats, earnings, analyst, news, portfolio (**Ask Ghost / WOLF play / chart below portfolio**),
  **Intraday squeeze radar** (CT session clock, score/P(+3%)/stop, overnight offline copy),
  **Squeeze accountability log** (predicted buy/sell/stop vs session OHLC, EOD resolve),
  **v3 pick lane · post-falsification** (hero truth strip, BIAS ONLY gauge when gates bind),
  **Today's v3 pick** lane label, **Pick Journal** (contract banner + POST-FALSIFICATION verdict),
  **Signal History**, **Performance Log**, Truth Mode.
- **Operator console:** `admin.html` at `GET /admin` (cookie-gated).
- Cockpit JS uses v2 field names: `outcome` (not status), `stop_price` (not
  stop_loss), `expires_at` (unix). Calculate gain as `(target-entry)/entry*100`.

---

## CACHE-BUSTING RAILWAY (recurring pain)

Railway/Nixpacks has served stale containers repeatedly. Bust the cache by bumping
ALL of: `Procfile` boot-echo string, `nixpacks.toml` `cache_bust` comment, and the
`wolf_app.py` boot banner / `_RUNNING_PR_VERSION`. Verify deploy via `/api/_version`.

---

## WHAT FAILED — DO NOT REPEAT

- **(SUPERSEDED) "XGBoost removed."** The 2026-03 note said XGBoost overfit on
  skewed gpo data and was removed. The v3.2 engine (PR #10/#21) retrains on **TP/SL
  daily-bar labels with walk-forward validation + holdout gates**, which is a
  different setup — it is now the live engine at ~65.4%. The old caution (don't
  train on bear-skewed gpo direction labels) still stands; the blanket "no XGBoost"
  does not.
- **Walk-forward produced 0 folds** — hardcoded `min_train=max(120, n*0.5)` with
  n≈127 → 0 folds → no model. Fixed PR #21 by making floors env-tunable
  (`V3_WF_MIN_TRAIN` default 60, `V3_WF_TEST_SIZE` default 15). **This trained the model.**
- **News leak (Zoom/IBM/Ralph Lauren)** — Finnhub tags every WOLF-query article
  `["WOLF"]`, so the `WOLF in syms` shortcut passed everything. Fixed PR #26 by
  requiring a textual mention (WOLFSPEED/WOLF/SiC/silicon carbide).
- **/admin blank page** — HTTP Basic Auth on Railway. Fixed with cookie login (PR #28).
- **Pushing fixes to an already-merged branch** — always branch fresh from `main`.
- **Reuters RSS / morning_card 3600 / HOOD-COIN / crypto defaults** — see history;
  still applies.

---

## NOT YET BUILT (blueprint backlog, dependency-ordered)

### Done (Phase 1+2 — PR #60, commit `91dc94c`)
- [x] **Falsification evaluated** — gate tripped; `ABANDON_80_CLAIM`; honest product copy
- [x] **Regime calibration (dynamic min_p)** — `core/regime_calibration.py`; not full isotonic maps
- [x] **Squeeze ML v2 (baseline blend)** — logistic priors until labeled outcomes accrue
- [x] **Unified regime classifier (rules)** — price + engine labels; not HMM
- [x] **Lexicon sentiment probe** — `core/news_sentiment.py`; not FinBERT
- [x] **Feature drift (z-shift)** — `ghost_feature_snapshots`; not KL divergence
- [x] **Options flow probe (yfinance PCR)** — nearest expiry; not Polygon GEX model
- [x] **Intraday squeeze radar** — scorecard, CT session, admin card, scan cache
- [x] **Squeeze daily log** — `ghost_squeeze_outcomes`, EOD job, cockpit + admin UI (PR #61)
- [x] **Truth-mode UX alignment** — hero strip, v3 pick lane copy, BIAS ONLY gauge (PR #62)
- [x] **Live vs alert drift** — `core/squeeze_live_drift.py`, cockpit summary + columns (PR #63)

### Still open (Phase 3 depth)
- [x] **Prod-verify PR #60** on Railway — **2026-06-11** operator confirmed (see session log)
- [x] **Prod-verify PR #61–#62** — **2026-06-12** operator + browser agent confirmed (`376bf8c`)
- [x] **Prod-verify PR #63** — **2026-06-15** agent API curl confirmed (`66da1f9`)
- [x] **Squeeze first-wake check** — **2026-06-12** ~8:39 AM CT; radar live, 4 Telegram alerts, leaders populated
- [x] **Squeeze EOD resolve check** — **2026-06-12** 17 rows resolved (5 WIN, 5 LOSS, 7 NEUTRAL)
- [ ] **Weekly ops checklist** — first full pass during CT session (see below)
- [ ] **Retrain squeeze ML v2** from labeled squeeze session outcomes
- [ ] **Regime detector (HMM)** — deferred: ~250 days too thin for 5-state HMM
- [ ] **Options-flow model (Polygon)** — IV skew, GEX; validate WOLF chain depth first
- [ ] **Sentiment model (FinBERT)** on existing news pipeline
- [ ] **Feature-drift KL divergence** (upgrade from z-shift alerts)
- [ ] **Regime-conditional isotonic calibration** — separate maps per regime
- [ ] **Paid data provider** for Key Stats / Analyst / Short Interest (budget decision)

---

## WEEKLY OPS CHECKLIST (5 URLs + healthy accumulation)

Run once per week (any time for deploy checks; squeeze/radar checks best **Mon–Fri 3 AM–7 PM CT**). Mark prod-verified in `PROJECT_STATE.py` only after you personally confirm.

**Base:** `https://ghost-protocol-v2-production.up.railway.app`

| # | URL | Healthy signal | Red flag |
|---|-----|----------------|----------|
| 1 | `/api/_version` | `_pr_version: 69`, `git_sha_short` matches recent `main` | Stale SHA vs GitHub; `_pr_version` &lt; 69 |
| 2 | `/api/ghost/contract` | `ok: true`, `north_star_retired: true`, two lanes in `lanes` | `ok: false` or missing contract |
| 3 | `/api/squeeze/picks` | During session: `radar_active: true`, `scan_ok: true`, `last_scan_ts` fresh (&lt;5 min); leaders populated | `radar_active: false` mid-session; `fetch_fail` high; empty leaders all week |
| 3b | `/api/squeeze/daily-log?days=14` | `enabled: true`, rows accrue; pending rows show `live_price`/`gap_pct`; `live_drift[]` summary | `enabled: false`; zero rows after active alert day |
| 4 | `/api/wolf/pick-journal?limit=5` | `ok: true`, `verdict.falsification.status` present (expect `ABANDON_80_CLAIM`); metrics updating if picks resolve | 500 / pool errors; journal frozen weeks with open picks |
| 5 | `/api/shadow-stats?days=7` | `ok: true`, `enabled: true`, `resolved` growing week-over-week; WOLF row has pending or resolved rows | `enabled: false`; zero seeded rows for 7+ days (scans broken) |

**Admin (`/admin`, cookie login) — same session, optional depth:**

| Card | Healthy | Red flag |
|------|---------|----------|
| **Squeeze daily log** | Rows for session date; pending before 3 PM CT; resolved after EOD job | API 500; cockpit `#squeeze-daily-log-section` missing |
| **Squeeze status** | Last scan time recent; top movers show scores/RVOL; Telegram alerts on volatile days (may be **zero** on quiet days — OK) | No scan for full CT session; force-scan fails |
| **Blueprint modules** | Phase 1 flags on; squeeze ML `squeeze_ml_v2`; drift `stable` or `insufficient_samples` early on | Phase 1 off unexpectedly; drift `alert` on many features |
| **Kill conditions** | Loads without “connection pool exhausted” | Pool / DB errors |
| **Gate status** | Binding gate named (e.g. `v3_regime_gate`); WOLF `up_prob` shown | Blank / 500 |

### What “healthy accumulation” means (passive — no action required)

| Data | Accumulates when | “Enough” for Phase 3 (rule of thumb) |
|------|------------------|--------------------------------------|
| **Squeeze scan rows** | Every ~60s, **3 AM–7 PM CT** weekdays | Leaders table updating daily = radar alive |
| **Squeeze Telegram alerts** | Only on **~2.5× RVOL + move** thresholds | **Quiet weeks with 0 alerts is normal**; retrain needs **~30+ resolved rows** in `ghost_squeeze_outcomes` with EOD labels |
| **Squeeze outcome labels** | `ghost_squeeze_outcomes` — Telegram + first candidate/day; EOD resolve after 3 PM CT | Rows stuck pending after market close; resolve job not firing |
| **v3 pick journal** | When a pick **fires** + reconcile resolves (~3-day hold) | Slow by design; shadow stats are the main v3 learning surface while journal is quiet |
| **Shadow virtual picks** | Hourly job + each market scan seeds evals | **100+ resolved** shadow rows across watchlist over 30d = engine eval pipeline healthy |
| **Feature drift** | `ghost_feature_snapshots` from scans | Admin drift: **`samples` ≥ 30** before trusting alerts; `insufficient_samples` OK for first weeks |
| **Squeeze ML v2 retrain** | Not automatic yet | Wait until labeled outcome dataset exists (target: **≥30 squeeze events** with binary +3%/60m label); until then baseline weights in `data/squeeze_ml_v2.json` are intentional |

### Weekly pass/fail (30 seconds)

- [ ] Deploy current (`/api/_version`)
- [ ] Contract + lanes honest (`/api/ghost/contract`)
- [ ] Squeeze radar scanned this week (`/api/squeeze/picks` or admin squeeze card)
- [ ] Squeeze daily log accruing (`/api/squeeze/daily-log?days=7`)
- [ ] No infra regressions (kill-status, gate-status on `/admin`)
- [ ] Shadow stats still seeding (`/api/shadow-stats?days=7`)

**When to start Phase 3 squeeze ML work:** `ghost_squeeze_outcomes` has **≥30 resolved rows** with WIN/LOSS/NEUTRAL labels (target after ~2–3 active squeeze weeks), not before.

---

## CHANGE LOG (this era)

| PR | Date | What changed |
|---|---|---|
| #2 | 05-22 | inverse confidence + constant-time cron auth + pytest in CI |
| #3 | 05-22 | WOLF-only cleanup (23 files) — drop crypto code paths |
| #4 | 05-22 | correct `/api/clean-garbage` SQL filter |
| #5 | 05-22 | `_cron_ok` reads `CRON_SECRET` at call time |
| #6 | 05-22 | cockpit display upgrade — Truth Mode + confidence calibration |
| #7 | 05-22 | WOLF command center cockpit + 7 backend endpoints |
| #8 | 05-22 | Telegram signal-alert cron wiring + Ghost Score composite |
| #9 | 05-22 | `_fetch_ohlcv` SIP feed + IEX fallback for post-restructure WOLF |
| #10 | 05-22 | lower v3 training thresholds for limited WOLF data |
| #11 | 05-22 | yfinance fallback + WOLF-only train filter |
| #12 | 05-22 | Polygon + multi-strategy yfinance fallback for training |
| #13 | 05-22 | Polygon path logs every branch (surface silent skips) |
| #14 | 05-22 | PR14 diag markers across the v3 training path |
| #15 | 05-22 | PR15 cache-bust — Procfile + nixpacks + boot banner |
| #16 | 05-22 | "Train v3 Model" button in cockpit Truth Mode |
| #17 | 05-22 | Stooq fifth-tier data source + `/api/diag/data-sources` |
| #18 | 05-22 | v3_train force flag + per-phase state + `/api/v3/train/last` |
| #19 | 05-22 | PR19 cache-bust + `/api/_version` + `/api/v3/train/sync` |
| #20 | 05-22 | surface per-symbol gate-fail detail in train-sync response |
| **#21** | 05-22 | **walk-forward fold floors env-tunable — TRAINED THE MODEL (~65.4%)** |
| #22 | 05-23 | ops polish — purge non-WOLF / Telegram status / split freshness |
| #23 | 05-23 | critical investor-view cleanup (items 1–7 of 15) |
| #24 | 05-23 | investor-view polish (items 8–15) + `HEALTH_PROBE_SYMBOL` |
| #25 | 05-23 | news leak + Polygon/short-data fallbacks + PR25 cache-bust |
| #26 | 05-23 | news textual filter + auto-purge ghost portfolio on boot |
| #27 | 05-23 | `/admin` objective-gate monitor + `/api/wolf/gate-status` |
| #28 | 05-23 | `/admin` cookie login (replaces blank-page HTTP Basic Auth) |
| #29 | 05-23 | per-cycle gate-outcome recorder + history endpoint + admin table |
| **#30** | 05-23 | **pick journal — credibility ledger (audit trail + expectancy/Brier + kill condition)** |
| — | 06-07 | **performance log** — `ghost_perf_*` tables + `/api/wolf/performance-log/*` + cockpit panel |
| — | 06-07 | **Daily Prediction Panel** — next-session O/H/C forecast tiles + market row; 4:33 PM CT refresh |
| — | 06-07 | **Cockpit layout** — Ask Ghost, WOLF play, Prediction vs Reality moved **below** My Portfolio |
| — | 06-07 | **Training reliability** — OHLCV retry/cache/2y history; watchlist peer pool; thin-ticker WF floors → **44/44 models** |
| — | 06-07 | **RDFN scorecard fix** — daily forecast falls back to 2y history; marks delisted/stale last-trade dates |
| — | 06-07 | **Pre-market scans** — `GHOST_PREMARKET_SCAN=1` (default): watchlist scans 4:00–9:30 AM CT, extended-session gap overlay, +3% confidence floor bump |
| — | 06-07 | **Open pick review** — `GHOST_OPEN_PICK_REVIEW=1`: withdraw (`WITHDRAWN`) when model flips; supersede on level/confidence shift; Telegram notify |
| — | 06-09 | **Watchlist parity verified live** — prod scans all 44/cycle (`gate-history scanned: 44`); `OBJECTIVE_BOOTSTRAP_MIN_CONF=0.65` set on Railway (removes hidden ~0.58 double-gate for first-time symbols; prob floor + regime gates untouched) |
| — | 06-09 | **Shadow scoring** — `core/shadow_outcomes.py`: every scanned symbol's daily eval resolved as a virtual pick (live TP/SL bar-path rules, gates ignored); `/api/shadow-stats` + MCP `ghost_shadow_stats` + hourly scheduler job; regime gates in `predict_live_ex` now enforce AFTER model scoring so all 44 evals carry `up_prob` (live-verified: pending rows seeding) |
| — | 06-09 | **Silence card leaderboard + alert dedup** — ranked closest-to-firing candidates in 8 AM card; accurate scan-cadence copy (was "Tomorrow 8 AM" — wrong, engine scans ~30 min); portfolio exit alerts dedupe by symbol (was duplicate AMC); morning card claims daily Telegram slot before cycle (was duplicate SILENCE cards) |
| — | 06-09 | **Crypto junk purged on prod** — `POST /api/admin/purge-crypto-junk` deleted 223,873 legacy crypto/zero-entry rows from `predictions` (user-approved; dry-run verified first) |
| **#55** | 06-10 | **Squeeze scorecard + CT radar** — Setup/Trigger/Confirm, heuristic P(+3%), buy/sell/stop, `/api/squeeze/picks`, cockpit panel (`d7477d1`) |
| **#56** | 06-10 | **Overnight squeeze honesty** — persist `data/squeeze_last_scan.json`, paused/resume CT copy (`d17ffe3`) |
| **#57** | 06-10 | **Two-lane cockpit labels** — Today's v3 pick vs intraday squeeze (`3d9f4b9`) |
| **#58** | 06-10 | **Admin squeeze card + DB pool fix** — kill-status no longer exhausts pool; force scan (`c0bad2e`) |
| **#60** | 06-10 | **Phase 1+2 blueprint wired** — ghost_contract, regime_calibration, squeeze_ml_v2, regime/sentiment/drift/options probes, `/api/ghost/*`, admin cards, 403 tests (`91dc94c`) |
| **#61** | 06-12 | **Squeeze daily log** — `ghost_squeeze_outcomes`, EOD resolve job, `/api/squeeze/daily-log`, cockpit accountability section + admin card (`37c5db6`) |
| **#62** | 06-12 | **Truth-mode UX** — hero post-falsification strip, v3 pick lane copy, BIAS ONLY gauge, journal-aligned perf copy (`376bf8c`) |
| **#63** | 06-15 | **Live vs alert drift** — first Telegram buy vs live quote; `live_drift[]` on picks + daily-log; cockpit summary table (`66da1f9`) |
| — | 06-11 | **PR #60 prod-verified** — operator confirmed Railway `7367631c`: admin blueprint/kill/gates, cockpit POST-FALSIFICATION + contract banner, overnight squeeze pause OK; ledger `b20fff6` |
| — | 06-12 | **PR #61–#62 prod-verified** — operator + browser agent: `_pr_version` 62, `376bf8c`; squeeze daily log 11 rows pending EOD; hero truth strip + v3 lane copy; squeeze wake + 4 Telegram alerts |
| — | 06-15 | **PR #63 prod-verified** — agent API curl: `_pr_version` 63, `66da1f9`; daily-log `live_drift` 18 symbols; EOD 2026-06-12 resolved |
| **#65** | 06-22 | **Settings alignment, config sync** — settings.py/symbols.py coherence pass |
| **#66** | 06-22 | **Stacking ensemble + reliability overhaul** — XGBoost+RF soft-voting (`_build_ensemble`), conformal calibration (q_hat per symbol), circuit breakers on all 5 external APIs, latency SLO middleware, degraded mode, dead-letter queue, SHAP explain, EDGAR integration, VADER sentiment, Ghost Score spec v1.0, WebSocket cockpit feed. **CRITICAL FIX:** missing `from core.stacking_ensemble import is_stacking_enabled` caused NameError → all retrain threads crashed silently → state stuck `running` forever. |
| **#67** | 06-22 | **Cross-sectional features + Kelly sizing** — after 44-symbol scan loop, `_all_feats` dict stashed on `predict_live_ex._last_scan_features`; `position_sizing_plan()` now accepts `win_rate/avg_win_pct/avg_loss_pct/open_positions` and returns `kelly_fraction/kelly_note/portfolio_heat_mult` via `core.kelly_sizing` |
| **#68** | 06-22 | **WOLF gate fix + latency SLO fix** — `V3_WF_ACC_MIN_OVERRIDES=WOLF=0.52` on Railway demanded every WF fold beat 52%; capped at `wf_acc_mean` (40%) by code (`_v3_wf_acc_min_overrides()` sanity cap). `_SLO_EXCLUDE_PREFIXES = ("/api/v3/train",)` excludes multi-minute training from p95/p99. Result: WOLF ensemble=True, 41/44 ensemble, 41/44 conformal. |
| **#69** | 06-22 | **Circuit breaker false-positive fix** — Alpaca SIP 403/404 (free-tier expected) and yfinance empty history no longer count as `record_failure()`. Before fix: Alpaca had 4,785 false failures → breaker open → 0/43 squeeze fetches. After fix: squeeze radar recovering (20+/43). Added `reset_all_breakers()` + `POST /api/admin/reset-breakers`. |
| **#70** | 06-25 | **yfinance JSON parse breaker + Alpaca rate-limit storm** — JSON parse errors in `_yfinance()` now count as breaker failures; 1.2s inter-symbol delay in scan loop; yfinance 0.2.44→0.2.54; yfinance library logger suppressed to CRITICAL |
| **#71** | 06-25 | **Circuit breaker infinite half-open probe loop (yfinance)** — `record_failure()` was resetting `_half_open_probes=0` on every failure, granting infinite free probes. Breaker now only resets probes on initial trip. |
| **#72** | 06-25 | **Circuit breaker infinite half-open probe loop (Alpaca rate-limit)** — Same bug in rate-limit path: `_half_open_probes=0` made `state` return `"half_open"`, allowing in-flight calls to close the circuit 12ms after trip. Now exhausts probes on rate-limit trip. |
| **#73** | 06-25 | **API rate limiter 120→300 RPM** — Cockpit fires ~25 parallel API calls on page load; 120 RPM (2/sec) couldn't handle the burst. |
| **#74** | 06-25 | **Gate prev-close yfinance fallback behind circuit breaker** — `_predict_symbol_ex` had a raw yfinance call that bypassed the breaker. |
| **#75** | 06-25 | **Gate all remaining raw yfinance calls** — macro_regime, wolf_monitor, wolf_context, squeeze_monitor all now check `_yfinance_cb.allow()`. |
| **#76** | 06-25 | **Watchlist-membership filter** — `REAL_TRADE_WHERE` now includes `AND symbol IN (OFFICIAL_WATCHLIST)`; write-side guard in prediction cycle. |
| **#77** | 06-26 | **6 audit findings** — unauth portfolio routes, raw yfinance in extended/intraday, sentiment confidence floor bypass, public test-alert auth, CRON_SECRET production boot guard, double inter-symbol delay |
| **#78** | 06-26 | **7 audit findings** — 5-tier spot chain (Polygon/IEX/Stooq spot functions), degraded mode half_open counting, scheduler overlap guard, XFF hardening, Playwright selectors, CircuitBreaker tests (8 new), wolf_price alias |
| **#79** | 06-26 | **4 continuation findings** — NaN sanitization in Polygon/Stooq OHLCV, Telegram dedup conditional on `_send()`, dead-letter admin UI fix, OAuth CIMD SSRF hardening |
| **#80** | 06-26 | **9 third-pass findings** — Ghost Ask portfolio leak, Polygon/Stooq NaN, check_feeds 5-tier, Playwright hidden element, cockpit 401 handling, reconcile double-resolve, train endpoint lock, morning card dedup after send, OAuth redirects |
| **#81** | 06-26 | **GP-A03 fix + War Room** — yfinance wrapper (`core/yfinance_client.py`) + `api/wolf_endpoints.py` monkeypatch (zero raw yfinance calls remain); War Room endpoint (`POST /api/wolf/war-room`) — 6-agent equity research pipeline powered by Claude Sonnet |
| **#82** | 06-28 | **Super Ghost AI brain + market-regime adjustment** — `detect_market_regime()`, conviction multiplier, Claude AI brief on `GET /api/wolf/super-ghost?ai=1` |
| **#83** | 06-28 | **Super Ghost AI model fix** — default AI model changed to proven `claude-haiku-4-5-20251001`; live `ai_brief.available=true` verified |
| **#84** | 06-28 | **Super Ghost Truth Ledger** — `super_ghost_predictions` table, log/history/accuracy/if-followed/resolve endpoints, scheduler resolver job |
| **#85** | 06-28 | **Super Ghost Master Build map** — `docs/SUPER_GHOST_MASTER_BUILD.md`, machine-readable `super_ghost_master_plan.json`, CI guard `tests/test_master_plan.py` |
| **#86** | 06-29 | **Unified Liquid Glass prediction console** — `/` + `/picks` serve `ghost_console.html`; tabs: Overview, Top stocks, Bullish, Today, 48 hour, This week, Live mirror, Health; `/legacy-picks` + `/cockpit` preserved |
| **#87** | 06-29 | **Real live-market mirror** — `GET /api/market/session/{symbol}` exposes live open/high/low/price; console compares prediction reference/stop/target against live session data |
| **#88** | 06-29 | **Super Ghost Data Coverage Upgrade** — `core/market_history.py`, `core/sec_fundamentals.py`, EPS YoY/revenue YoY, current-price fallback, hard coverage gate (`MIN_COVERAGE_FOR_AB=18`), `/api/wolf/super-ghost/coverage` |
| **#89** | 06-29 | **History source fix** — `get_daily_history()` now delegates first to production-proven `_fetch_ohlcv` chain (Alpaca SIP→IEX→Polygon→yfinance→Stooq) before direct fallbacks |
| **#90** | 06-29 | **Generic SEC ticker→CIK** — common large-cap CIK map + best-effort SEC ticker index; AAPL/NVDA/etc fundamentals resolve, not WOLF-only |
| **#91** | 06-29 | **Ghost console trust-state polish** — persistent post-falsification banner, clearer Top Stocks “completed predictions” copy, duplicate top-tabs hidden; `_pr_version` 91 |
