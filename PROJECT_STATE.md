# Ghost Protocol v2 — PROJECT STATE
**Last updated:** 2026-03-23
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
│   ├── prices.py            CoinGecko + Coinbase for crypto, Polygon+yfinance for stocks
│   ├── scheduler.py         Single background task runner
│   ├── prediction.py        WIN-RATE SIGNAL ENGINE (no XGBoost)
│   ├── telegram.py          Morning card, position alerts, weekly summary
│   └── news.py              Finnhub + CryptoPanic every 30 min
├── scripts/
│   ├── retrain.py           XGBoost trainer (unused - see Known Issues)
│   └── __init__.py
├── Procfile / requirements.txt / nixpacks.toml
└── PROJECT_STATE.md         THIS FILE
```

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

**Crypto (19):** BTC, ETH, SOL, XRP, CHZ, LINK, ADA, AVAX, DOT, MATIC, TRX, LTC, ATOM, UNI, BCH, NEAR, SUI, ARB, AAVE
**Stocks (11):** AAPL, NVDA, TSLA, MSFT, META, AMZN, PLTR, AMD, T, XPO, NET
**NEVER ADD BACK:** HOOD (11.6% wr), COIN (14% wr) — historically wrong

**Symbols with real edge in gpo data:**
COMP 88%, BCH 80%, CRV 78%, NEAR 75%, T 68%, SUI 67%, LINK 66%, ATOM 64%,
BAND 63%, ENJ 62%, AAVE 61%, XPO 61%, ARB 61%, LTC 59%, XRP 55%, NET 55%

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
`GET /api/picks` — active + recent picks
`GET /api/history` — resolved picks
`GET /api/stats` — win/loss counts
`GET /api/symbol-accuracy` — per-symbol win rates from gpo
`GET /api/schema` — DB columns
`GET /api/db-probe` — row counts
`GET /api/news` — recent articles
`GET /api/price/{symbol}?asset_type=crypto|stock` — live price
`POST /api/test-alert` — send test Telegram message

**Protected (needs x-cron-secret header):**
`POST /api/run-predictions` — cycle only, NO Telegram
`POST /api/morning-card` — cycle + Telegram card
`POST /api/reconcile` — manual reconcile
`POST /api/retrain` — train XGBoost (unused, see Known Issues)
`POST /api/migrate-outcomes` — import gpo into predictions
`POST /api/clean-garbage` — delete $0.50 garbage picks

---

## KNOWN ISSUES — FIX BEFORE ADDING FEATURES

**P1 — Garbage predictions corrupting stats**
~120 picks with entry_price=0.50 making win rate show 15% instead of real number.
Fix: `POST /api/clean-garbage` (run once).

**P2 — XGBoost retrain endpoint works but has no effect**
`prediction.py` no longer loads any model. `/api/retrain` trains but nothing uses the file.
Do not re-enable until 500+ clean v2 picks exist and confidence correlates with win rate.

**P3 — Stock prices only available market hours**
Polygon returns null on weekends/evenings. Stocks may never appear in the morning card.
Fix: add Alpha Vantage delayed quotes.

**P4 — Dashboard is placeholder HTML**
`/cockpit` shows basic links. Full dashboard is Week 4 work.

**P5 — Weekly summary never fires**
`send_weekly_summary()` exists in telegram.py but no scheduler task calls it.
Add: Friday 4 PM CT scheduler task.

**P6 — Watchdog not built**
No real-time alert when a pick hits target/stop. Reconciler catches it within 15 min.
Build: `core/watchdog.py`

---

## WHAT FAILED — DO NOT REPEAT

- **XGBoost on gpo data** — 86% val accuracy was overfitting on skewed bear data. Predicted DOWN 100%. Removed.
- **Migrating v1 outcomes INTO predictions table** — NOT NULL constraints block every INSERT. Use gpo directly.
- **Reuters RSS** — Railway network blocks feeds.reuters.com. Use Finnhub API.
- **features[0] as price** — Never put confidence as features[0]. Price and features are always separate.
- **morning_card at interval_s=3600** — Sent 24 cards/day. Always 86400.
- **HOOD/COIN in stocks** — 11-14% historical win rate. Never add back.

---

## COMPLETED

- [x] Ghost v2 live on Railway, health 100/100
- [x] DB connected, schema migration running
- [x] Crypto price feeds (CoinGecko + Coinbase)
- [x] Per-symbol win rate signal from 13,945 outcomes
- [x] Regime gate (BTC crash blocks crypto BUYs)
- [x] Telegram morning card with real prices and real confidence
- [x] News monitoring (Finnhub + CryptoPanic)
- [x] Outcome reconciler (15 min cycle)
- [x] cron-job.org wired to 8 AM CT daily
- [x] Ghost v1 Telegram silenced
- [x] Bad symbols removed (HOOD, COIN)
- [x] Edge symbols added (T 68%, XPO 61%, NET 55%)
- [x] PROJECT_STATE.md created

## NOT YET BUILT

- [ ] Run /api/clean-garbage to fix stats
- [ ] Watchdog (real-time hit alerts)
- [ ] Weekly Friday summary
- [ ] Dashboard (cockpit, 8 tabs)
- [ ] Stock prices outside market hours
- [ ] Raise MIN_SAMPLES to 20 (after 200 resolved picks)

---

## THE GOAL

Paper trade for 4 consecutive weeks with 55%+ win rate.
Only then consider real money.
Do not add features. Fix the foundation first.