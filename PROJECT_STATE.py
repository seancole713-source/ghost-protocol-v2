"""
PROJECT_STATE.py â Ghost Protocol v2
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
# LIVE SYSTEM â VERIFIED 2026-03-23
# ============================================================

PRODUCTION_URL = "https://ghost-protocol-v2-production.up.railway.app"
GITHUB_REPO = "seancole713-source/ghost-protocol-v2"
RAILWAY_PROJECT = "tender-benevolence"
RAILWAY_SERVICE_V2 = "98593080-065d-43ef-840c-4a3d36a1b572"
RAILWAY_SERVICE_V1 = "098281d7-7dba-447c-981e-0ebd625cecad"  # old ghost, Telegram silenced
CRON_JOB = "cron-job.org fires POST /api/morning-card at 8 AM CT (America/Chicago, 0 8 * * *)"
AUTH_HEADER = "x-cron-secret â value in Railway env as CRON_SECRET"

# ============================================================
# VERIFIED LIVE STATE â 2026-03-23 ~11:30 AM CT
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
    "win_rate_NOTE": "CORRUPTED â 120 losses are from $0.50 price bug, not real trades",
    "open_positions": 132,
    "active_picks_shown": 50,
    "symbols_with_edge": 18,
    "top_edge_symbols": "COMP 88% (43), BCH 80% (166), LINK 66% (405), XRP 55% (508)",
}

# ============================================================
# TODO LIST â sorted by priority
# Mark [x] ONLY after verifying on live site yourself.
# ============================================================

TODO = """
P1 — FIX IMMEDIATELY
[ ] Win rate stats corrupted — 10.3% (11W/96L). The 96 losses are from broken model runs
    (all-DOWN cards with $68K entries targeting $0.50 resolved as LOSS immediately).
    clean-garbage endpoint targets entry_price BETWEEN 0.49 AND 0.51 but those picks
    have real prices now. Need a different filter: DELETE predictions WHERE predicted_at
    IS NOT NULL AND entry_price > 50 AND target_price < 1.0 (target was $0.53 from $0.50).
    Until fixed, win rate is meaningless.

[ ] PROJECT_STATE.py itself is stale — update after each session (do this now).

P2 — SIGNAL QUALITY
[ ] LTC UP 94% is wrong — gpo says 59% DOWN bias. v2 double-weighting overrides.
    Reduce v2 row weight or require direction consistency between v2 and gpo.

[ ] XRP UP 88% is marginal — 54.7% gpo win rate is below EDGE_THRESHOLD (60%).
    It fires via the v2 double-weight path. Consider stricter v2 weight.

[ ] Stock prices unavailable outside market hours (Polygon API).
    Stocks never appear in picks. Fix: accept stocks are daytime-only OR add pre-open data.

P3 — CLEANUP
[ ] cron-job.org timezone should be America/Chicago with 0 8 * * * (currently UTC 0 14).
[ ] Remove /api/debug-signal endpoint before going live with real money.
[ ] Raise MIN_SAMPLES from 10 to 20 after 300+ resolved v2 picks exist.
[ ] Circuit breaker threshold (8 consecutive losses) needs validation over time.
"""

# ============================================================
# COMPLETED â verified on live site
# ============================================================

COMPLETED = """
[x] Ghost v2 deployed on Railway — VERIFIED live, health 100/100
[x] DB connected — VERIFIED health.db=true
[x] Crypto price feeds (CoinGecko + Coinbase) — VERIFIED BCH $478, LINK $9.16
[x] All 3 background tasks running 0 errors — VERIFIED 2026-03-23
[x] Telegram morning card — VERIFIED card arrived with BCH/LINK/XRP/LTC picks
[x] morning_card interval 86400s — VERIFIED health tasks interval=86400
[x] Ghost v1 Telegram silenced — VERIFIED no more v1 cards
[x] Watchdog running — VERIFIED health tasks shows watchdog wd_runs=1 0 errors
[x] Weekly summary scheduled — VERIFIED health tasks shows weekly_summary 0 errors
[x] HOOD/COIN removed — COMMITTED 02ab7b7
[x] T/XPO/NET added — COMMITTED 02ab7b7
[x] cron-job.org URL updated to v2 — CONFIRMED by user
[x] 3-zone signal logic (FIRE>60 / BENCH 40-60 / INVERT<40) — COMMITTED 6821d7e
[x] Circuit breaker (8 v2 losses = bench, unless gpo>60%) — COMMITTED 354b129
    VERIFIED: BCH was benched, fix applied, BCH DOWN 87% now appears in picks
[x] Inverse confidence capped at 0.65 — COMMITTED 4084f1b
[x] MIN_SAMPLES=10, EDGE_THRESHOLD=0.60, FLOOR=0.70 (Railway env) — VERIFIED via debug

NOT YET BUILT (honest list):
[ ] Cockpit dashboard — placeholder HTML only
[ ] Real technical features (RSI, MACD, BB) — using win-rate signal only
[ ] Alpha Vantage for stock prices outside market hours
"""

# ============================================================
# WHAT FAILED â DO NOT REPEAT THESE MISTAKES
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
   Why: 11.6% and 14% historical win rate â Ghost was almost always wrong on these.
   Resolution: Removed from STOCK_SYMBOLS in prediction.py (commit 02ab7b7).
   Do not repeat: Never add HOOD or COIN back.

7. Agents claiming fixes are done without verifying
   What happened: Multiple sessions where claimed [x] items were still broken on live site.
   Resolution: This file exists. Update it. Verify before marking done.
"""

# ============================================================
# SESSION LOG â append a new entry after every fix session
# ============================================================

SESSION_LOG = """
--- 2026-03-23 | Agent: Claude Sonnet ---
Context: Ghost v1 was 41% accuracy, 166 files, broken. Full rebuild as v2.

Commits this session (in order):
  c2f9609 â initial repo + README
  db.py   â DB pool, schema migration
  prices.py â CoinGecko + Coinbase + Polygon + yfinance
  scheduler.py â single background task runner
  prediction.py â multiple rewrites (see failures above)
  telegram.py â multiple rewrites (unterminated string bug caused 3 failed deploys)
  news.py â switched from Reuters to Finnhub after Railway network block
  nixpacks.toml â libpq-dev fix for psycopg2 on Python 3.13
  wolf_app.py â 10+ revisions, current state has all endpoints
  scripts/retrain.py â trained XGBoost, not used (see failures)
  940bce5 â morning_card interval 3600 -> 86400 (stop hourly spam)
  02ab7b7 â remove HOOD/COIN, add T/XPO/NET to symbols
  7669e83 â PROJECT_STATE.md created (markdown version)
  10f4797 â fix hit_direction 0/1 vs WIN/LOSS comparison (was causing 100% confidence)
  eac7797 â signal queries ghost_prediction_outcomes not empty v2 predictions table
  ddce09b â add /api/clean-garbage endpoint
  THIS COMMIT â PROJECT_STATE.py created

Verified live at end of session (2026-03-23 ~11:30 AM CT):
  health_score = 100
  morning_card = 86400s interval, 1 run, 0 errors
  reconcile = 0 errors
  news = 0 errors
  picks = 10 generated, real prices, mixed directions
  Telegram card received on phone with real prices (BTC $68K, ETH $2K)
  18 symbols with edge from gpo data

NOT verified (still needs doing):
  /api/clean-garbage has not been run â stats still show 15.5% win rate
  Stocks untested during market hours â all null right now
  cron-job.org timezone needs changing to America/Chicago (currently UTC)

--- 2026-03-24 | Agent: Claude Sonnet ---
Context: Continuing from 2026-03-23 session. BCH/LINK being silenced by circuit breaker.

Root cause found: BCH had exactly 8 v2 resolved picks, all LOSS from broken model runs.
Circuit breaker threshold was 8 — so it fired on exactly 8 losses. Fix: skip CB if gpo_wr > EDGE_THRESHOLD.

Commits this session:
  4084f1b — circuit breaker 3→8 + inverse confidence capped at 0.65
  85fba02 — debug endpoint rebuilt as step-by-step trace
  354b129 — circuit breaker respects strong gpo signal (BCH 80%+ overrides 8 v2 losses)
  36d97f0 — debug endpoint calls actual _get_symbol_signal function
  50ae220 — debug endpoint with step trace

Verified live at end of session (2026-03-24):
  health_score = 100
  picks_generated = 4: BCH DOWN 87%, LINK DOWN 86%, XRP UP 88%, LTC UP 94%
  BCH DOWN 87% matches gpo 80.1% win rate DOWN bias — CORRECT
  LINK DOWN 86% matches gpo 65.1% win rate DOWN bias — CORRECT
  Telegram morning card sent successfully (ok:true, picks_generated:4)

NOT verified / still broken:
  Win rate stats: 10.3% (corrupted — 96 losses from broken model runs)
  LTC UP 94% questionable — gpo says DOWN bias, v2 double-weighting overrides
  XRP UP 88% marginal — 54.7% is below EDGE_THRESHOLD (60%)
"""