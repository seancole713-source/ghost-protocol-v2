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

LAST UPDATED: 2026-06-29 — PR #94 learning target-calibration fix (517 tests passing)
"""

# ============================================================
# LIVE SYSTEM — LAST VERIFIED 2026-06-29 (PR #94 deployed)
# ============================================================

PROD_VERIFY_2026_06_29_PR93 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "90a9fc7",
    "_pr_version": 93,
    "verified_at_ct": "2026-06-29",
    "tests": "515 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=90a9fc7, _pr_version=93, app_version=2.5.0",
        "learning_summary": "GET /api/wolf/super-ghost/learning?symbol=WOLF&horizon=5 -> ok true, cold-start 0 profiles/lessons",
        "report_learning_block": "GET /api/wolf/super-ghost?symbol=WOLF includes learning_adjustment",
        "learn_auth_gate": "POST /api/wolf/super-ghost/learn without auth -> 401",
    },
    "key_fixes": [
        "core/super_ghost_learning.py: postmortem events + learning profiles from resolved ledger outcomes",
        "Classifies target_too_low / target_too_high / wrong_direction / missed_move / good_skip / direction_right",
        "User's $5 target -> $7 realized case is learned as target_too_low and can widen future target moves after enough samples",
        "Bounded confidence/conviction/target adjustments applied to build_super_ghost reports",
        "Hourly resolver now triggers learning after outcome resolution",
        "Public GET /api/wolf/super-ghost/learning and auth-gated POST /learn",
        "Console Health row shows Learning brain profile/lesson counts",
    ],
    "known_issues": [
        "Learning is cold-start until enough resolved Super Ghost ledger rows exist (min 3 per symbol/direction/horizon)",
        "Adjustments are deliberately bounded and do not bypass coverage/risk gates",
        "This is evidence-based learning, not guaranteed prediction or auto-trading",
    ],
}

PROD_VERIFY_2026_06_29_PR92 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "c6c912b",
    "_pr_version": 92,
    "verified_at_ct": "2026-06-29",
    "tests": "508 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=c6c912b, _pr_version=92, app_version=2.5.0",
        "favicon": "GET /favicon.ico -> 200 image/svg+xml (was 404)",
        "no_intraday_guard": "/picks money() is null-safe; missing live OHLC shows 'No intraday data' not $0.00",
        "coverage_gate_copy": "/picks Overview coverage note explains >=18/25 A/B-grade gate (WOLF 21/25, meets_ab_gate true)",
    },
    "key_fixes": [
        "Root cause of IQ/LCID $0.00 was client-side JS Number(null)===0; fixed money() + m3row",
        "Overview coverage metric now states the 18/25 A/B-grade evidence gate",
        "/favicon.ico (+/favicon.svg) route added; inline SVG <link rel=icon>",
        "Deploy/cache markers bumped to _pr_version 92",
    ],
    "known_issues": [
        "External CSS extraction still deferred (inline styles; maintainability only, not a blocker)",
        "Intraday live fields remain feed/cache-sensitive; UI now degrades honestly to 'No intraday data'",
    ],
}

PROD_VERIFY_2026_06_29_PR91 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "3a83893",
    "_pr_version": 91,
    "verified_at_ct": "2026-06-29",
    "tests": "504 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=3a83893, _pr_version=91, app_version=2.5.0",
        "console_markers": "/picks contains post-falsification banner, completed-predictions Top Stocks copy, hidden duplicate top-tabs",
        "wolf_coverage_still_ok": "GET /api/wolf/super-ghost/coverage?symbol=WOLF -> 21/25, meets_ab_gate=true",
    },
    "key_fixes": [
        "Global post-falsification / old-80%-claim-abandoned banner outside Health tab",
        "Top Stocks gate copy changed from resolved sample jargon to completed predictions / 5 minimum",
        "Duplicate top-tab chrome hidden; sidebar remains visible navigation source",
        "Deploy/cache markers bumped to _pr_version 91",
    ],
    "known_issues": [
        "Coverage gate threshold (18/25) still could be explained more directly in Overview",
        "Favicon still 404",
        "If a market-session feed temporarily lacks OHLC, UI should say No intraday data rather than implying $0.00",
    ],
}

PROD_VERIFY_2026_06_29 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "5bc05a0",
    "_pr_version": 88,
    "verified_at_ct": "2026-06-29",
    "tests": "503 passed, 3 skipped/deselected, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=5bc05a0, _pr_version=88, app_version=2.5.0",
        "wolf_coverage": "GET /api/wolf/super-ghost/coverage?symbol=WOLF -> 21/25, meets_ab_gate=true",
        "aapl_coverage": "AAPL -> 19/25, meets_ab_gate=true",
        "nvda_coverage": "NVDA -> 20/25, meets_ab_gate=true",
        "gate_invariant": "No A/B grade and no HIGH-CONVICTION action below 18/25; verified in tests and live metadata",
    },
    "key_fixes": [
        "Super Ghost 25-point engine has market-regime conviction adjustment + optional Claude AI brief",
        "AI model default fixed to proven claude-haiku-4-5-20251001",
        "Truth Ledger shipped: log/history/accuracy/if-followed/resolve routes + scheduler resolver",
        "Master Build map shipped and CI-enforced",
        "Unified Liquid Glass console live at /picks; /legacy-picks and /cockpit preserved",
        "Live market mirror endpoint GET /api/market/session/{symbol}",
        "Railway-friendly market history via core/market_history.py delegating to _fetch_ohlcv chain",
        "SEC XBRL fundamentals via core/sec_fundamentals.py (EPS YoY + revenue YoY)",
        "Generic ticker->CIK resolution for common large-caps + best-effort SEC ticker index",
        "Hard A/B coverage gate MIN_COVERAGE_FOR_AB=18, exposed in report coverage{}",
        "Coverage health endpoint GET /api/wolf/super-ghost/coverage",
    ],
    "new_files": [
        "ghost_console.html — unified Liquid Glass prediction console",
        "core/super_ghost.py — 25-point prediction-intelligence engine",
        "core/super_ghost_ledger.py — truth ledger + outcome resolver",
        "core/market_history.py — Railway-friendly daily OHLCV history",
        "core/sec_fundamentals.py — SEC XBRL fundamentals + ticker->CIK",
        "docs/SUPER_GHOST_MASTER_BUILD.md — max build roadmap",
        "docs/super_ghost_master_plan.json — machine-readable plan",
        "tests/test_super_ghost_coverage.py — coverage gate + source tests",
    ],
    "known_issues": [
        "Coverage can vary by symbol/provider/cache; unknowns are honest and block A/B if below 18/25",
        "Form 4 insider parser, 13F institutional parser, analyst revisions, options chain, and macro event calendar are still future P1 work",
        "No guaranteed-profit claims; output remains prediction intelligence only, not financial advice or auto-trading",
    ],
}

PROD_VERIFY_2026_06_28 = {
    "deploy_id": "2cb3db3",
    "git_sha_short": "2cb3db3",
    "_pr_version": 81,
    "verified_at_ct": "2026-06-28",
    "tests": "426 passed, 3 skipped, 2 warnings",
    "key_fixes": [
        "Circuit breakers actually block (infinite probe loop fixed)",
        "All yfinance calls gated behind _yfinance_cb (zero raw calls remain)",
        "5-tier spot price chain (Alpaca→yfinance→Polygon→IEX→Stooq)",
        "Portfolio routes auth-gated; test-alert requires cron secret",
        "CRON_SECRET production boot guard",
        "Ghost Ask portfolio leak fixed (include_portfolio=False default)",
        "NaN sanitization in all OHLCV paths (yfinance/Polygon/Stooq)",
        "Sentiment confidence floor bypass fixed",
        "Reconcile/legacy watchdog double-resolve fixed (AND outcome IS NULL)",
        "Morning card dedup after send success (not before)",
        "Train endpoints have concurrency lock",
        "Watchlist-membership filter on all stats/journal queries",
        "API rate limiter 120→300 RPM",
        "Scheduler overlap guard; degraded mode counts half_open",
        "X-Forwarded-For hardening; OAuth CIMD SSRF hardening",
        "Dead-letter admin UI fixed; Playwright selectors updated",
        "CircuitBreaker class tests (8 new)",
        "War Room endpoint (POST /api/wolf/war-room)",
    ],
    "new_files": [
        "core/yfinance_client.py — centralized breaker-gated yfinance wrapper",
        "core/war_room.py — 6-agent equity research pipeline (Claude Sonnet)",
    ],
    "known_issues": [
        "yfinance breaker may be OPEN (Yahoo blocking Railway IPs) — expected, Ghost falls back through other 4 tiers",
        "Playwright browser smoke still needs #mvr-toggle click (fixed in spec, not yet verified on CI)",
    ],
}

PROD_VERIFY_2026_06_12 = {
    "deploy_id": "ba7b1c7e-ccd3-480c-8bd4-5e9b01cf2886",
    "git_sha_short": "376bf8c",
    "_pr_version": 62,
    "verified_at_ct": "2026-06-12 ~11:06 AM CT",
    "squeeze_daily_log_api": "11 rows 2026-06-12 (8 telegram, 11 pending EOD)",
    "squeeze_daily_log_ui": "#squeeze-daily-log-section SQUEEZE ACCOUNTABILITY LOG — AMC row pending",
    "hero_truth_strip": "POST-FALSIFICATION · WR 28.6% 2W/5L · expectancy +0.28% · Pick journal link",
    "v3_pick_lane": "v3 pick lane · post-falsification + honest subtitle",
    "ghost_score": "46 WATCHING · bias only — no trade cleared gates (14d)",
    "squeeze_radar": "live 10:53 AM CT · 35/44 ok · 4 Telegram alerts · AMC ACTIVE",
    "journal": "POST-FALSIFICATION MODE; 28.6% issued / 25% closed; Brier 0.597",
    "engine": "44 scanned · 0 saved · v3_regime_gate binding (ITRI near-miss)",
    "known_noise": "price feeds 1/2 on freshness probe; Alpaca SIP→IEX OK",
    "next_watch": "EOD resolve after 3 PM CT 2026-06-12 (11 rows → session OHLC)",
}

PROD_VERIFY_2026_06_11 = {
    "deploy_id": "7367631c",
    "git_sha_short_admin": "87db7b4",
    "git_sha_short_cockpit": "b20fff6",
    "railway_active": True,
    "python": "3.13.13",
    "phase1_2_admin": "contract v2.0-post-falsification; regime cal on; squeeze_ml_v2 on",
    "cockpit": "contract banner + POST-FALSIFICATION MODE; squeeze paused overnight OK",
    "kill_status": "all clear; DB pool max 25 loads",
    "v3_gate": "WOLF up_prob 0.5373 vs floor 0.5380 (-0.0007); SMA5 Trend-up bypass logged",
    "squeeze_overnight": "PAUSED resumes 3:00 AM CT — expected at 12:14 AM CT",
    "drift": "insufficient_samples (0) — expected early",
    "options_pcr": "empty — thin chain OK",
    "scan_health_7d": "187 cycles, 44/44 scanned, 0 saved (silence by design)",
    "journal": "28.2% WR, ABANDON_80_CLAIM copy live",
    "known_noise": "Alpaca SIP 403→IEX OK; yfinance WOLF flake overnight; RDFN delisted noise",
    "next_watch": "First 3 AM CT squeeze wake Thu 2026-06-11; weekly checklist during session",
}

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
# NORTH-STAR — RETIRED (post-falsification, 2026-06-10)
# ============================================================

NORTH_STAR = {
    "status": "ABANDON_80_CLAIM — legacy ~80% selective-pick claim retired",
    "headline": "Selective directional aid + intraday squeeze radar",
    "philosophy": "Silent most cycles on v3 lane; squeeze radar independent (3 AM–7 PM CT)",
    "data_reality": "WOLF post-Chapter-11 (new shares 2025-09-29) ~250 trading days only",
    "blueprint": "WOLFSPEED-Only Prediction Engine: 7 specialists -> meta-model, 12 data cats",
    "today": "v3.2 XGBoost ~65.4% holdout, four-gate chain, squeeze scorecard, Phase 1+2 probes wired",
    "contract": "core/ghost_contract.py + GET /api/ghost/contract",
    "metrics": "Track live win rate, expectancy, Brier on pick journal — no fixed accuracy marketing",
}

# Kill condition (pre-registered). See core.prediction.FALSIFICATION_THRESHOLD.
FALSIFICATION = {
    "min_samples": 30,
    "win_rate_floor": 0.70,
    "north_star": 0.80,
    "rule": "N>=30 AND win_rate<0.70 AND 95% CI excludes 0.80 => abandon the 80% claim",
    "status": "TRIPPED — ABANDON_80_CLAIM (journal ~28% WR, CI excludes 80%)",
    "surfaced_at": "GET /api/wolf/pick-journal -> verdict.falsification",
    "product_copy": "GET /api/ghost/contract",
}

# ============================================================
# LIVE ENV CONFIG (set in Railway, confirmed by user 2026-05-23)
# ============================================================

LIVE_ENV = {
    "OBJECTIVE_MODE": "aggressive",
    "OBJECTIVE_AUTO_MODE_ENABLED": "0",  # env wins; runtime auto-override disabled
    "MIN_ALERT_CONFIDENCE": "0.75",  # restored from 0.55 (PR #77, 2026-06-25)
    "OBJECTIVE_BOOTSTRAP_MIN_CONF": "0.78",  # restored from 0.65
    "STOCK_SYMBOLS": "43-symbol official watchlist (RDFN removed 2026-06-25)",
    "CB_ALPACA_RATE_MAX_CALLS": "50",  # bumped from 30
    "MODEL_COVERAGE": "44/44 trained (2026-06-07)",
    "V3_MIN_HOLDOUT_ACC": "0.38",
    "V3_MIN_WF_ACC_MEAN": "0.40",
    "V3_MIN_EDGE": "0.0",
    "V3_WF_ACC_MIN_SLACK": "0.15",
    "RATE_LIMIT_RPM": "300",  # bumped from 120 (PR #73)
    "WATCHLIST_FILTER_ENABLED": "1",  # PR #76: only OFFICIAL_WATCHLIST in stats/journal
    "SCAN_INTER_SYMBOL_DELAY_S": "1.2",  # PR #70: prevent Alpaca rate-limit storms
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
    "GET /api/squeeze/picks        — intraday squeeze board + live_drift[] vs first alert buy (PR #55-#63)",
    "GET /api/squeeze/status       — last 44-symbol scan snapshot + radar_active (PR #55-#60)",
    "GET /api/squeeze/daily-log    — squeeze ledger vs session OHLC + live_drift[] intraday (PR #61-#63)",
    "POST /api/admin/squeeze-resolve — force EOD resolve (ops)",
    "GET /api/ghost/contract       — post-falsification product contract (PR #60)",
    "GET /api/ghost/blueprint      — Phase 1+2 module status rollup (PR #60)",
    "GET /api/ghost/{regime,drift,sentiment,options} — Phase 2 probes (PR #60)",
    "GET /api/shadow-stats         — virtual hit-rate scoreboard (gates ignored)",
    "GET /api/_version             — running PR version marker (_RUNNING_PR_VERSION)",
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
P0 — PRODUCT POSITION (done 2026-06-10)
[x] Falsification gate tripped — ABANDON_80_CLAIM; honest copy in cockpit + ghost_contract
[x] User prod-verify PR #60 (2026-06-11): admin + cockpit Phase 1+2 cards, kill-status, overnight squeeze pause
[x] User prod-verify PR #61–#62 (2026-06-12): squeeze daily log API+UI, hero truth strip, v3 lane copy, squeeze wake
[x] Agent prod-verify PR #63 (2026-06-15): live vs alert drift on daily-log API + cockpit HTML; _pr_version 63
[x] Squeeze EOD resolve verify — 2026-06-12: 17 rows resolved (5 WIN, 5 LOSS, 7 NEUTRAL)
[ ] Weekly ops checklist (PROJECT_STATE.md) — run 5 URLs + admin cards once/week (first full pass during CT session)
[x] Confirm squeeze radar wake after 3:00 AM CT 2026-06-11 (leaders + last_scan_ts) — verified 2026-06-12 session

P1 — PHASE 3 DEPTH (probes exist; not yet gating picks)
[ ] Train squeeze ML v2 from labeled squeeze outcomes in ghost_squeeze_outcomes (replace baseline logistic in data/squeeze_ml_v2.json)
[ ] FinBERT sentiment on existing news pipeline (lexicon_v1 is probe-only today)
[ ] Wire drift alerts to Telegram/admin when GHOST_DRIFT_Z_ALERT fires
[ ] Full options-flow model (Polygon IV skew/GEX) — validate WOLF chain depth first
[ ] Regime-conditional isotonic calibration (separate maps per regime; journal has regime-at-issuance)
[ ] Regime detector (HMM) — DEFERRED: ~250 days too thin; build after journal shows loss clusters

P2 — DATA (budget decision, not code)
[ ] Key Stats / Analyst / Short Interest return empty on current tier. Needs paid provider.
"""

COMPLETED_PHASE1_2 = """
Phase 1 (PR #60, commit 91dc94c):
[x] core/ghost_contract.py + GET /api/ghost/contract — post-falsification product copy
[x] core/regime_calibration.py — effective_min_win_proba by regime; SMA5 Trend-up bypass (env)
[x] core/squeeze_ml_v2.py — 60/40 blend into squeeze scorecard probabilities
[x] Cockpit: contract banner, pick-journal POST-FALSIFICATION MODE copy
[x] signal_engine wired: regime_calibration meta on scores; min_p adjusted live

Phase 2 (PR #60, commit 91dc94c):
[x] core/regime_classifier.py + GET /api/ghost/regime
[x] core/news_sentiment.py lexicon_v1 + fetch_news_sentiment + GET /api/ghost/sentiment
[x] core/feature_drift.py z-shift alerts + GET /api/ghost/drift + admin card
[x] core/options_flow.py yfinance PCR probe + GET /api/ghost/options + admin card
[x] GET /api/ghost/blueprint + admin Blueprint Modules card
[x] tests/test_ghost_phase12.py (403 total tests passing at ship)

Squeeze radar lane (PR #55-#59, commits d7477d1..c0bad2e):
[x] core/squeeze_scorecard.py — Setup/Trigger/Confirm + heuristic + ML blend
[x] core/squeeze_monitor.py — 44-symbol scan, VWAP, Finviz short fallback, scan cache
[x] core/market_hours.py — CT session helpers, next_radar_resume_label
[x] GET /api/squeeze/picks | /api/squeeze/status; POST /api/admin/squeeze-scan
[x] Cockpit intraday squeeze panel + Today's v3 pick lane labels
[x] Admin squeeze status card; core/db.py pool max 25 + kill-status bundled query

PR #61–#62 (commits 37c5db6, 376bf8c, _pr_version 61–62):
[x] core/squeeze_outcomes.py — ghost_squeeze_outcomes table, record on Telegram/candidate, EOD resolve
[x] GET /api/squeeze/daily-log; POST /api/admin/squeeze-resolve; squeeze_eod scheduler job
[x] Cockpit squeeze accountability log (#squeeze-daily-log-section) + admin daily log card
[x] Truth-mode UX — hero-truth-strip, v3 pick lane post-falsification copy, BIAS ONLY gauge
[x] tests/test_squeeze_outcomes.py

PR #63 (commit 66da1f9, _pr_version 63):
[x] core/squeeze_live_drift.py — first alert buy vs live quote; enrich picks + daily log
[x] GET /api/squeeze/picks + /api/squeeze/daily-log — live_drift[] + per-row gap fields
[x] Cockpit — Ghost prediction vs live summary + Live vs alert columns (radar + daily log)
[x] tests/test_squeeze_live_drift.py
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
[x] Intraday squeeze radar + scorecard (PR #55-#59): CT session 3 AM–7 PM, Telegram path separate from v3
[x] Phase 1+2 blueprint modules wired (PR #60, 91dc94c) — see COMPLETED_PHASE1_2 above
[x] PR #60 prod-verified on Railway 2026-06-11 (operator) — see PROD_VERIFY_2026_06_11
[x] Squeeze daily log + truth-mode UX (PR #61–#62) — see COMPLETED_PHASE1_2 above
[x] PR #61–#62 prod-verified on Railway 2026-06-12 (operator + browser agent) — see PROD_VERIFY_2026_06_12
[x] PR #63 prod-verified on Railway 2026-06-15 (agent API curl) — see PROD_VERIFY_2026_06_15
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
--- 2026-06-28–29 | PR #82–#90 — Super Ghost foundation, unified console, live coverage gate ---
Context: User clarified Ghost is a prediction-market/intelligence product, not an
auto-trading/broker bot. Mission: build toward the strongest possible stock prediction
platform while staying honest — no guaranteed profit, no fake accuracy, no fake data.

Phase 1 — Super Ghost intelligence layer (PR #82–#83):
  - Added market-regime detection and conviction adjustment (SPX/Nasdaq/sector/VIX/Fed/CPI context)
  - Added optional real AI brief on /api/wolf/super-ghost?ai=1 using Ghost's Anthropic integration
  - Fixed model default to the proven Ghost Ask model: claude-haiku-4-5-20251001
  - Live verified ai_brief.available=true after PR #83

Phase 2 — Prediction Truth Ledger (PR #84):
  - Added core/super_ghost_ledger.py and super_ghost_predictions table
  - Routes: log, history, accuracy, if-followed, resolve
  - Hourly resolver job added; resolve is auth-gated
  - Purpose: every prediction can be judged later; wins include correct DOWN calls, not only UP calls

Phase 3 — Max Build roadmap (PR #85):
  - Added docs/SUPER_GHOST_MASTER_BUILD.md
  - Added docs/super_ghost_master_plan.json (98 requirements, 11 phases)
  - Added tests/test_master_plan.py so the plan itself is CI-enforced
  - Next true accuracy phase identified: P1 Data Coverage Upgrade

Phase 4 — Unified Liquid Glass prediction console (PR #86–#87):
  - / and /picks now serve ghost_console.html, the unified prediction console
  - Preserved /legacy-picks and /cockpit
  - Sidebar tabs: Overview, Top stocks, Bullish, Today, 48 hour, This week, Live mirror, Health
  - Local prediction pool controls; Top Picks gated by truth-ledger win rate
  - Added GET /api/market/session/{symbol} for live open/high/low/price mirror

Phase 5 — Data Coverage Upgrade + hard trust gate (PR #88–#90):
  Root cause: live Super Ghost coverage was 7/25 because _fetch_live_snapshot sourced
  price history + fundamentals from yfinance. Yahoo blocks Railway IPs, starving the
  existing scorers. The scorers were not the problem; the live data path was.

  - PR #88:
    * core/market_history.py added
    * core/sec_fundamentals.py added
    * EPS YoY + revenue YoY via SEC XBRL
    * current-price fallback via existing 5-tier spot chain
    * hard gate MIN_COVERAGE_FOR_AB=18: no A/B grade and no HIGH-CONVICTION below 18/25
    * GET /api/wolf/super-ghost/coverage added
  - PR #89:
    * get_daily_history() now delegates first to production-proven _fetch_ohlcv chain:
      Alpaca SIP -> IEX -> Polygon -> yfinance -> Stooq
  - PR #90:
    * Generic ticker->CIK support for SEC fundamentals; common large-cap built-in map
      and best-effort SEC ticker index

Live verified on production 2026-06-29:
  - GET /api/_version: sha=5bc05a0, _pr_version=88, app_version=2.5.0
  - GET /api/wolf/super-ghost/coverage?symbol=WOLF: 21/25, meets_ab_gate=true
  - AAPL: 19/25, meets_ab_gate=true
  - NVDA: 20/25, meets_ab_gate=true
  - Deployed report carries coverage.min_for_ab_grade=18 and meets_ab_gate metadata
  - Invariant verified: if coverage <18, no A+/A/B+/B grade and no HIGH-CONVICTION

Tests:
  - make test: 503 passed, 3 deselected/skipped, 2 warnings
  - make test-compile: exit 0
  - Focused suite: 34 passed (coverage gate, Super Ghost, master plan)

Important honesty note:
  WOLF meeting coverage gate does NOT mean "buy" or "high confidence." Live output
  can still be grade F / NO EDGE — WATCH ONLY if the evidence is weak. That is correct.
  Coverage is a prerequisite for trust, not a promise of profit.

PR #93 Learning Brain shipped after the evolution directive:
  - Every resolved Super Ghost prediction can become a learning event.
  - If Ghost predicts a target too low/high (e.g. $5 -> $7), it records the mistake type and lesson.
  - Learning profiles can apply bounded future confidence/conviction/target adjustments after enough samples.
  - Learning is visible via /api/wolf/super-ghost/learning and the console Health row.

PR #91 follow-up polish shipped after the review:
  - Added persistent post-falsification banner outside Health.
  - Replaced "resolved sample" jargon with "completed predictions" copy.
  - Hid duplicate top-tab chrome; sidebar nav is the single visible nav source.
  - Deployed as sha 3a83893, _pr_version 91; 504 tests passing.

Remaining P1 work:
  - Form 4 insider parser
  - 13F institutional parser
  - analyst revisions / target-change feed
  - options chain / unusual activity
  - macro event calendar + CPI/Fed surprise classifier
  - guidance/news event classifier + dedup
  - server-persisted prediction pool + richer chart overlays

--- 2026-06-25–26 | PR #70–#81 — Comprehensive security + reliability audit (12 PRs) ---
Context: User asked "is ghost working perfect now can i trust the predictions?"
This triggered a multi-phase audit spanning 2 days, 12 PRs, and 3 external agent
audit passes. All 13 original findings + 10 continuation findings resolved.

Phase 1 — Production incident response (PR #70–#76, 2026-06-25):
  - yfinance JSON parse errors on EVERY symbol (Yahoo blocking Railway IPs)
  - Alpaca rate-limit storm (45 calls/60s, limit 30)
  - Circuit breaker infinite half-open probe loop — breakers logged "OPEN" but never blocked
  - API rate limiter 120→300 RPM (cockpit ~25 parallel calls on load)
  - All raw yfinance calls gated behind _yfinance_cb across 6 modules
  - Watchlist-membership filter on REAL_TRADE_WHERE + write-side guard
  - Confidence gates restored: MIN_ALERT_CONFIDENCE 0.55→0.75, OBJECTIVE_BOOTSTRAP_MIN_CONF 0.6→0.78
  - RDFN removed from STOCK_SYMBOLS

Phase 2 — External agent audit (PR #77–#78, 2026-06-26):
  Agent ran deep read-only audit against ed541c4. Found 13 findings (F01–F13).
  All fixed: unauth portfolio routes, raw yfinance bypasses, sentiment floor bypass,
  public test-alert, CRON_SECRET fail-open, double delay, 5-tier spot chain,
  degraded mode, scheduler overlap, XFF hardening, Playwright, breaker tests,
  wolf_price alias.

Phase 3 — Continuation audit (PR #79, 2026-06-26):
  Agent continued audit. Found 5 more findings (C01–C05). C01 was false alarm
  (dirty working tree). Fixed: NaN sanitization in Polygon/Stooq, Telegram dedup
  conditional on _send(), dead-letter admin UI, OAuth CIMD SSRF hardening.

Phase 4 — Third-pass audit (PR #80, 2026-06-26):
  Agent ran third pass. Found 10 findings (GP-A01–GP-A10). Fixed 9: Ghost Ask
  portfolio leak, Polygon/Stooq NaN, check_feeds 5-tier, Playwright hidden element,
  cockpit 401 handling, reconcile double-resolve, train endpoint lock, morning card
  dedup after send, OAuth redirects. GP-A03 deferred to PR #81.

Phase 5 — GP-A03 + War Room (PR #81, 2026-06-26):
  - yfinance wrapper (core/yfinance_client.py) + api/wolf_endpoints.py monkeypatch
  - Zero raw yfinance calls remain in the codebase
  - War Room endpoint (POST /api/wolf/war-room) — 6-agent equity research pipeline

Final state: 426 tests passing, 3 skipped, 2 warnings. Clean working tree.
All 23 audit findings resolved across 12 PRs.

--- 2026-06-15 | PR #63 prod verification (agent API curl, Railway tender-benevolence) ---
Context: live vs alert drift shipped (66da1f9). Compare first Telegram alert buy to live quote
on squeeze radar, daily log, and API before EOD resolve.

Agent-verified on prod 2026-06-15 (~10:16 AM CT):
  - GET /api/_version: _pr_version 63, git_sha_short 66da1f9, deploy_id 66258b11
  - GET /api/squeeze/daily-log: live_drift[] 18 symbols; pending rows have live_price/gap_pct/drift_status
  - GET /api/squeeze/picks: live_drift key present; 0 rows (alert_history empty in-process — fills after Telegram)
  - /cockpit HTML: sq-drift-block, Ghost prediction vs live, Live vs alert, _sqDriftSummaryBlock
  - GET /api/squeeze/daily-log?session_date=2026-06-12: 17 resolved (5 WIN, 5 LOSS, 7 NEUTRAL) — EOD OK

Open / next:
  - Confirm live_drift board on cockpit during active Telegram session (browser pass optional)
  - Weekly ops checklist first full pass
  - Passive label accumulation toward Phase 3 squeeze ML retrain

--- 2026-06-12 | PR #61–#62 prod verification (operator + browser agent, Railway tender-benevolence) ---
Context: squeeze daily log + truth-mode UX shipped (37c5db6, 376bf8c). Operator ran
follow-up browser verify after initial agent missed #squeeze-daily-log-section.

User-verified on prod 2026-06-12 (~10:54–11:06 AM CT):
  - GET /api/_version: _pr_version 62, git_sha_short 376bf8c; cockpit deploy badge matches
  - GET /api/squeeze/daily-log: 11 rows 2026-06-12 (8 telegram), all pending (correct pre-EOD)
  - /cockpit #squeeze-daily-log-section: SQUEEZE ACCOUNTABILITY LOG; AMC pending row shown
  - /cockpit #hero-truth-strip: POST-FALSIFICATION · WR 28.6% 2W/5L · expectancy +0.28%
  - /cockpit #v3-pick-label: v3 pick lane · post-falsification (not squeeze subheader)
  - Squeeze radar live: ~35/44 priced, 4 Telegram alerts, AMC ACTIVE
  - Ghost score WATCHING 14d · 0 saved · regime gate binding (expected post-falsification)
  - Journal POST-FALSIFICATION MODE; closed-after-cutover 25% WR

Not yet verified this session:
  - EOD resolve after 3 PM CT (11 rows should get session OHLC + WIN/LOSS/NEUTRAL)

Open / next:
  - Confirm EOD resolve 2026-06-12 PM
  - Weekly ops checklist first full pass
  - Passive label accumulation in ghost_squeeze_outcomes toward Phase 3 squeeze ML retrain

--- 2026-06-11 | PR #60 prod verification (operator, Railway tender-benevolence) ---
Context: operator pasted live admin + cockpit + deploy logs after Phase 1+2 ship.
Sandbox cannot reach Railway; this entry records operator-confirmed prod state.

User-verified on prod 2026-06-11 (~12:14 AM CT):
  - Railway deploy 7367631c Active; Python 3.13.13; build from main (checklist b20fff6)
  - /admin: Blueprint Phase 1 on (regime cal, SMA5 bypass, squeeze_ml_v2); Phase 2 cards load
  - /admin: kill conditions all clear; no connection pool exhausted
  - /admin: squeeze PAUSED overnight, resumes 3:00 AM CT; leaders 0 overnight (expected)
  - /admin: gate-status WOLF up_prob 0.5373 vs effective floor 0.5380 (prob_low -0.0007)
  - /cockpit: contract banner + POST-FALSIFICATION MODE; deploy b20fff6 shown
  - /cockpit: squeeze radar offline overnight copy correct; v3 WATCHING 13d silent OK
  - Logs: REGIME GATE SMA5 bypass Trend-up WOLF; Cycle 0/0 picks; market_hours=False OK
  - Logs: Alpaca SIP 403 → IEX fallback (not a regression); yfinance WOLF flake overnight
  - Performance log: 187 scan cycles / 7d, 44 scanned, binding v3_regime_gate

Not yet verified this session (watch next):
  - GET /api/_version _pr_version: 60 (operator did not paste curl; admin UI confirms Phase 1+2)
  - First post-deploy 3 AM CT squeeze scan with leaders populated — **done 2026-06-12** (see 2026-06-12 session log)

Open / next:
  - Weekly ops checklist during CT session
  - Passive accumulation (drift samples, squeeze labels) — no Phase 3 code yet

--- 2026-06-10 | Phase 1+2 blueprint + squeeze radar + post-falsification contract ---
Context: operator chose "Phase 1 + Phase 2" (not product-only). Falsification gate
already tripped (~28% WR, CI excludes 80%). Goal: honest repositioning, wire blueprint
probes, keep squeeze radar production-ready for next CT session.

Two lanes (independent):
  v3 picks     — ~3-day gated XGBoost holds; silent when regime/objective gates bind
  squeeze radar — intraday RVOL + move; 3 AM–7 PM CT; separate Telegram path

Shipped squeeze lane (commits d7477d1 .. c0bad2e, _pr_version 55–59):
  d7477d1 — squeeze scorecard (Setup/Trigger/Confirm), CT radar, probability targets
  d17ffe3 — overnight panel honest copy; persist data/squeeze_last_scan.json
  3d9f4b9 — cockpit lane labels (Today's v3 pick vs intraday squeeze)
  c0bad2e — admin squeeze card; db pool max 25; kill-status single checkout fix

Shipped Phase 1+2 (commit 91dc94c, _pr_version 60):
  Phase 1:
    - core/ghost_contract.py, cockpit contract banner, pick-journal POST-FALSIFICATION copy
    - core/regime_calibration.py wired in signal_engine (effective_min_win_proba, SMA5 bypass)
    - core/squeeze_ml_v2.py blended into squeeze_scorecard (60% ML / 40% heuristic)
  Phase 2:
    - core/regime_classifier.py, news_sentiment (lexicon + fetch_news_sentiment)
    - core/feature_drift.py, core/options_flow.py
    - GET /api/ghost/{contract,blueprint,regime,drift,sentiment,options}
    - admin Blueprint / feature drift / options flow cards
    - tests/test_ghost_phase12.py; 403 tests passing at push

Prod verify: PR #60 completed 2026-06-11; PR #61–#62 completed 2026-06-12 — see session logs.
  Squeeze first-wake check: verified 2026-06-12 AM CT session.

Open / Phase 3:
  - Retrain squeeze ML v2 from labeled outcomes (baseline weights are priors only)
  - FinBERT sentiment; KL drift; Polygon options/GEX; per-regime isotonic maps
  - HMM regime detector still deferred (~250 trading days too thin)
  - Paid feed for Key Stats / Analyst / Short Interest

--- 2026-06-07 | Open pick review (change mind mid-trade) ---
Context: operator wanted Ghost always tracking — withdraw or refresh when live scans
no longer support an open pick (not locked until expiry).

Shipped:
  - core/pick_review.py — review_open_picks each cycle before save
  - Outcome WITHDRAWN (mark-to-market P&L, not WIN/LOSS)
  - Withdraw on: regime gate, prob_low, meta gate, confidence floor, supersede on
    entry move (GHOST_SUPERSEDE_ENTRY_PCT) or confidence shift (GHOST_SUPERSEDE_CONF_DELTA)
  - GHOST_WITHDRAW_MIN_AGE_MIN=15 grace after fire; GHOST_WITHDRAW_NOTIFY=1 Telegram
  - Ghost Ask + cockpit WITHDRAWN styling

--- 2026-06-07 | Pre-market watchlist scans ---
Context: operator asked Ghost to include pre-market (previously scans skipped 4:00-9:30 AM CT).

Shipped:
  - GHOST_PREMARKET_SCAN=1 (default): run_prediction_cycle scans full watchlist pre-open
  - Market scan cadence: 30m interval during pre-market when enabled
  - core/prices.get_extended_session: prior close, session price, gap_pct
  - predict_live_ex: overlays extended-hours price on last daily bar; scores.extended_session
  - GHOST_PREMARKET_FLOOR_BUMP=0.03 extra confidence required pre-open
  - Ghost Ask + cockpit copy updated for pre/after-hours sessions
  - tests/test_premarket_scan.py

--- 2026-06-07 | Full watchlist coverage + daily forecast panel + training reliability ---
Context: operator wanted all 44 watchlist symbols trained, cockpit panel reorder, and
daily prediction UI. Root training failure was batch OHLCV fetch returning empty under
Alpaca rate limits (14 symbols showed n_samples=0 despite 126+ samples on retry).

Shipped (commits c088b05 .. 8ba8143):
  - Backend performance log (ghost_perf_cycles/symbol_evals/events) + cockpit panel
  - Daily Prediction Panel (next session predict row + last session market row)
  - Cockpit: Ask Ghost, WOLF play, Prediction vs Reality moved below My Portfolio
  - Training: OHLCV retry/cache/2y default, inter-symbol delay, watchlist peer pool,
    adaptive WF floors for thin tickers (STUB), relaxed Railway gates
  - Fixed Railway STOCK_SYMBOLS (was TSLA,META,AMZN,T -> official 44 list)
  - RDFN daily forecast: scorecard no longer hardcodes 3mo; uses 2y fallback + stale flag
    (RDFN delisted ~2025-06-30 after Rocket acquisition)

Prod verified 2026-06-07 (user/agent):
  - /api/v3/status: models 44/44, watchlist_missing_models []
  - /api/_version git_sha_short 8ba8143
  - /api/wolf/daily-forecast-scorecard?symbol=RDFN: 16 days, data_stale=true,
    last_bar_date 2025-06-30
  - Use POST /api/v3/train?force=true (async) for full retrains; /api/v3/train/sync
    502s on Railway proxy timeout

Open / deferred:
  - News manual import: already live at /admin; wait on bulk use — Finnhub auto-cycle OK
  - Relaxed V3 gates trade quality for coverage on edge-case symbols; monitor pick journal
  - RDFN and other delisted names: forecasts show stale last-trade data only

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
