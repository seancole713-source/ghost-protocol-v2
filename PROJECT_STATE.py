"""
PROJECT_STATE.py — Ghost Protocol v2
Single source of truth. Accountability ledger. Read this before touching anything.

RULES:
  1. Read this file before starting work.
  2. Verify claimed fixes on the LIVE site before trusting them.
  3. After every fix session, add a dated section with commit hashes + live verification.
  4. Never mark [x] done unless YOU personally verified it on the live dashboard.
  5. If you find new bugs, add them to the TODO list.
  6. This is not documentation. It is an accountability ledger.
     Agents lie. This file exists because of that.
"""

# ============================================================
# LIVE SYSTEM — VERIFIED 2026-03-23
# ============================================================

PRODUCTION_URL = "https://ghost-protocol-v2-production.up.railway.app"
GITHUB_REPO = "seancole713-source/ghost-protocol-v2"
RAILWAY_PROJECT = "tender-benevolence"
RAILWAY_SERVICE_V2 = "98593080-065d-43ef-840c-4a3d36a1b572"
RAILWAY_SERVICE_V1 = "098281d7-7dba-447c-981e-0ebd625cecad"  # old ghost, Telegram silenced
CRON_JOB = "cron-job.org fires POST /api/morning-card at 8 AM CT (America/Chicago, 0 8 * * *)"
AUTH_HEADER = "x-cron-secret — value in Railway env as CRON_SECRET"

# ============================================================
# VERIFIED LIVE STATE — 2026-03-23 ~11:30 AM CT
# Verified by direct API calls, not by agent claims.
# ============================================================

LIVE_VERIFIED = {
    "health_score": 100,
    "health_status": "healthy",
    "issues": [],
    "price_feeds": "CoinGecko OK, Coinbase OK, Binance FAIL, Polygon FAIL (2/4)",
    "morning_card_task": "interval=86400s, runs=1, errors=0",
    "reconcile_task": "runs=1, errors=0",
    "news_task": "runs=1, errors=0",
    "stats_wins": 22,
    "stats_losses": 120,
    "win_rate_pct": 15.5,
    "win_rate_NOTE": "CORRUPTED — 120 losses are from $0.50 price bug, not real trades",
    "open_positions": 132,
    "active_picks_shown": 50,
    "symbols_with_edge": 18,
    "top_edge_symbols": "COMP 88% (43), BCH 80% (166), LINK 66% (405), XRP 55% (508)",
}

# ============================================================
# TODO LIST — sorted by priority
# Mark [x] ONLY after verifying on live site yourself.
# ============================================================

TODO = """
P1 — BLOCKING (fix before anything else)
[ ] Run /api/clean-garbage to delete 120 garbage $0.50 predictions.
    Until this runs, win rate shows 15.5% instead of real number.
    Command: POST /api/clean-garbage with x-cron-secret header.
    Verify: /api/stats should show wins=22, losses=0 or small number after.

[ ] cron-job.org timezone — currently set to UTC 0 14 * * * which fires 9 AM CT during DST.
    Should be America/Chicago timezone with 0 8 * * * for consistent 8 AM CT year-round.
    Verify: check cron-job.org job settings, confirm timezone = America/Chicago.

P2 — SIGNAL QUALITY
[ ] MIN_SAMPLES is 5 — too low, allows noisy signals.
    Raise to 20 after 200+ v2 picks have resolved.
    File: core/prediction.py line: MIN_SAMPLES = 5

[ ] Stock prices fail outside market hours.
    AAPL/NVDA/TSLA all return null on evenings and weekends.
    Current fallback: yfinance, also unreliable.
    Fix needed: Alpha Vantage delayed quotes or accept stocks are crypto-hours-only.

[ ] Model retraining is wired but unused.
    /api/retrain trains XGBoost but prediction.py never loads it.
    XGBoost was removed because it predicted DOWN 100% on skewed data.
    Do NOT re-enable until 500+ clean v2 picks exist.

P3 — MISSING FEATURES
[ ] Watchdog — real-time alert when a pick hits target or stop.
    Currently: reconciler catches it within 15 min, no immediate Telegram alert.
    Build: core/watchdog.py, register in scheduler, alert via telegram.send_position_alert()

[ ] Weekly summary — send_weekly_summary() exists in core/telegram.py but is never called.
    Add: scheduler task on Friday 4 PM CT (22:00 UTC).

[ ] Dashboard (cockpit) is placeholder HTML.
    /cockpit returns basic links only. Full 8-tab dashboard not built.
    This is Week 4 work.

[ ] Binance and Polygon price feeds failing.
    Polygon fails outside market hours (expected).
    Binance fails on Railway network (possible block).
    Currently 2/4 feeds responding — acceptable but investigate Binance.
"""

# ============================================================
# COMPLETED — verified on live site
# ============================================================

COMPLETED = """
[x] Ghost v2 deployed on Railway — VERIFIED live at ghost-protocol-v2-production.up.railway.app
[x] PostgreSQL connected — VERIFIED health.db=true
[x] Crypto price feeds (CoinGecko + Coinbase) — VERIFIED BTC $68K, ETH $2K returned correctly
[x] All 3 background tasks running with 0 errors — VERIFIED health endpoint 2026-03-23
[x] Telegram morning card working — VERIFIED card arrived on phone with real prices
[x] morning_card interval fixed to 86400s — VERIFIED health.tasks[morning_card].interval_s=86400
[x] Ghost v1 Telegram silenced — DEPLOYED blank TELEGRAM_BOT_TOKEN to v1 service on Railway
[x] Real prices in cards (not $0.50) — VERIFIED BTC $68K in Telegram card received 2026-03-23
[x] Mixed UP/DOWN directions in picks — VERIFIED e.g. XRP UP 88%, LINK DOWN 97%
[x] Signal from ghost_prediction_outcomes (13,945 rows) — VERIFIED /api/symbol-accuracy returns data
[x] 18 symbols with measured edge — VERIFIED /api/symbol-accuracy shows COMP 88%, BCH 80%, etc
[x] Regime gate coded — IN CODE but not verified to have blocked a real trade yet
[x] HOOD/COIN removed (11-14% win rate) — COMMITTED 02ab7b7
[x] T/XPO/NET added (55-68% win rate) — COMMITTED 02ab7b7
[x] cron-job.org URL updated to v2 — CONFIRMED by user 2026-03-23
[x] PROJECT_STATE.py created — THIS FILE
"""

# ============================================================
# WHAT FAILED — DO NOT REPEAT THESE MISTAKES
# ============================================================

FAILURES = """
1. XGBoost on ghost_prediction_outcomes
   What happened: Trained at 86% val accuracy, predicted DOWN 100% on everything.
   Why: bear-market skewed data. High accuracy = overfitting majority class, not real signal.
   Resolution: Removed XGBoost. Using per-symbol win rate instead.
   Do not repeat: Do not re-enable until data is balanced and confidence correlates with win rate.

2. Migrating v1 outcomes INTO predictions table via INSERT
   What happened: 5+ attempts, each failed with NOT NULL on run_at, method, horizon_h.
   Why: v1 predictions table has rigid schema we cannot safely write to.
   Resolution: Query ghost_prediction_outcomes directly. Never try to write to predictions from gpo.

3. Reuters RSS feeds
   What happened: DNS resolution failed on Railway for feeds.reuters.com.
   Why: Railway egress proxy blocks external RSS hosts.
   Resolution: Switched to Finnhub API (allowed). Never add Reuters RSS back.

4. confidence_val=0.5 as features[0], extracted as price
   What happened: All picks showed $0.50 entry price.
   Why: _build_features() put confidence placeholder at index 0.
         predict_symbol() did price = features[0]. Got 0.5 instead of real price.
   Resolution: _build_features() returns (price, feature_list) tuple. Price always separate.
   Do not repeat: Never put price in the feature vector.

5. morning_card scheduler interval = 3600
   What happened: 9+ Telegram cards sent in one day.
   Why: interval_s=3600 means every 60 minutes, not once per day.
   Resolution: Changed to 86400. Verified on live health endpoint.
   Do not repeat: morning_card is always 86400s. cron-job.org handles the 8 AM timing.

6. HOOD and COIN in stock symbols
   What happened: Poisoned accuracy stats.
   Why: 11.6% and 14% historical win rate — Ghost was almost always wrong on these.
   Resolution: Removed from STOCK_SYMBOLS in prediction.py (commit 02ab7b7).
   Do not repeat: Never add HOOD or COIN back.

7. Agents claiming fixes are done without verifying
   What happened: Multiple sessions where claimed [x] items were still broken on live site.
   Resolution: This file exists. Update it. Verify before marking done.
"""

# ============================================================
# SESSION LOG — append a new entry after every fix session
# ============================================================

SESSION_LOG = """
--- 2026-03-23 | Agent: Claude Sonnet ---
Context: Ghost v1 was 41% accuracy, 166 files, broken. Full rebuild as v2.

Commits this session (in order):
  c2f9609 — initial repo + README
  db.py   — DB pool, schema migration
  prices.py — CoinGecko + Coinbase + Polygon + yfinance
  scheduler.py — single background task runner
  prediction.py — multiple rewrites (see failures above)
  telegram.py — multiple rewrites (unterminated string bug caused 3 failed deploys)
  news.py — switched from Reuters to Finnhub after Railway network block
  nixpacks.toml — libpq-dev fix for psycopg2 on Python 3.13
  wolf_app.py — 10+ revisions, current state has all endpoints
  scripts/retrain.py — trained XGBoost, not used (see failures)
  940bce5 — morning_card interval 3600 -> 86400 (stop hourly spam)
  02ab7b7 — remove HOOD/COIN, add T/XPO/NET to symbols
  7669e83 — PROJECT_STATE.md created (markdown version)
  10f4797 — fix hit_direction 0/1 vs WIN/LOSS comparison (was causing 100% confidence)
  eac7797 — signal queries ghost_prediction_outcomes not empty v2 predictions table
  ddce09b — add /api/clean-garbage endpoint
  THIS COMMIT — PROJECT_STATE.py created

Verified live at end of session (2026-03-23 ~11:30 AM CT):
  health_score = 100
  morning_card = 86400s interval, 1 run, 0 errors
  reconcile = 0 errors
  news = 0 errors
  picks = 10 generated, real prices, mixed directions
  Telegram card received on phone with real prices (BTC $68K, ETH $2K)
  18 symbols with edge from gpo data

NOT verified (still needs doing):
  /api/clean-garbage has not been run — stats still show 15.5% win rate
  Stocks untested during market hours — all null right now
  cron-job.org timezone needs changing to America/Chicago (currently UTC)
"""