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
  7. SESSION_LOG is the handover log — read it to understand what happened in the
     last session and what's in flight. Update it at the end of every session.

LAST UPDATED: 2026-07-06 — PR #135 audit honesty fixes + DOWN-lane governance reset deployed
"""

# ============================================================
# SESSION_LOG — Handover log for the next AI agent
# ============================================================
# Read this FIRST. It tells you what happened, what's deployed,
# what's in flight, and what needs attention next.
# ============================================================

SESSION_LOG = {
    "session_date": "2026-06-30 → 2026-07-04",
    "session_span": "Multi-day: initial health check → bug discovery → autonomous execution → P0 audit → Phase 0-2 model overhaul → forensic audit → GO verification → PR #129 tech-debt cleanup",
    "handover_to": "Next AI agent picking up this project",

    # ── WHAT'S DEPLOYED RIGHT NOW ──
    "production": {
        "url": "https://ghost-protocol-v2-production.up.railway.app",
        "railway_project": "tender-benevolence",
        "pr_version": 135,
        "git_sha": "5d82d39",
        "app_version": "2.5.0",
        "health": "95/100 (live-verified 2026-07-05 post PR #130 deploy)",
        "degraded": False,
        "tests": "687 passed, 3 skipped",
        "playwright_e2e": "33/33 (desktop + mobile, as of PR #127)",
        "release_gates": "GO — all checks passed (first fully clean end-to-end in project history)",
        "models_trained": "126 models stored incl. DOWN direction (e.g. ABCL_down) — Phase 2 DOWN lane deployed; V3_DOWN_SIGNALS_ENABLED stays 0 until shadow track record exists",
        "accuracy_contract": "70% target, balanced mode",
        "research_mode": "exited (63 resolved picks, >15 threshold)",
        "breakers": "yfinance open 4/5 — cooldown cycling, not degraded",
    },

    # ── WHAT HAPPENED THIS SESSION ──
    "session_summary": [
        "PR #114: Research pick mode + breaker diagnostics + prev_close 5-tier chain + confidence caps",
        "PR #115: P0 audit — Kelly formula corrected (f* = p-(1-p)/b), breaker auto-recovery fixed, research pick hardening, auth-gated writes, async fixes",
        "PR #116: Phase 0-1 — FEATURE_COLS 33→49 (8 macro + 8 cross-sectional), point-in-time training data, auto-log watchlist (43× multiplier), dead promotion gate removed",
        "Phase 2 (LOCAL, NOT DEPLOYED): DOWN model — backtest_symbol returns (up, down) tuple, _train_one_direction helper, dual model training, predict_live_ex picks stronger signal, load_model accepts direction param",
        "PR #122-#125: Accuracy contract 70%, feed resilience (null-bars guard, seeder deadlock, OHLCV storms), precision gate global pool, stop geometry precision",
        "PR #126: Full forensic audit fixes — 13 critical + 15 high issues resolved (regime modifier sync, 3,600 lines dead code removed, dependency inversion fixed, ghost_state DDL centralized, dev-mode auth bypass hardened, cache race conditions fixed, import integrity baseline cleared)",
        "PR #127: GO verification — 686 tests, 33/33 Playwright, 0 critical health findings, 0 new ERROR signatures, release gates all green",
        "PR #135 (2026-07-06): AUDIT HONESTY FIXES (external read-only audit, verified w/ 2 corrections). GOVERNANCE DRIFT FOUND+FIXED: V3_DOWN_SIGNALS_ENABLED was '1' in Railway against the documented keep-at-0 plan, undocumented by any ledger entry — reset to 0 until a DOWN shadow track record exists. Audit corrections: stacking ensemble was ALREADY on (V3_ENSEMBLE=stacking), DOWN lane was ALREADY on (not off as audit claimed) — auditor read code defaults, not live env. Fixes: (a) /api/v3/status per-model fireable_now + fire_block_reason mirroring _evaluate_lane static checks (audit found 11 precision_ok displayed vs 0 actually fireable), base_rate_rider flag (accuracy<=natural_rate+2pp), proven_skill (edge>0 AND wf_edge>0); (b) V3_MIN_WF_EDGE default -0.05 -> 0.0 (negative out-of-time edge never counts toward 70%); (c) /api/stats win_rate_wilson_lb95_pct (3/10 live record = 10.8% Wilson floor, not '30%'); (d) /api/symbol-accuracy marked legacy:true with crypto-era warning; (e) scorecard metrics_note: overall_pct = OHLC level closeness telemetry, direction_hit_rate_pct is the headline. 730 tests.",
        "PR #134 (2026-07-06): NEWS EVENT LAYER + SEC UN-BLINDING (merged Fugu+Claude plan). (a) ROOT-CAUSE FIX: SEC ticker index returned 403 to the old User-Agent (no contact email) — 42/43 watchlist symbols had NO CIK and were fundamentals-blind since inception; new UA + hourly retry + warning-level logging; 10,415 tickers now resolve. (b) core/news_events.py: ghost_news_raw_articles + ghost_news_events tables, deterministic 18-type classifier (dilution, going_concern, guidance, FDA, M&A rumor-vs-confirmed, ...), asof_ts point-in-time discipline, dedupe one-event-per-type-per-day. (c) core/news_ingest.py: Alpaca+Finnhub providers, 15-min scheduler job, /api/news/events + cron-gated /api/news/ingest. (d) news_shadow_v2 registered as 10th brain (v1 FROZEN as baseline — never mutate a ledgered model_id); dead feed reads as news-unavailable HOLD, never neutral. (e) core/news_defense.py tripwire: fresh bearish high-materiality event on active UP pick → warn/withdraw; SHIPPED DARK (NEWS_DEFENSE_ENABLED=0, mode=warn). LLM classifier layer deliberately deferred. 722 tests. LIVE-VERIFIED 2026-07-06: _pr_version=134; SPCE eps+revenue_growth now RESOLVE (were critical-missing); ingest cycle 43 symbols/51 articles/15.9s both providers OK; real events extracted day one (XPO analyst_upgrade, PFE analyst_downgrade + earnings_beat); 10 brains in live manifest incl news_shadow_v2.",
        "PR #133 (2026-07-05): seasonal_shadow_v1 added as 9th shadow brain — core/seasonality.py computes the symbol's ~4-year record for the current 5-day calendar window vs its own baseline (24h in-process cache). Commits only on excess >=2.5% with >=75% year consistency; confidence hard-capped at 0.65 (n<=4 windows is thin evidence by construction). Plumbing: OHLCV days_map +5y, Alpaca bar limit 1000->10000. Born from the post-July-4th study: 8/42 watchlist symbols positive all 4 years vs ~2.6 expected by chance. Live-verified: ABCL lean UP (+11.3% excess, 4/4), WOLF correctly NONE (0.5 consistency).",
        "PR #132 (2026-07-05): contrarian_shadow_v1 added as 8th shadow brain — inverts every committed production call (HOLD stays HOLD), mirrors risk geometry around entry. Tests the operator's anti-signal hypothesis with real evidence: under 5-day sign resolution its win_rate is the exact complement of production's on committed calls. Shadow-only; judge via shadow profiles once samples accumulate.",
        "PR #131 (2026-07-05): ValueError handler triage — 422 only for route-file origins; internal/core origins return 500 with logged traceback (no more bugs disguised as invalid_input). 4 new tests, 691 total.",
        "PR #130 (2026-07-05): God-object split — wolf_app.py 6,097→3,151 lines (82 endpoints → api/routes_admin|ghost_system|v3|wolf_ops|data, late-import + facade re-export pattern); signal_engine 2,747→2,141 lines (engine_config/indicators/features/calibration split out); all 86 silent except-pass blocks in core/ now call core.quiet.note_suppressed() (DEBUG log + COUNTS). 687 tests, live-verified: _pr_version=130, 10 moved endpoints 200, MCP connector OK.",
        "PR #129 (2026-07-04): tech-debt cleanup — 33 inline ghost_state DDL sites migrated to ensure_ghost_state(); global psycopg2→503 and ValueError→422 exception handlers; Chart.js vendored to /static with SRI-pinned CDN fallback; WCAG contrast remap (#444/#555/#666 → #808080/#888/#999) in cockpit+admin; aria-labels on admin inputs/selects and cockpit icon button; label-for on portfolio inputs; pulse animation on loading placeholders (reduced-motion safe); datetime.utcnow() removed (3 real sites); settings VERSION 2.1.0→2.5.0; redis dep removed; plaintext GHOST_OAUTH_SECRET scrubbed from this file (git history still has it — rotate). Live-verified: _pr_version=129, health 95, /static/chart.umd.min.js 200, MCP gate/kill-status OK.",
    ],

    # ── KEY ARCHITECTURE FACTS ──
    "architecture": {
        "engine": "XGBoost v3.2, TP/SL daily-bar labels, walk-forward validation",
        "features": "49 features (33 technical + 8 macro + 8 cross-sectional)",
        "data_feeds": "5-tier chain: Alpaca IEX → yfinance → Polygon → IEX → Stooq (deprecated)",
        "circuit_breakers": "5 breakers: yfinance (5/600s), alpaca (5/300s, 50/60s), finnhub, polygon, anthropic",
        "watchlist": "43 symbols in OFFICIAL_WATCHLIST (config/symbols.py)",
        "super_ghost": "25-point checklist, coverage gate ≥18/25 for A/B grade",
        "kelly_sizing": "Corrected formula f* = p - (1-p)/b",
        "two_lanes": "v3 picks (gated, often silent) + Squeeze radar (intraday)",
        "primary_symbol": "WOLF (Wolfspeed Inc, NYSE)",
        "era": "Post-falsification — ABANDON_80_CLAIM",
    },

    # ── FILES HEAVILY MODIFIED THIS SESSION ──
    "files_touched": {
        "core/signal_engine.py": "FEATURE_COLS 33→49, backtest_symbol returns (up,down) tuple, _train_one_direction extracted, _simulate_down_tp_sl bridge, load_model(symbol, direction), predict_live_ex dual-model scoring",
        "core/prices.py": "5-tier prev_close chain, 24h persistent cache, Stooq deprecated",
        "core/circuit_breaker.py": "auto_recover only on genuine cooldown expiry, wired into health check",
        "core/prediction.py": "Research pick mode, cold-start + stall detection, daily cap across all cycles",
        "core/kelly_sizing.py": "Formula corrected from edge/odds to p-(1-p)/b",
        "core/macro_regime.py": "FRED_API_KEY empty string check, _build_historical_macro_series, get_macro_features_for_date",
        "core/tp_sl_resolve.py": "simulate_down_tp_sl_label for DOWN label generation",
        "core/super_ghost_ledger.py": "log_prediction commits before side-writes, auto_log_watchlist 43× daily with 20h guard",
        "core/regime.py": "Regime modifier synced with ghost_score_spec (0.90/0.80 → 0.95/0.90)",
        "core/db.py": "ensure_ghost_state() centralized (was 37 duplicate DDL statements)",
        "core/squeeze_monitor.py": "_squeeze_risk_tag moved here from api/wolf_endpoints.py (dependency inversion fix)",
        "core/news.py": "_seen_headlines capped at 5000",
        "api/wolf_endpoints.py": "Auth-gated writes, asyncio.to_thread for Claude, _squeeze_risk_tag re-exports from core, _CACHE lock added",
        "wolf_app.py": "Breaker/research status endpoints, auto-recovery in health check, _cron_ok requires GHOST_DEV_MODE=1, _COCKPIT_DB_CACHE lock added, _pr_version=127",
        "tests/conftest.py": "GHOST_DEV_MODE=1 autouse fixture",
        "tests/test_import_integrity.py": "Baseline cleared — all 625 first-party imports resolve",
    },

    # ── FILES REMOVED THIS SESSION ──
    "files_removed": [
        "core/stock_engine.py — dead code, imported non-existent core.pattern_tracker",
        "core/world_feed_fusion.py — 1,107 lines, never imported",
        "engines/startup.py — ~1,000 lines, self-declared deprecated",
        "core/model.py — deprecated legacy XGBoost, superseded by signal_engine",
        "routes/schema.py — duplicate /api/schema endpoint, router never mounted",
    ],

    # ── WHAT'S IN FLIGHT / NOT YET DONE ──
    "in_flight": {
        "phase_2_down_model": {
            "status": "DEPLOYED (verified 2026-07-04)",
            "description": "DOWN model training + dual-direction prediction is on main and live; /api/v3/status shows *_down models stored. DOWN firing stays shadow-only (V3_DOWN_SIGNALS_ENABLED=0) until a shadow track record exists.",
        },
        "ghost_state_centralization": {
            "status": "DONE (PR #129)",
            "description": "All 33 app-code call sites now call ensure_ghost_state(); scripts/calibrate_confidence_slope.py intentionally keeps inline DDL (standalone script).",
        },
        "live_fire_test": {
            "status": "UNTESTED",
            "description": "Monday open — gate flips on moving tape, intraday pick resolution at target/stop. Reconnect Ghost connector for live watch.",
        },
        "optional_github_secrets": {
            "status": "OPTIONAL",
            "description": "TEST_DATABASE_URL activates DB integration job; CRON_SECRET upgrades health audit to deep POST path. Both slot in with zero workflow changes.",
        },
    },

    # ── KNOWN ISSUES / TECHNICAL DEBT ──
    "known_issues": [
        "wolf_app.py split (PR #130): 82 endpoints moved to api/routes_{admin,ghost_system,v3,wolf_ops,data}.py — 3,151 lines remain (helpers, health cluster, pages, middleware, ws). Moved endpoints late-import shared helpers from wolf_app; wolf_app re-exports every moved name (facade). Do NOT top-level-import wolf_app from a routes module (cycle).",
        "signal_engine split (PR #130): config knobs → core/engine_config.py, indicators → core/engine_indicators.py, features → core/engine_features.py, calibration → core/engine_calibration.py; 2,141 lines remain (OHLCV chain + training + inference stay — tests monkeypatch their interplay on core.signal_engine, and _ProbaEnsemble must stay for stored-model pickle paths).",
        "except-pass sweep (PR #130): all 86 silent blocks in core/ now call core.quiet.note_suppressed() — DEBUG-logged + counted in core.quiet.COUNTS; raise logger ghost.suppressed to DEBUG to audit live.",
        "GHOST_OAUTH_SECRET ROTATED 2026-07-04 (leaked value in git history is now dead — cannot approve connector sessions or forge tokens). Connector survived rotation via DB-backed refresh token. New value lives only in Railway env vars; keep it out of this file.",
        "ValueError handler fixed (PR #131): 422 only when raised in a route file (wolf_app.py, api/, portfolio_routes); deeper origins → 500 + ERROR-logged traceback. Covered by tests/test_value_error_handler.py.",
        "scripts/calibrate_confidence_slope.py keeps its inline ghost_state DDL by design (standalone script, no app imports)",
    ],

    # ── QUICK VERIFICATION COMMANDS ──
    "verify_commands": {
        "production_version": "curl -s https://ghost-protocol-v2-production.up.railway.app/api/_version | python3 -m json.tool",
        "production_health": "curl -s https://ghost-protocol-v2-production.up.railway.app/health",
        "production_breakers": "curl -s https://ghost-protocol-v2-production.up.railway.app/api/system/breakers | python3 -m json.tool",
        "production_models": "curl -s https://ghost-protocol-v2-production.up.railway.app/api/v3/status | python3 -m json.tool",
        "production_gate_status": "curl -s https://ghost-protocol-v2-production.up.railway.app/api/wolf/gate-status | python3 -m json.tool",
        "full_test_suite": "cd /Users/studio713/ghost-protocol-v2 && python3.13 -m pytest tests/ -q --tb=short",
        "import_integrity": "cd /Users/studio713/ghost-protocol-v2 && python3.13 scripts/check_import_integrity.py",
        "deploy": "cd /Users/studio713/ghost-protocol-v2 && railway up --environment production",
        "railway_variables": "cd /Users/studio713/ghost-protocol-v2 && railway variables list --environment production",
    },

    # ── GIT BRANCHES ──
    "branches": {
        "main": "Production — PR #127 deployed (44ebd3b)",
        "fix/accuracy-contract-70": "Has the Phase 2 DOWN model code + work log updates — NOT YET MERGED to main",
    },
}

# ============================================================
# LIVE SYSTEM — LAST VERIFIED 2026-07-04 (PR #127 deployed; 686 tests pass)
# ============================================================

PROD_VERIFY_2026_07_01_PR114 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "e02b8ec",
    "_pr_version": 114,
    "verified_at_ct": "2026-07-01",
    "tests": "590 passed, 3 skipped",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=e02b8ec, _pr_version=114, app_version=2.5.0",
        "breakers_endpoint": "GET /api/system/breakers -> yfinance:open, finnhub:closed, polygon:closed, alpaca:open, anthropic:closed",
        "degraded": "false, 2/2 open circuits at threshold",
        "health": "score=95",
        "change_pct": "WOLF +0.79%, SPCE +18.65%, DOMO +39.10%, LCID +33.11%, IQ +10.30%, FLNC -1.10%, HIMS +8.91%, ITRI +5.55%",
        "research_mode": "armed — < 15 resolved picks, confidence floor 0.55, v3 min_win_proba 0.40",
        "squeeze": "0 picks (market closed), 3/43 fetch",
        "v3_active": "0 picks (WOLF up_prob=0.34, below research threshold 0.40)",
    },
    "changes": [
        "Research pick mode: lowers confidence floor to 0.55 + v3 min_win_proba to 0.40 when < 15 resolved picks",
        "GET /api/system/breakers: per-breaker state, failure count, cooldown, rate-limit",
        "Prev_close 5-tier chain: Alpaca 1Day → 5-min bar → yfinance → Polygon → 24h cache",
        "Confidence caps: squeeze_confidence max 95, score_confirmation max 95, extreme short bonus halved",
        "Stooq deprecated (JS challenge wall)",
        "Degraded reasons now include half_open state",
        "_pr_version bumped to 114",
    ],
}












PROD_VERIFY_2026_06_29_PR113 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "9edd4ac",
    "_pr_version": 113,
    "verified_at_ct": "2026-06-29",
    "tests": "Authenticated admin 7/7; health audit PASS unresolved=0; lint/type-check/pytest/compile/live/error-signatures/health-audit/prelaunch/Playwright all pass",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=9edd4ac, _pr_version=113, app_version=2.5.0",
        "authenticated_admin": "login, diagnostics, admin health, portfolio read, logout, post-logout gating all pass",
        "authenticated_health_audit": "POST /api/health/audit?auto_fix=false with CRON_SECRET -> audit_status PASS, unresolved=0",
        "lint": "npm run lint -> pass",
        "type_check": "npm run type-check -> pass",
        "pytest": "python3 -m pytest tests/ -q -> 590 passed, 3 skipped",
        "compile": "make test-compile -> pass",
        "live_health": "npm run verify:live -> pass",
        "error_signatures": "npm run verify:error-signatures -> pass",
        "health_audit_public": "npm run verify:health-audit -> pass public fallback",
        "prelaunch": "npm run verify:prelaunch -> ALL PASS (7 checks)",
        "playwright": "npm run test:e2e -> 33 passed",
    },
    "key_fixes": [
        "Health audit cockpit static check updated to redesigned cockpit UI tokens",
        "Removed stale expectations for tab-stocks/tab-portfolio/function show(",
        "Authenticated health audit now reports PASS with zero unresolved findings",
    ],
    "known_issues": [
        "Rotate the temporary admin/cron secret shared during verification after this session",
    ],
}

PROD_VERIFY_2026_06_29_PR112 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "3e8ba89",
    "_pr_version": 111,
    "verified_at_ct": "2026-06-29",
    "tests": "lint pass; type-check pass; 590 passed, 3 skipped; compile pass; live/prelaunch/error-signature/health-audit pass; Playwright 33/33 pass",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=3e8ba89, _pr_version=111, app_version=2.5.0",
        "lint": "npm run lint -> pass",
        "type_check": "npm run type-check -> pass",
        "pytest": "python3 -m pytest tests/ -q -> 590 passed, 3 skipped",
        "compile": "make test-compile -> pass",
        "live_health": "npm run verify:live -> pass",
        "error_signatures": "npm run verify:error-signatures -> pass",
        "health_audit": "npm run verify:health-audit -> pass public fallback",
        "prelaunch": "npm run verify:prelaunch -> ALL PASS (7 checks)",
        "playwright": "npm run test:e2e -> 33 passed",
    },
    "key_fixes": [
        "Playwright cockpit flow uses current redesigned cockpit selectors",
        "Portfolio E2E verifies auth-gated security contract instead of unauthenticated mutation",
        "API surface E2E retries transient 429s with backoff",
        "Playwright script runs without NO_COLOR/FORCE_COLOR warning noise",
    ],
    "known_issues": [
        "Manual browser QA still recommended for authenticated admin login/logout because local verifier has no CRON_SECRET/admin cookie",
    ],
}

PROD_VERIFY_2026_06_29_PR111 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "28c8d77",
    "_pr_version": 111,
    "verified_at_ct": "2026-06-29",
    "tests": "CI test pass; post-PR112 Playwright confirms cockpit/portfolio contract",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=28c8d77, _pr_version=111, app_version=2.5.0",
        "cockpit": "Public cockpit no longer auto-fetches auth-gated portfolio; locked portfolio state visible",
    },
    "key_fixes": [
        "cockpit.html: render locked portfolio state by default and manual Load Portfolio button",
        "e2e: redesigned cockpit selectors replace removed legacy #cgrid/tab-panel assumptions",
        "e2e: portfolio mutation tests now assert 401 auth gating",
    ],
    "known_issues": [],
}

PROD_VERIFY_2026_06_29_PR110 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "8616306",
    "_pr_version": 108,
    "verified_at_ct": "2026-06-29",
    "tests": "verify:error-signatures pass; verify:health-audit pass public fallback",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=8616306, _pr_version=108, app_version=2.5.0",
        "error_signatures": "npm run verify:error-signatures -> PASS; gated diagnostics/audit skipped safely without admin/CRON secret",
        "health_audit": "npm run verify:health-audit -> PASS public history fallback; POST skipped because CRON_SECRET unset locally",
    },
    "key_fixes": [
        "check_error_signatures no longer false-fails on intentionally admin-gated /api/diagnostics",
        "check_error_signatures no longer false-fails on /api/health/audit when CRON_SECRET is unset locally",
        "Focused tests cover gated diagnostics and audit behavior",
    ],
    "known_issues": [],
}

PROD_VERIFY_2026_06_29_PR109 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "35da460",
    "_pr_version": 108,
    "verified_at_ct": "2026-06-29",
    "tests": "npm run lint pass; npm run type-check pass; 587 passed, 3 skipped; compileall pass",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=35da460, _pr_version=108, app_version=2.5.0",
        "lint": "npm run lint -> All checks passed",
        "type_check": "npm run type-check -> Success: no issues found in 5 source files",
        "test_suite": "python3 -m pytest tests/ -q -> 587 passed, 3 skipped",
        "compile": "make test-compile -> pass",
    },
    "key_fixes": [
        "Ruff cleanup across configured lint surface",
        "Type-check script now follows mypy.ini and checks production audit/prelaunch scripts",
        "All configured local quality gates pass cleanly",
    ],
    "known_issues": [],
}

PROD_VERIFY_2026_06_29_PR108 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "089a5fc",
    "_pr_version": 108,
    "verified_at_ct": "2026-06-29",
    "tests": "587 passed, 3 skipped; warning-clean",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=089a5fc, _pr_version=108, app_version=2.5.0",
        "health": "GET /health -> score 90",
        "top_pick_gate": "GET /api/wolf/super-ghost/top-pick-gate?symbol=WOLF&horizon=5 -> ok true, decision LOCKED",
        "test_suite": "python3 -m pytest tests/ -q -> 587 passed, 3 skipped, no warnings summary",
    },
    "key_fixes": [
        "Removed hardcoded default API_AUTH_TOKEN from startup self-call logic",
        "Startup predictions now skip unless API_AUTH_TOKEN is explicitly configured",
        "Static tests assert no default API auth token and no raw private keys/live secrets in public HTML",
    ],
    "known_issues": [],
}

PROD_VERIFY_2026_06_29_PR107 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "a74b71d",
    "_pr_version": 107,
    "verified_at_ct": "2026-06-29",
    "tests": "585 passed, 3 skipped; warning-clean",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=a74b71d, _pr_version=107, app_version=2.5.0",
        "health": "GET /health -> score 95",
        "top_pick_gate": "GET /api/wolf/super-ghost/top-pick-gate?symbol=WOLF&horizon=5 -> ok true, decision LOCKED",
        "super_ghost": "GET /api/wolf/super-ghost?symbol=WOLF -> ok true, NO EDGE — WATCH ONLY",
        "test_suite": "python3 -m pytest tests/ -q -> 585 passed, 3 skipped, no warnings summary",
    },
    "key_fixes": [
        "load_model validates metadata before model payload decode/unpickle",
        "Strict base64 decode and max payload size guard",
        "New trained models persist model_sha256 and model_payload_bytes",
        "If model_sha256 exists, load verifies hash before pickle.loads",
        "Legacy model rows only load after serve guards pass",
    ],
    "known_issues": [
        "Legacy DB model rows without SHA are still loadable after metadata guards; retraining will add SHA metadata",
    ],
}

PROD_VERIFY_2026_06_29_PR106 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "0e944aa",
    "_pr_version": 105,
    "verified_at_ct": "2026-06-29",
    "tests": "582 passed, 3 skipped; warning-clean",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=0e944aa, _pr_version=105, app_version=2.5.0",
        "test_suite": "python3 -m pytest tests/ -q -> 582 passed, 3 skipped, no warnings summary",
    },
    "key_fixes": [
        "pytest.ini filters known third-party sklearn/scipy L-BFGS-B disp/iprint deprecation noise",
        "Project warnings remain actionable; runtime behavior unchanged",
    ],
    "known_issues": [],
}

PROD_VERIFY_2026_06_29_PR105 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "8234a16",
    "_pr_version": 105,
    "verified_at_ct": "2026-06-29",
    "tests": "582 passed, 3 skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=8234a16, _pr_version=105, app_version=2.5.0",
        "top_pick_gate": "GET /api/wolf/super-ghost/top-pick-gate?symbol=WOLF&horizon=5 -> ok true, decision LOCKED, eligible false, blockers present",
        "console": "/picks includes Top Pick gate Health row and positive-if-followed/calibrated-evidence copy",
    },
    "key_fixes": [
        "core/super_ghost_top_picks.py: centralized backend Top Pick Evidence Gate",
        "Top Picks requires completed predictions, >=70% direction wins, >=60/100 precision, calibration readiness, positive if-followed evidence, and clear kill conditions",
        "GET /api/wolf/super-ghost/top-pick-gate endpoint",
        "Console Top Stocks gate consumes backend gate instead of partial local-only checks",
    ],
    "known_issues": [
        "Gate is intentionally locked until enough resolved evidence/calibration exists",
        "Top Picks remains evidence-only; no auto-trading or trade-now actions",
    ],
}

PROD_VERIFY_2026_06_29_PR104 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "83f208e",
    "_pr_version": 104,
    "verified_at_ct": "2026-06-29",
    "tests": "578 passed, 3 skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=83f208e, _pr_version=104, app_version=2.5.0",
        "regime_calibration": "GET /api/wolf/super-ghost/regime-calibration?symbol=WOLF&horizon=5 -> ok true, cold-start 0 profiles",
        "regime_rebuild_auth_gate": "POST /api/wolf/super-ghost/regime-calibration/rebuild without auth -> 401",
        "super_ghost_report": "GET /api/wolf/super-ghost?symbol=WOLF -> includes regime_calibration cold-start block with detected regime/setup bucket",
        "console": "/picks includes Regime calibration Health row",
    },
    "key_fixes": [
        "core/super_ghost_regime_calibration.py: regime/setup-specific calibration slices",
        "New super_ghost_regime_calibration_profiles table",
        "Buckets by market regime (risk_on/risk_off/high_volatility/mixed) and setup style (news/earnings/squeeze/thin_liquidity/analyst/general)",
        "Runtime lookup uses narrow symbol+regime+setup profile first, then broader safe fallbacks",
        "Public /regime-calibration endpoint and auth-gated /regime-calibration/rebuild",
        "Resolver job rebuilds regime calibration after broad range calibration",
    ],
    "known_issues": [
        "Regime slices are cold-start until enough resolved precision events exist per market/setup bucket",
        "Price adjustment remains bounded and long-plan-only until short-side calibration is validated",
    ],
}

PROD_VERIFY_2026_06_29_PR103 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "b2c3c50",
    "_pr_version": 103,
    "verified_at_ct": "2026-06-29",
    "tests": "569 passed, 3 skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=b2c3c50, _pr_version=103, app_version=2.5.0",
        "range_calibration": "GET /api/wolf/super-ghost/range-calibration?symbol=WOLF&horizon=5 -> ok true, cold-start 0 profiles",
        "range_rebuild_auth_gate": "POST /api/wolf/super-ghost/range-calibration/rebuild without auth -> 401",
        "super_ghost_report": "GET /api/wolf/super-ghost?symbol=WOLF -> includes range_calibration cold-start block; raw risk plan unchanged until enough precision samples",
        "console": "/picks includes Range calibration Health row",
    },
    "key_fixes": [
        "core/super_ghost_range_calibration.py: bounded adaptive range calibration from Precision Brain profiles",
        "New super_ghost_range_calibration_profiles table",
        "Target/stop multipliers derive from target_too_low, target_too_high, stop_too_wide, stop_too_tight, and low-precision patterns",
        "Super Ghost risk_plan can publish target_price_raw/calibrated, stop_loss_raw/calibrated, expected high/low/close zones, bull/bear cases, and invalidation level",
        "Public /range-calibration endpoint and auth-gated /range-calibration/rebuild",
        "Resolver job rebuilds range calibration after precision scoring",
    ],
    "known_issues": [
        "Range profiles are cold-start until enough precision profiles exist",
        "Calibration currently applies price-adjustment only to long-oriented UP risk plans; DOWN/HOLD keep raw plan until short-side range model is validated",
    ],
}

PROD_VERIFY_2026_06_29_PR102 = {
    "deploy_id": "ed610631-19c1-4c4e-a10d-a030656c9ba7",
    "git_sha_short": "c21bca2",
    "_pr_version": 102,
    "verified_at_ct": "2026-06-29",
    "tests": "561 passed, 3 skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=c21bca2, _pr_version=102, app_version=2.5.0",
        "precision_summary": "GET /api/wolf/super-ghost/precision?symbol=WOLF&horizon=5 -> ok true, cold-start 0 profiles/events",
        "precision_score_auth_gate": "POST /api/wolf/super-ghost/precision/score without auth -> 401",
        "squeeze_daily_log": "GET /api/squeeze/daily-log?days=7 -> rows include precision_score / precision_grade / mistake_type fields",
        "console": "/picks includes Precision brain Health row and Direction-vs-Precision EOD mirror copy",
    },
    "key_fixes": [
        "core/ghost_precision.py: pure target-stop truth vs price-precision scorer",
        "core/super_ghost_precision.py: durable Super Ghost precision events + profiles",
        "Top Picks gate now requires both >=70% directional wins and >=60/100 average precision",
        "Squeeze daily-log stores precision_score, precision_grade, mistake_type, and precision_json",
        "Console shows Mirror score and Precision separately, so a WIN cannot hide poor price accuracy",
    ],
    "known_issues": [
        "Precision profiles are cold-start until resolved Super Ghost ledger rows are scored",
        "Intraday same-bar path order is still unknown for squeeze MIXED rows; finer tape data is future work",
    ],
}

PROD_VERIFY_2026_06_29_PR101 = {
    "deploy_id": "Verified on PR #102 deployment after PR #101 merge",
    "git_sha_short": "e7cb673",
    "_pr_version": 101,
    "verified_at_ct": "2026-06-29",
    "tests": "554 passed, 3 deselected/skipped, 2 warnings during PR #101; PR #102 full suite 561 passed after merge",
    "live_acceptance": {
        "data_brain": "GET /api/wolf/super-ghost/data-brain?symbol=WOLF -> ok true, coverage payload present",
        "data_brain_history": "GET /api/wolf/super-ghost/data-brain/history?symbol=WOLF -> route live (cold-start allowed)",
        "refresh_auth_gate": "POST /api/wolf/super-ghost/data-brain/refresh without auth -> 401",
        "console": "/picks includes Data brain Health row",
    },
    "key_fixes": [
        "core/super_ghost_data_brain.py: expanded SEC/news/macro/options evidence collector",
        "New super_ghost_data_brain_snapshots table",
        "Super Ghost snapshot merges available Form 4 insider activity, guidance/catalyst context, and options flow payload",
        "Public /data-brain and /data-brain/history endpoints; auth-gated /data-brain/refresh",
    ],
    "known_issues": [
        "Provider depth varies by symbol; unavailable sources are explicit rather than fabricated",
        "Data Brain snapshots are cold-start until refresh/persist runs on production",
    ],
}

PROD_VERIFY_2026_06_29_PR100 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "8254319",
    "_pr_version": 100,
    "verified_at_ct": "2026-06-29",
    "tests": "549 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=8254319, _pr_version=100, app_version=2.5.0",
        "feature_store": "GET /api/wolf/super-ghost/feature-store?symbol=WOLF -> ok true, cold-start 0 snapshots",
        "leakage_audit": "GET /api/wolf/super-ghost/feature-store/audit?symbol=WOLF -> ok true, clean, 0 leaks",
        "snapshot_auth_gate": "POST /api/wolf/super-ghost/feature-store/snapshot without auth -> 401",
        "console": "/picks includes Point-in-time store Health row",
    },
    "key_fixes": [
        "core/super_ghost_feature_store.py: immutable point-in-time snapshots for Super Ghost reports",
        "New super_ghost_feature_snapshots table",
        "Source timestamp parser/walker catches future source timestamps",
        "Snapshots persist on log_prediction with ledger_id",
        "Public /feature-store and /feature-store/audit endpoints; auth-gated /feature-store/snapshot",
    ],
    "known_issues": [
        "Snapshot table is cold-start until new Super Ghost predictions are logged",
        "Raw external data source timestamps are limited by what upstream providers expose",
    ],
}

PROD_VERIFY_2026_06_29_PR99 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "cdf810b",
    "_pr_version": 99,
    "verified_at_ct": "2026-06-29",
    "tests": "544 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=cdf810b, _pr_version=99, app_version=2.5.0",
        "promotion": "GET /api/wolf/super-ghost/promotion?symbol=WOLF -> ok true, cold-start 0 reviews, requirements present",
        "promotion_review_auth_gate": "POST /api/wolf/super-ghost/promotion/review without auth -> 401",
        "console": "/picks includes Promotion gate Health row",
    },
    "key_fixes": [
        "core/super_ghost_promotion.py: conservative promotion review gate",
        "Durable super_ghost_promotion_reviews table",
        "Decisions: PROMOTE_CANDIDATE, KEEP_CHAMPION, KEEP_SHADOWING, RETIRE_CANDIDATE, INSUFFICIENT_EVIDENCE",
        "Gates for minimum rows/actionable calls/profit factor/win-rate delta/EV delta/false positives/drawdown",
        "Scheduler now runs promotion review after resolver, learning, lab, feature memory, and shadow resolution",
        "Public /promotion and auth-gated /promotion/review",
    ],
    "known_issues": [
        "Promotion reviews are cold-start until enough lab/shadow evidence accumulates",
        "Gate creates recommendations only; no automated model promotion yet",
    ],
}

PROD_VERIFY_2026_06_29_PR98 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "3cc0987",
    "_pr_version": 97,
    "verified_at_ct": "2026-06-29",
    "tests": "536 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=3cc0987, _pr_version=97, app_version=2.5.0",
        "shadow_summary": "GET /api/wolf/super-ghost/shadow?symbol=WOLF -> ok true, 7-model manifest",
        "shadow_models": "GET /api/wolf/super-ghost/shadow/models -> ok true, 7-model manifest",
        "auth_gates": "POST /shadow/run and /shadow/resolve without auth -> 401",
        "console": "/picks includes Shadow models Health row",
    },
    "key_fixes": [
        "core/super_ghost_shadow.py: 7 specialist shadow prediction brains",
        "Persistent tables: super_ghost_shadow_predictions and super_ghost_shadow_model_profiles",
        "Shadows generated whenever a Super Ghost prediction is logged",
        "Shadow resolver scores against parent Truth Ledger outcomes",
        "Specialists: technical, news, fundamental, macro, regime, learning-adjusted, ensemble",
        "Public /shadow /shadow/models and auth-gated /shadow/run /shadow/resolve",
    ],
    "known_issues": [
        "Shadow profiles are cold-start until logged shadow predictions resolve",
        "No auto-promotion; promotion gate remains a future PR",
    ],
}

PROD_VERIFY_2026_06_29_PR96 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "4ea2e24",
    "_pr_version": 96,
    "verified_at_ct": "2026-06-29",
    "tests": "529 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=4ea2e24, _pr_version=96, app_version=2.5.0",
        "models": "GET /api/wolf/super-ghost/models -> ok true, 1 production model",
        "feature_profile": "GET /api/wolf/super-ghost/feature-profile?symbol=WOLF&horizon=5 -> ok true, cold-start 0 profiles",
        "feature_score_auth_gate": "POST /api/wolf/super-ghost/features/score without auth -> 401",
        "console": "/picks includes Feature memory Health row",
    },
    "key_fixes": [
        "core/super_ghost_memory.py: Model Registry + Feature Attribution Memory",
        "New durable tables: model_versions, prediction_features, feature_outcomes, feature_profiles, model_contributions",
        "Every logged Super Ghost checklist item becomes a feature attribution row",
        "Resolved outcomes classify features as helped/hurt/underweighted/noise/missing",
        "Feature reliability profiles remember which evidence has worked by symbol/horizon",
        "Public /models /features /feature-profile and auth-gated /features/score",
        "Hourly resolver now also scores feature memory",
    ],
    "known_issues": [
        "Feature profiles are cold-start until logged predictions resolve and features are scored",
        "Feature attribution currently uses checklist scores; future PRs should add model-specific SHAP/importance values",
    ],
}

PROD_VERIFY_2026_06_29_PR95 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "aba64f3",
    "_pr_version": 95,
    "verified_at_ct": "2026-06-29",
    "tests": "523 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=aba64f3, _pr_version=95, app_version=2.5.0",
        "lab_summary": "GET /api/wolf/super-ghost/lab?symbol=WOLF&horizon=5 -> ok true, cold-start, 6 candidate manifest entries",
        "lab_run_auth_gate": "POST /api/wolf/super-ghost/lab/run without auth -> 401",
        "console": "/picks includes Research lab Health row",
    },
    "key_fixes": [
        "core/super_ghost_lab.py: Champion/Challenger shadow benchmark lab",
        "Persistent lab memory tables: super_ghost_lab_runs and super_ghost_lab_results",
        "Candidate policies: production_champion, coverage_gate, strict_confidence, grade_b_or_better, regime_aligned, edge_score_policy",
        "Benchmark metrics: win rate, false positives, avg signed return, profit factor, drawdown, score",
        "Conservative recommendation gates; no auto-promotion and no trading",
        "Public GET /api/wolf/super-ghost/lab and auth-gated POST /lab/run",
        "Hourly resolver now also runs the lab after learning",
    ],
    "known_issues": [
        "Lab is cold-start until enough resolved Super Ghost ledger rows exist",
        "Lab recommendations are shadow evidence only; no automated production model promotion yet",
    ],
}

PROD_VERIFY_2026_06_29_PR94 = {
    "deploy_id": "Railway auto-deploy from main",
    "git_sha_short": "f4156a4",
    "_pr_version": 94,
    "verified_at_ct": "2026-06-29",
    "tests": "517 passed, 3 deselected/skipped, 2 warnings; compileall exit 0",
    "live_acceptance": {
        "version_endpoint": "GET /api/_version -> sha=f4156a4, _pr_version=94, app_version=2.5.0",
        "learning_summary": "GET /api/wolf/super-ghost/learning?symbol=WOLF&horizon=5 -> ok true, cold-start",
        "target_calibration": "target_move_multiplier uses direction-correct rows only; target_calibration_samples exposed",
    },
    "key_fixes": [
        "Wrong-direction rows no longer dilute target-magnitude learning",
        "The $5 target -> $7 realized lesson stays target_too_low even with unrelated wrong-direction rows",
        "Win-rate and confidence still learn from all rows",
    ],
    "known_issues": [
        "Learning remains cold-start until enough resolved rows exist",
    ],
}

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
--- 2026-07-02–04 | PR #114–#123 — Phase 2 DOWN lane, Alpaca breaker fix, GHOST_ACCURACY_CONTRACT=70 ---
Context: Operator asked for production status, then auto-mode to build toward 70%+ live
accuracy. Diagnosed 6.7% live win rate (legacy picks under aggressive env). Shipped
accuracy contract; did NOT modify PROD_VERIFY or the structured SESSION_LOG dict above.

Phase 1 — Alpaca breaker thrash (PR #115, v118):
  P2-6 force-refresh busted cache when price >2% from cached high OR low — always true
  for >4% intraday range symbols. Fix: core/prices.py _intraday_breakout_pct() — refresh
  only when live trade breaks OUTSIDE cached [low, high]. CB_ALPACA_RATE_MAX_CALLS 50→150.

Phase 2 — Phase 2 DOWN lane + audit quick wins (PR #114, v117):
  UP fires independently of stronger DOWN; DOWN shadow-only unless V3_DOWN_SIGNALS_ENABLED=1.
  Direction-separated peer pools; _strip_model_direction_suffix for meta_{sym}_up/_down keys.
  Model cache, predictions indexes, login throttle, NON_RESEARCH_WHERE hygiene.

Phase 3 — GHOST_ACCURACY_CONTRACT=70 (PR #119–#123):
  core/accuracy_contract.py — unified training/firing/objective/kill floors; env cannot
  weaken contract (V3_MIN_HOLDOUT_ACC=0.38 etc clamped). Research picks no longer bypass
  precision gate under 70 contract. Railway: GHOST_ACCURACY_CONTRACT=70, OBJECTIVE_MODE=
  balanced, RESEARCH_PICK_ENABLED=0, KILL_WINRATE_FLOOR=0.70. Training admission 60%
  OOS; live fire still requires 70% precision proof. /api/_version exposes accuracy_contract.
  Tests: 649 passed at ship. Retrain under strict gates: 0/5 batch passed initially.

Operator Q&A: stale status report (v116/968b2c4) corrected — prod reached v123+;
Phase 2 code deployed; 0 DOWN models trained; live 6.7% is pre-fix record; engine
correctly silent (precision_unproven / meta_gate / chop) until models re-prove.

Open for next agent: monitor retrain + precision_gate.ok counts; post-fix resolved WR
only; keep V3_DOWN_SIGNALS_ENABLED=0 until DOWN shadow track record exists.

--- 2026-07-03–04 | PR #125–#131 — security round, live verification pipeline, kill-status honesty ---
Context: full forensic diagnostic (this agent + a second agent cross-verifying).
The second agent shipped the critical-fix wave (regime modifier sync, ~3,600 lines
dead code removed, ghost_state DDL centralized, GHOST_DEV_MODE dev gate) as ace0998.
This session then verified survivors and shipped:

PR #125 (marker 127) — forensic security round:
  - build_ask_context(include_portfolio=False) restored: the portfolio-PII gate is a
    real parameter again (was an undefined name whose NameError got swallowed; the two
    auth-gated callers were TypeError-broken). Public /api/wolf/ask passes False explicitly.
  - POST /api/wolf/ask auth-gated (admin cookie/MCP/OAuth) — was the only
    unauthenticated paid-LLM endpoint. Returns renderable JSON 401.
  - _admin_token_valid fails CLOSED without CRON_SECRET unless GHOST_DEV_MODE=1
    (mirrors _cron_ok; previously any non-Railway deploy was wide open).
  - /api/v2/recent?symbol=ALL auth-gated (WOLF-only default stays public).
  - record_pick_resolution failures now LOGGER.error (were silent P&L divergence).
  - Watermark 223438 centralized: core.prediction_filters.V32_ERA_MIN_ID (was in 8 files).
  - GHOST_CORS_ORIGINS env knob. Tests: tests/test_forensic_security.py.

PR #126–#130 — live verification pipeline modernized (was broken since inception):
  - #126: missing TEST_DATABASE_URL/CRON_SECRET secrets now warn+skip instead of
    hard-failing every main push (release-gates had NEVER run before this).
  - #127: go/no-go aligned to hardened contracts — slim public /health {status,score,ts}
    + internal-key leak tripwire; /api/diagnostics asserted 404 unauth; cockpit markers.
  - #128: "Wait for deploy to settle" gate (3x consecutive /health 200s) — every merge
    redeploys the exact commit the smoke suite tests; it was racing the container swap.
  - #129: coverage check understands directional model maps {sym:{UP,DOWN}} (wf_acc_min per lane).
  - #130: mobile truth-toggle round-trip forced (live layout churn made hit-testing flaky).
  RESULT: first fully clean end-to-end GO in project history (run 28679241539, 44ebd3b):
  686 unit tests + 33/33 live Playwright (desktop+mobile) + go/no-go GO + 0 critical
  health findings + 0 new error signatures. Pipeline runs automatically on every merge.

PR #131 (marker 128) — kill-status honesty:
  - /api/wolf/kill-status showed all-time window (win_rate red, auto_pause) while
    enforce_kill_conditions correctly evaluates only since the last manual resume
    (engine_pause_resume_ts window reset) → paused=false looked like a broken kill
    switch to a live reviewer. VERIFIED working-as-designed; endpoint now returns
    enforcement_window {since_ts, conditions, any_triggered} alongside. Second
    consecutive clean pipeline GO (90ce3b4).

Prod state (user-verified 2026-07-04): _pr_version 128, health 95/100 (holiday
freshness warnings only), degraded=false. Bootstrap phase: 2/8 wins, gate CLOSED
(up_prob 0.4977 vs 0.6309 needed, Chop regime), recent picks WITHDRAWN by open-pick
review (several +P&L anyway), kill dashboard red all-time / enforcement window
insufficient (correct). Engine triple-locked: closed gate, 0.85 bootstrap floor,
per-cycle kill enforcement. Models re-proving precision thresholds on purged slices
post-retrain (label_schema_stale invalidation is intentional).

Ghost MCP connector: OAuth completed 2026-07-04; ghost_gate_status verified live
via MCP from a fresh session. URL: https://ghost-protocol-v2-production.up.railway.app/mcp

NEXT (Monday 2026-07-06 open) — LIVE WATCH, the one untested surface:
  1. Poll ghost_gate_status / ghost_picks / ghost_kill_status through premarket+open.
  2. Any fire must satisfy contract-70: confidence >= floor, precision gate proven
     (not precision_unproven), regime ok, kill conditions clear.
  3. Any intraday resolution must land exit_price EXACTLY at target/stop (capped),
     appear in the performance log, and any perf-log failure now logs
     "record_pick_resolution failed" (grep Railway logs).
  4. Watch /api/system/breakers (yfinance/alpaca) under live load; 95->100 health
     should self-heal with fresh data.
  Optional user config: TEST_DATABASE_URL + CRON_SECRET GitHub Actions secrets
  activate DB integration tests + deep health audit in the pipeline.

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

PR #100 Point-in-Time Feature Store shipped after the evolution directive:
  - Every logged Super Ghost prediction now gets an immutable feature snapshot.
  - Timestamp audit flags any source data dated after the prediction time.
  - This prevents future leakage in learning/lab/model-training work.

PR #99 Promotion Gate shipped after the evolution directive:
  - Ghost can now decide PROMOTE / KEEP CHAMPION / KEEP SHADOWING / RETIRE / INSUFFICIENT using strict evidence gates.
  - No auto-promotion; this creates auditable review records only.

PR #98 Shadow Model Runner shipped after the evolution directive:
  - Seven specialist prediction brains now run in parallel with production Ghost when predictions are logged.
  - Shadow predictions are stored and later resolved against parent Truth Ledger outcomes.
  - This creates true model disagreement memory for future promotion gates.

PR #96 Feature Attribution Memory shipped after the evolution directive:
  - Every logged Super Ghost checklist point is now feature-attribution memory.
  - Resolved outcomes classify features as helped/hurt/underweighted/noise/missing.
  - Feature reliability profiles create long-term memory of which evidence mattered.

PR #95 Champion/Challenger Lab shipped after the evolution directive:
  - Production Ghost now competes against shadow challenger policies on resolved ledger rows.
  - Results are persisted in super_ghost_lab_runs/results.
  - Conservative gates prevent any recommendation without enough rows/actionable calls/improvement.
  - No auto-promotion and no trading; this creates evidence only.

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
