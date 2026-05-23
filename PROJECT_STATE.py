"""
PROJECT_STATE.py — Ghost Protocol v2
Single source of truth. Accountability ledger. Read this before touching anything.

RULES:
  1. Read this file before starting work.
  2. Verify claimed fixes on the LIVE site before trusting them.
  3. After every fix session, add a dated section with PR numbers + live verification.
  4. Never mark [x] done unless YOU personally verified it on the live dashboard.
  5. If you find new bugs, add them to the TODO list.
  6. This is not documentation. It is an accountability ledger.
     Agents lie. This file exists because of that.

NOTE (2026-05-23): refreshed to the v3.2 era. The pre-v3.2 sections (win-rate-from-
gpo signal, "XGBoost removed", crypto stats) were stale for ~2 months. Historical
session logs are preserved at the bottom as accountability history.
"""

# ============================================================
# LIVE SYSTEM — LAST VERIFIED 2026-05-23 (v3.2 engine + pick journal)
# ============================================================

PRODUCTION_URL = "https://ghost-protocol-v2-production.up.railway.app"
GITHUB_REPO = "seancole713-source/ghost-protocol-v2"
RAILWAY_PROJECT = "tender-benevolence"
RAILWAY_SERVICE_V2 = "98593080-065d-43ef-840c-4a3d36a1b572"
RAILWAY_SERVICE_V1 = "098281d7-7dba-447c-981e-0ebd625cecad"  # old ghost, Telegram silenced
CRON_JOB = "cron-job.org fires POST /api/morning-card at 8 AM CT (America/Chicago, 0 8 * * *)"
AUTH_HEADER = "x-cron-secret — value in Railway env as CRON_SECRET"
ADMIN_CONSOLE = "/admin — HMAC cookie login (PR #28). Investor cockpit at /cockpit."

# Sandbox CANNOT reach Railway prod (egress allowlist). All prod verification = user.

# ============================================================
# NORTH-STAR — ~80% on WOLF under an honest, narrow contract
# ============================================================

NORTH_STAR = {
    "target": "~80% accuracy on selective high-conviction WOLF picks (not daily direction)",
    "philosophy": "selective prediction — silent most of the time, fire only when gates agree",
    "data_reality": "WOLF post-Chapter-11 (new shares 2025-09-29) ~250 trading days only",
    "blueprint": "WOLFSPEED-Only Prediction Engine: 7 specialists -> meta-model, 12 data cats",
    "today": "1 model (XGBoost v3.2 ~65.4%), four-gate chain, aggressive mode, credibility ledger live",
}

# Kill condition (pre-registered). See core.prediction.FALSIFICATION_THRESHOLD.
FALSIFICATION = {
    "min_samples": 30,
    "win_rate_floor": 0.70,
    "north_star": 0.80,
    "rule": "N>=30 AND win_rate<0.70 AND 95% CI excludes 0.80 => abandon the 80% claim",
    "surfaced_at": "GET /api/wolf/pick-journal -> verdict.falsification",
}

# ============================================================
# LIVE ENV CONFIG (set in Railway, confirmed by user 2026-05-23)
# ============================================================

LIVE_ENV = {
    "OBJECTIVE_MODE": "aggressive",
    "OBJECTIVE_AUTO_MODE_ENABLED": "0",  # env wins; runtime auto-override disabled
    "MIN_ALERT_CONFIDENCE": "0.75",
}

# ============================================================
# V3.2 PREDICTION ENGINE
# ============================================================

ENGINE = {
    "model": "XGBoost v3.2, trained on TP/SL daily-bar outcomes + walk-forward validation",
    "wolf_holdout_accuracy": 0.654,
    "confidence_formula": "clamp(accuracy + (up_prob - min_p) * 4.0, 0.75, 0.95)  # min_p=V3_MIN_WIN_PROBA 0.55",
    "data_feed_chain": "Alpaca SIP -> Alpaca IEX -> Polygon -> yfinance -> Stooq (5-tier)",
    "health_probe_symbol": "AAPL (HEALTH_PROBE_SYMBOL) — decoupled from WOLF data gaps",
    "buy_only": "DOWN/SELL signals blocked (1.9% historical win rate)",
}

FOUR_GATE_CHAIN = [
    "1. Model gate — engine emits a signal (regime gates: EMA200/ADX, bearish stack; meta gates: edge/accuracy/walk-forward)",
    "2. Confidence floor — MIN_ALERT_CONFIDENCE / CONFIDENCE_FLOOR (0.75 live)",
    "3. SELL block — BUY-only",
    "4. Objective gate — _objective_gate (precision / balanced / aggressive)",
]

OBJECTIVE_MODES = {
    "precision":  {"target_wr": 0.80, "min_samples": 20, "bootstrap_min_conf": 0.90},
    "balanced":   {"target_wr": 0.70, "min_samples": 12, "bootstrap_min_conf": 0.85},
    "aggressive": {"target_wr": 0.62, "min_samples": 8,  "bootstrap_min_conf": 0.78},  # LIVE
    "_note": "OBJECTIVE_AUTO_MODE_ENABLED=1 lets ghost_state.objective_mode_runtime override env. Currently OFF.",
}

# ============================================================
# DATABASE
# ============================================================

DB = {
    "predictions": "v1+v2/v3 picks. v3.2 era = id >= 223438. Cols: outcome, exit_price, "
                   "pnl_pct, resolved_at, features JSONB, scores JSONB (PR #30).",
    "ghost_state": "key/val cross-cycle state (objective_mode_runtime, gate_outcome_history, "
                   "v32_stats_start_ts, last_train_details).",
    "ghost_prediction_outcomes": "legacy v1 signal source — no longer the engine.",
    "ghost_v3_model": "trained v3 model blob + meta_* rows.",
    "V32_ERA_MIN_ID": 223438,
    "migrations": "core/db.py — additive / IF NOT EXISTS / non-destructive. scores col added on boot.",
}

# ============================================================
# API ENDPOINTS (current)
# ============================================================

ENDPOINTS_PUBLIC = [
    "GET /health",
    "GET /api/picks",
    "GET /api/v2/recent",
    "GET /api/stats/v32",
    "GET /api/objective",
    "GET /api/cockpit/context",
    "GET /api/wolf/{price,predictions,stats,earnings,analyst,news,ghost-score,context}",
    "GET /api/wolf/gate-status     — live four-gate diagnostic (PR #27)",
    "GET /api/wolf/gate-history    — rolling per-cycle gate outcomes, last 50 (PR #29)",
    "GET /api/wolf/pick-journal    — credibility ledger: audit trail + win-rate CI + expectancy + Brier + falsification (PR #30)",
    "GET /api/_version             — running PR version marker",
    "GET /api/diag/data-sources    — feed-tier visibility",
]

ENDPOINTS_PROTECTED = [
    "POST /api/run-predictions | /api/morning-card | /api/reconcile  (x-cron-secret)",
    "POST /api/v3/train/sync   — sync train + gate report (PR #18/#20)",
    "POST /api/cron/signal-check — Telegram signal alert (PR #8)",
    "POST /api/admin/purge-ghost-portfolio | /api/clean-garbage",
]

ENDPOINTS_ADMIN = [
    "GET  /admin            — operator console (login form if no cookie)",
    "POST /admin/login      — JSON body (no python-multipart dep); mints HMAC cookie",
    "POST /admin/logout",
]

# ============================================================
# V2 API FIELD NAMES — JS/Frontend must use these
# ============================================================

V2_PICK_FIELDS = {
    "outcome": "null=active | 'WIN' | 'LOSS' | 'EXPIRED'  — NOT p.status",
    "stop_price": "stop loss price  — NOT p.stop_loss",
    "expires_at": "unix timestamp  — NOT p.done_by",
    "gain_pct": "NOT a field — calculate: (target-entry)/entry*100",
    "pnl_pct": "filled after outcome resolved",
    "scores": "JSONB — specialist score vector + regime-at-issuance (new picks only, PR #30)",
}

# ============================================================
# TODO — dependency-ordered (blueprint backlog)
# ============================================================

TODO = """
P0 — COLD-START (gates everything below)
[ ] Accumulate ~30+ resolved high-conviction picks so the falsification gate can
    be honestly evaluated. As of 2026-05-23: 1 resolved pick. Silence is by design.

P1 — NEXT BLUEPRINT MODULES (only after the journal has data)
[ ] Regime detector (HMM): Squeeze/Trend-up/Trend-down/Chop/Capitulation.
    DEFERRED: ~250 days too thin for a 5-state HMM, and it adds a new silencing
    gate to an engine that barely fires. Build after the journal shows loss clusters.
[ ] Options-flow model (Polygon): put/call, IV skew, GEX. VALIDATE WOLF options
    depth first — do not assume mega-cap GEX techniques transfer to a $3.4B name.
[ ] Sentiment model (FinBERT) on the existing news pipeline.
[ ] Feature-drift monitoring (KL divergence).
[ ] Regime-conditional calibration (journal already captures regime-at-issuance).

P2 — DATA (budget decision, not code)
[ ] Key Stats / Analyst / Short Interest return empty on current tier. Needs a
    paid provider (Finnhub paid / Tiingo / Alpha Vantage).
"""

# ============================================================
# COMPLETED — verified on live site
# ============================================================

COMPLETED = """
[x] WOLF pivot — crypto stripped, STOCK_SYMBOLS default 'WOLF'
[x] v3.2 XGBoost engine TRAINED (~65.4% holdout) — PR #21 unblocked walk-forward folds
[x] 5-tier data feed chain (Alpaca SIP/IEX -> Polygon -> yfinance -> Stooq) — PR #9/#12/#17
[x] Four-gate prediction chain (model / confidence floor / SELL block / objective gate)
[x] Objective gate modes + aggressive mode live — PR #27, env confirmed by user
[x] /api/wolf/gate-status live diagnostic — PR #27 (user-verified)
[x] /admin cookie login (replaced blank-page HTTP Basic) — PR #28 (user-verified)
[x] Per-cycle gate-outcome recorder + /api/wolf/gate-history + admin card — PR #29
[x] Pick journal credibility ledger — PR #30 (user-verified 2026-05-23):
    - predictions.scores JSONB; predict_live_ex surfaces score vector + regime-at-issuance
    - /api/wolf/pick-journal: paginated audit trail + win-rate 95% Wilson CI + expectancy + Brier
    - FALSIFICATION_THRESHOLD kill condition (verdict.falsification)
    - cockpit module-7 Pick Journal card
[x] News textual filter (killed Zoom/IBM/Ralph-Lauren leak) — PR #26
[x] Investor-view forensic cleanup (15 items) — PR #23/#24
"""

# ============================================================
# WHAT FAILED — DO NOT REPEAT
# ============================================================

FAILURES = """
1. (SUPERSEDED) "XGBoost removed"
   2026-03 note: XGBoost on ghost_prediction_outcomes hit 86% val by overfitting
   bear-skewed DOWN labels; it was removed in favor of per-symbol win rate.
   STATUS NOW: the v3.2 engine (PR #10/#21) retrains on TP/SL daily-bar labels with
   walk-forward validation + holdout gates — a different setup — and IS the live
   engine at ~65.4%. The narrow caution (don't train on bear-skewed gpo direction)
   still holds; the blanket "no XGBoost" does not.

2. Walk-forward produced 0 folds
   Hardcoded min_train = max(120, n*0.50) with n~127 -> 0 folds -> no model.
   Fix (PR #21): env-tunable floors V3_WF_MIN_TRAIN (60), V3_WF_TEST_SIZE (15).
   THIS is the change that finally trained the WOLF model.

3. News leak (Zoom / IBM / Ralph Lauren)
   Finnhub tags every WOLF-query article ["WOLF"], so the `WOLF in syms` shortcut
   passed everything. Fix (PR #26): require textual mention (WOLFSPEED/WOLF/SiC/
   silicon carbide). Also fixed an unfiltered yfinance augmentation loop.

4. /admin blank page
   HTTP Basic Auth (PR #23) returned correct 401 locally but blank-paged on Railway
   (edge/browser mishandling the Basic challenge). Fix (PR #28): HMAC cookie login,
   JSON body (no python-multipart). Do not reintroduce HTTP Basic.

5. Railway serving stale containers (recurring)
   Bust cache by bumping ALL of: Procfile boot-echo, nixpacks cache_bust comment,
   wolf_app boot banner / _RUNNING_PR_VERSION. Verify via /api/_version.

6. Pushing fixes to an already-merged branch
   Created a commit on a stale merged branch. Always branch fresh from main.

7. (historical) Reuters RSS blocked on Railway; morning_card interval 3600 spam;
   HOOD/COIN poison; crypto defaults; confidence as features[0]. Still applies.

8. Agents claiming fixes done without live verification
   This file exists because of that. Verify before marking [x].
"""

# ============================================================
# SESSION LOG — append a new entry after every fix session
# ============================================================

SESSION_LOG = """
--- 2026-05-23 | v3.2 era: engine trained -> investor cleanup -> admin -> ledger ---
Context: continued the WOLF rebuild from a dead prediction engine (no trained v3
model) through to a live, audit-ready credibility ledger. PRs #9-#30.

Engine resurrection (#9-#21):
  #9  _fetch_ohlcv SIP + IEX fallback for post-restructure WOLF
  #10 lower v3 training thresholds for limited WOLF data
  #11 yfinance fallback + WOLF-only train filter
  #12 Polygon + multi-strategy yfinance fallback
  #13 Polygon path logs every branch (surface silent skips)
  #14 PR14 diag markers across training path
  #15 PR15 cache-bust (Procfile + nixpacks + boot banner)
  #16 "Train v3 Model" button in cockpit Truth Mode
  #17 Stooq fifth-tier source + /api/diag/data-sources
  #18 v3_train force flag + per-phase state + /api/v3/train/last
  #19 PR19 cache-bust + /api/_version + /api/v3/train/sync
  #20 surface per-symbol gate-fail detail in train-sync
  #21 walk-forward fold floors env-tunable  <-- TRAINED THE MODEL (~65.4%)

Investor cockpit + ops (#22-#26):
  #22 ops polish — purge non-WOLF / Telegram status / split freshness
  #23 critical investor-view cleanup (items 1-7 of 15)
  #24 investor-view polish (items 8-15) + HEALTH_PROBE_SYMBOL=AAPL
  #25 news leak + Polygon/short-data fallbacks + PR25 cache-bust
  #26 news textual filter + auto-purge ghost portfolio on boot

Operator console + observability (#27-#30):
  #27 /admin objective-gate monitor + /api/wolf/gate-status
  #28 /admin cookie login (replaces blank-page HTTP Basic Auth)
  #29 per-cycle gate-outcome recorder + /api/wolf/gate-history + admin card
  #30 pick journal — credibility ledger (audit trail + expectancy/Brier + kill condition)

Root cause that unblocked everything: walk-forward made 0 folds on ~127 rows
because of a hardcoded 120-row training floor. PR #21 made the floors env-tunable.

Diagnosed (research, no code): WATCHING-mode root cause = four-gate chain where the
objective gate in precision mode needs either 80% historical WR (model is 65%,
impossible) or 0.90 per-pick bootstrap confidence (a 65% model rarely produces
up_prob high enough). User switched to aggressive mode (env above).

User-verified on prod 2026-05-23:
  - /api/wolf/gate-status: mode=aggressive, auto_mode_enabled=false, floor=0.75
  - /admin: clean cookie-login console (no blank page)
  - /api/wolf/pick-journal: 1 resolved pick (Mar 27, WOLF UP 91% conf, LOSS -1.36%);
    win_rate 0.0 [0-79 CI], expectancy -1.36%, Brier 0.819, verdict insufficient_samples
  - Cockpit Pick Journal card: "GATHERING EVIDENCE - Need 30 ... Have 1."

Open / not yet done:
  - Cold start: only 1 resolved high-conviction pick. Need ~30 before the
    falsification gate can judge the 80% claim. Silence is by design.
  - Key Stats / Analyst / Short Interest still empty (feed-tier limit; needs paid provider).
  - Regime detector (HMM) deferred until the journal has data (data too thin now).

--- 2026-03-24 | Agent: Claude Sonnet (PRE-v3.2, historical) ---
Context: crypto era. BCH/LINK silenced by circuit breaker (8 v2 losses).
Root cause: circuit breaker fired at exactly 8 losses; fix skipped CB when gpo_wr
> EDGE_THRESHOLD. Commits 4084f1b / 354b129 / 36d97f0. Win rate stats corrupted by
$0.50 garbage. (Superseded by the WOLF pivot + v3.2 engine.)

--- 2026-03-23 | Agent: Claude Sonnet (PRE-v3.2, historical) ---
Context: Ghost v1 was 41% accuracy, broken. Full rebuild as v2 (crypto era).
Built DB pool, price feeds, scheduler, per-symbol win-rate signal, Telegram card,
reconciler, cron wiring. Verified health 100. (Superseded by the WOLF pivot.)
"""
