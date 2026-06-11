# Ghost Protocol v2 — PROJECT STATE
**Last updated:** 2026-06-10
**Read this first.** Any agent picking up this project must read this file before touching any code.

> This file was significantly stale (pre-v3.2) until 2026-05-23. It now reflects
> the v3.2 XGBoost engine, the four-gate chain, aggressive objective mode, the
> cookie-login `/admin` console, and the pick-journal credibility ledger.
> **PR #60 (2026-06-10):** Post-falsification product contract, regime calibration,
> squeeze ML v2, unified regime classifier, lexicon sentiment, feature drift, options flow probe.

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

**Sandbox cannot reach Railway** (egress allowlist) — all production verification
is done by the user. Agents must not claim prod-verified without user confirmation.

### Live env config (set in Railway, confirmed by user)
- `OBJECTIVE_MODE=aggressive`
- `OBJECTIVE_AUTO_MODE_ENABLED=0` (env wins; runtime auto-mode override disabled)
- `MIN_ALERT_CONFIDENCE=0.75`
- `STOCK_SYMBOLS` = full 44-symbol official watchlist (was stale `TSLA,META,AMZN,T`)
- **v3 training gates (relaxed for coverage):** `V3_MIN_HOLDOUT_ACC=0.38`, `V3_MIN_WF_ACC_MEAN=0.40`, `V3_MIN_EDGE=0.0`, `V3_WF_ACC_MIN_SLACK=0.15`, `V3_MIN_TP_SL_WINS=10`, `V3_MIN_WF_FOLDS=2`
- **Model coverage (2026-06-07):** **44/44** watchlist symbols have trained v3 models

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
   where `accuracy ≈ 0.654` (model holdout) and `min_p = V3_MIN_WIN_PROBA` (0.55).
6. If `scores` dict is passed, it's populated with the **specialist score vector
   + regime-at-issuance** (PR #30) for the pick journal.

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
- `GET /api/_version` — running PR version marker
- `GET /api/diag/data-sources` — feed-tier visibility

**Protected (`x-cron-secret`):**
- `POST /api/run-predictions` | `/api/morning-card` | `/api/reconcile`
- `POST /api/v3/train/sync` — synchronous train + gate report (PR #18/#20)
- `POST /api/cron/signal-check` — Telegram signal alert (PR #8)
- `POST /api/admin/purge-ghost-portfolio`
- `POST /api/clean-garbage`

**Admin (`/admin`, cookie auth — PR #28):**
- `GET /admin` — operator console (login form if no cookie)
- `POST /admin/login` (JSON body — no python-multipart dep) / `POST /admin/logout`
- Console cards: gate monitor, gate history, train/purge/data-source, engine quality.

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
  **Pick Journal**, **Signal History**, **Performance Log**, Truth Mode.
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

- [ ] **Accumulate ~30+ resolved high-conviction picks** so the falsification gate
      can be honestly evaluated (cold-start; gates everything below).
- [ ] **Regime detector (HMM)** — Squeeze/Trend-up/Trend-down/Chop/Capitulation.
      Deferred per honesty layer: ~250 days is too thin for a 5-state HMM, and it
      adds a *new* silencing gate to an engine that barely fires. Build after the
      journal shows where losses cluster.
- [ ] Options-flow model (Polygon) — put/call, IV skew, GEX. **Validate WOLF
      options depth first** before assuming mega-cap GEX techniques transfer.
- [ ] Sentiment model (FinBERT) on the existing news pipeline.
- [ ] Feature-drift monitoring (KL divergence).
- [ ] Paid data provider for Key Stats / Analyst / Short Interest (budget decision).
- [ ] Regime-conditional calibration (separate isotonic maps per regime) — the
      journal already captures regime-at-issuance to enable this.

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
