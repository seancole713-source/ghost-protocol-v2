# Ghost Protocol v2 — PROJECT STATE
**Last updated:** 2026-05-21
**Read this first.** Any agent picking up this project must read this file before touching any code.

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
| Cron trigger | cron-job.org fires `POST /api/morning-card` daily 8 AM CT |
| Cron schedule | America/Chicago timezone, `0 8 * * *` |
| Auth header name | `x-cron-secret` (value stored in Railway env as CRON_SECRET) |

---

## ARCHITECTURE — 15 FILES

```
ghost-protocol-v2/
├── wolf_app.py              FastAPI app, all endpoints, lifespan startup
├── core/
│   ├── db.py                DB pool, schema migration (handles v1 NOT NULL columns)
│   ├── prices.py            yfinance/Polygon for stocks; WOLF is primary symbol
│   ├── scheduler.py         Single background task runner
│   ├── prediction.py        WIN-RATE SIGNAL ENGINE — WOLF-only, STOCK_SYMBOLS default="WOLF"
│   ├── telegram.py          Morning card, position alerts, weekly summary
│   └── news.py              Finnhub news every 30 min
├── templates/
│   └── cockpit_v5.html      WOLF-first cockpit UI (served at GET /cockpit)
├── static/
│   ├── cockpit_v5.js        All cockpit JS — wired to real v2 endpoints
│   └── cockpit_v5.css       Cockpit styles incl. WOLF intel panel
├── scripts/
│   ├── retrain.py           XGBoost trainer (unused - see Known Issues)
│   └── __init__.py
├── Procfile / requirements.txt / nixpacks.toml
└── PROJECT_STATE.md         THIS FILE
```

---

## WOLF PIVOT — 2026-05-21

Ghost Protocol v2 is now **WOLF-only**. All crypto defaults removed.

- `core/prediction.py`: `STOCK_SYMBOLS` env default = `"WOLF"`, `CRYPTO_SYMBOLS` default = `""`
- `wolf_app.py`: all `asset_type` fallbacks changed from `"crypto"` → `"stock"`
- Cockpit UI: crypto tab removed, WOLF tab is the default landing page
- Ticker bar: BTC/ETH replaced with WOLF/DRIV
- Target symbol: **WOLF** (Wolfspeed Inc, NYSE)

---

## HOW PICKS ARE GENERATED

**No XGBoost.** It was removed. See Known Issues for why.

**Signal: Per-symbol win rate from `ghost_prediction_outcomes` table (13,945 rows)**

Logic in `core/prediction.py → _get_symbol_signal()`:
1. Query `ghost_prediction_outcomes` for symbol (up to 200 rows)
2. Calculate UP win rate and DOWN win rate separately
3. If either > 55%: predict that direction at that confidence
4. If overall win rate < 45%: inverse signal
5. If 45-55% zone: skip (no edge)

MIN_SAMPLES = 5 (raise to 20 once 200+ v2 picks resolved)
CONFIDENCE_FLOOR = 0.52

**Check `/api/symbol-accuracy` to see real per-symbol win rates.**

---

## ACTIVE SYMBOLS

**Stocks:** `WOLF` (primary), optionally `DRIV` — set via `STOCK_SYMBOLS` Railway env var
**Crypto:** DISABLED — `CRYPTO_SYMBOLS` env defaults to empty string
**NEVER ADD BACK:** HOOD (11.6% wr), COIN (14% wr), any crypto defaults

---

## DATABASE — KEY TABLES

| Table | Rows | Notes |
|---|---|---|
| `predictions` | 223,449 | v1 + v2 picks. Has NOT NULL constraints on run_at/method/horizon_h |
| `ghost_prediction_outcomes` | 13,945 | PRIMARY SIGNAL SOURCE — query this, not predictions |
| `paper_trades` | 961 | Old v1 tracking |

**CRITICAL:** When INSERTing v2 predictions, always set `run_at = predicted_at`.
The v1 schema has NOT NULL on run_at. `core/db.py` drops this on startup but it may return.

**GARBAGE:** ~120 predictions have `entry_price = 0.50` from a bug.
Run `POST /api/clean-garbage` (auth required) to delete them and restore real accuracy stats.

---

## BACKGROUND TASKS

| Task | Interval | What it does |
|---|---|---|
| `morning_card` | 86400s | Predictions + Telegram card. DO NOT change back to 3600 |
| `reconcile` | 900s | Marks WIN/LOSS/EXPIRED on open picks |
| `news` | 1800s | Fetches Finnhub + CryptoPanic, alerts on bearish news |

---

## API ENDPOINTS

**Public (no auth):**
`GET /health` — health score + task status
`GET /api/picks` — active + recent predictions (array directly, NOT wrapped in .picks)
`GET /api/v2/recent` — resolved trades: `{ok, trades[], wins, losses, win_rate_pct}`
`GET /api/history` — resolved picks
`GET /api/stats` — win/loss counts
`GET /api/stats/v32` — v3.2 era WOLF stats: `{ok, era, wins, losses, win_rate_pct, open_picks, verdict}`
`GET /api/objective` — win-rate objective progress: `{ok, target_pct, current_pct, on_track, trades_evaluated}`
`GET /api/cockpit/context` — master context: `{ok, health, stats, direction, regime, v3, activity}`
`GET /api/symbol-accuracy` — per-symbol win rates from gpo
`GET /api/news` — recent articles (array or `{items:[]}` shape)
`GET /api/price/{symbol}?asset_type=stock` — live price: `{symbol, price, ok}`
`GET /api/wolf/price` — WOLF-specific price: `{ok, symbol, price}`
`GET /api/wolf/context` — WOLF deep intel: `{earnings, short_data, edgar_alert, competitor_signals, reasons[]}`
`POST /api/test-alert` — send test Telegram message

**Protected (needs x-cron-secret header):**
`POST /api/run-predictions` — cycle only, NO Telegram
`POST /api/morning-card` — cycle + Telegram card
`POST /api/reconcile` — manual reconcile
`POST /api/retrain` — train XGBoost (unused, see Known Issues)
`POST /api/migrate-outcomes` — import gpo into predictions
`POST /api/clean-garbage` — delete $0.50 garbage picks

---

## COCKPIT UI — WIRING REFERENCE

File: `templates/cockpit_v5.html` + `static/cockpit_v5.js` + `static/cockpit_v5.css`

**Default active tab:** WOLF Intel
**Ticker bar:** SPY, DIA, QQQ, WOLF, DRIV, VIX — each fetched via `/api/price/{sym}?asset_type=stock`

**v2 API field names (JS must use these, not old v4 names):**
- Outcome: `p.outcome` (`null`=active, `"WIN"`, `"LOSS"`, `"EXPIRED"`) — NOT `p.status`
- Stop price: `p.stop_price` — NOT `p.stop_loss`
- Gain pct: calculate `(target-entry)/entry*100` — NOT `p.gain_pct`
- Expiry: `p.expires_at` (unix timestamp) — NOT `p.done_by`

**loadAll() calls (in order):**
```
/api/picks            → window._picks  (array)
/api/v2/recent        → window._history, _accuracy fallback
/api/news             → window._news
/api/cockpit/context  → _heartbeat, _audit, _accuracy (primary)
/api/stats/v32        → window._statsV32
/api/objective        → window._objective
```

---

## KNOWN ISSUES — FIX BEFORE ADDING FEATURES

**P1 — Garbage predictions corrupting stats**
~120 picks with entry_price=0.50 making win rate show 15% instead of real number.
Fix: `POST /api/clean-garbage` (run once).

**P2 — XGBoost retrain endpoint works but has no effect**
`prediction.py` no longer loads any model. `/api/retrain` trains but nothing uses the file.
Do not re-enable until 500+ clean v2 picks exist and confidence correlates with win rate.

**P3 — Stock prices only available market hours**
Polygon/yfinance returns null on weekends/evenings.
Fix: add Alpha Vantage delayed quotes or accept daytime-only.

**P4 — Weekly summary never fires**
`send_weekly_summary()` exists in telegram.py but no scheduler task calls it.
Add: Friday 4 PM CT scheduler task.

**P5 — Watchdog not built**
No real-time alert when a pick hits target/stop. Reconciler catches it within 15 min.
Build: `core/watchdog.py`

**P6 — /api/stats/v32, /api/objective, /api/wolf/price endpoints may not exist yet**
The cockpit JS calls these. If they return 404 the UI degrades gracefully (shows loading…).
Verify they exist in wolf_app.py before assuming UI is fully wired.

---

## WHAT FAILED — DO NOT REPEAT

- **XGBoost on gpo data** — 86% val accuracy was overfitting on skewed bear data. Predicted DOWN 100%. Removed.
- **Migrating v1 outcomes INTO predictions table** — NOT NULL constraints block every INSERT. Use gpo directly.
- **Reuters RSS** — Railway network blocks feeds.reuters.com. Use Finnhub API.
- **features[0] as price** — Never put confidence as features[0]. Price and features are always separate.
- **morning_card at interval_s=3600** — Sent 24 cards/day. Always 86400.
- **HOOD/COIN in stocks** — 11-14% historical win rate. Never add back.
- **Crypto defaults in prediction.py** — `CRYPTO_SYMBOLS` was hardcoded `"ETH,SOL,UNI,BCH"`. Now default=`""`. Never hardcode crypto defaults again.
- **v4/v3 endpoint names in cockpit JS** — The old JS called `/api/v4/picks`, `/api/v3/watchlist/enriched` etc. — none exist in v2. All calls now use real v2 endpoints.

---

## COMPLETED

- [x] Ghost v2 live on Railway, health 100/100
- [x] DB connected, schema migration running
- [x] Crypto price feeds (CoinGecko + Coinbase)
- [x] Per-symbol win rate signal from 13,945 outcomes
- [x] Regime gate (BTC crash blocks crypto BUYs)
- [x] Telegram morning card with real prices and real confidence
- [x] News monitoring (Finnhub)
- [x] Outcome reconciler (15 min cycle)
- [x] cron-job.org wired to 8 AM CT daily
- [x] Ghost v1 Telegram silenced
- [x] Bad symbols removed (HOOD, COIN)
- [x] Edge symbols added (T 68%, XPO 61%, NET 55%)
- [x] PROJECT_STATE.md created
- [x] **WOLF PIVOT** — crypto defaults stripped, STOCK_SYMBOLS default="WOLF" (commit `3ceebd3`)
- [x] **WOLF-first cockpit UI** — real v2 endpoints, WOLF intel hero tab default, no crypto (commit `9073d1b`)
  - Ticker: WOLF + DRIV replace BTC/ETH
  - loadAll() wired to 6 real endpoints
  - renderPicks() uses v2 field names (outcome, stop_price, expires_at)
  - WOLF tab: current signal, v3.2 stats row, objective progress bar
  - Crypto tab, nav button, and all filter buttons removed

## NOT YET BUILT

- [ ] Verify `/api/stats/v32`, `/api/objective`, `/api/wolf/price`, `/api/wolf/context` exist in wolf_app.py
- [ ] Run /api/clean-garbage to fix corrupted win rate stats
- [ ] Watchdog (real-time hit alerts)
- [ ] Weekly Friday summary scheduler task
- [ ] Stock prices outside market hours (Alpha Vantage)
- [ ] Raise MIN_SAMPLES to 20 (after 200+ resolved WOLF picks)

---

## THE GOAL

Paper trade WOLF for 4 consecutive weeks with 55%+ win rate.
Only then consider real money.
Do not add features. Fix the foundation first.

---

## CHANGE LOG

| Date | Commit | What changed |
|---|---|---|
| 2026-05-21 | `d0be424` | WOLF pivot phases 1–6: agents, intel, metrics, risk, patterns |
| 2026-05-21 | `3ceebd3` | Crypto defaults stripped from prediction.py + wolf_app.py asset_type fallbacks |
| 2026-05-21 | `9073d1b` | WOLF-first cockpit UI: real v2 endpoints, no crypto, WOLF intel hero tab |