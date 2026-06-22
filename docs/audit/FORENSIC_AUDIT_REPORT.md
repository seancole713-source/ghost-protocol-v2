# GHOST PROTOCOL v2 — MASTER FORENSIC SYSTEM AUDIT
**Audit Date:** 2026-06-19
**Auditor:** AI Forensic Agent (Read-Only Mode)
**Repository:** seancole713-source/ghost-protocol-v2
**Branch:** main (commit f9164c3, PR #64)
**Production:** ghost-protocol-v2-production.up.railway.app (Railway tender-benevolence)
**Production PR:** #63 (June 15, 2026 — PR #64 pending deploy)

---

# EXECUTIVE SUMMARY

## Can Ghost Protocol be trusted with real money decisions?

**Answer: CONDITIONALLY YES — with documented limitations.**

Ghost Protocol v2 is a **serious, well-engineered trading signal system** built by a developer who understands quantitative finance, machine learning, and production engineering. It is NOT a toy. The codebase shows evidence of:

- **Honest self-assessment**: The ~80% accuracy claim was pre-registered with a falsification gate, tested against real data, and **formally abandoned** when the data disproved it. This is rare integrity in trading systems.
- **Defense-in-depth**: Four-gate chain (regime → meta → probability → confidence), kill conditions with auto-pause, circuit breakers, objective-mode gating.
- **Production rigor**: HMAC-signed admin cookies, CSP headers, rate limiting, audit logging, health audit with persistent findings, boot self-healing.
- **Walk-forward validation**: Models are trained with time-series split, not random shuffle — no look-ahead bias.
- **Shared TP/SL resolution**: Training labels and live reconciliation use the same bar-path rules — no train/serve skew.

**However**, there are material limitations:

1. **Single-instance, no Redis** — all state is in-process memory (caches, rate-limit counters, scheduler). A Railway restart wipes everything.
2. **No message queue** — the asyncio scheduler runs everything in one process. If the scheduler loop blocks, all background tasks stall.
3. **WOLF data is thin** — post-Chapter-11, only ~250 trading days exist. The model pools peer tickers to compensate, but the target instrument's own history is limited.
4. **yfinance is unreliable overnight** — the primary fallback price source regularly fails with JSON parse errors. The multi-provider chain mitigates this but adds latency.
5. **No paid market data** — Alpaca free tier (SIP 403s on some symbols), Polygon free tier, yfinance (flakey), Stooq (last resort). Key Stats / Analyst / Short Interest fields are empty due to feed-tier limits.
6. **EDGAR module missing** — `core.edgar_integration` is referenced but doesn't exist (logs show `No module named 'core.edgar_integration'`).
7. **PR #64 not deployed** — RDFN (delisted June 2025) is still being fetched across 4 providers × 3 retries = 12 failing calls per cycle, wasting API quota and log volume.

## Trust Score Summary

| Dimension | Score | Evidence |
|-----------|-------|----------|
| Market Data Integrity | 72/100 | Multi-provider chain, but free-tier gaps and yfinance flakiness |
| Portfolio Accuracy | 85/100 | Realized P&L with limit-fill pricing, equity curve compounding |
| Signal Reliability | 78/100 | Walk-forward XGBoost, but thin WOLF data and peer-pooling dependency |
| Ghost Score Reliability | 70/100 | Composite of multiple probes, but some probes are best-effort |
| AI Reliability | 65/100 | Claude Haiku for sentiment only; no trading decisions from LLM |
| Alert Reliability | 80/100 | Telegram + Discord + email/SMS, dedup, daily caps |
| Security | 82/100 | HMAC cookies, CSP, rate limiting, no Swagger in prod |
| Performance | 75/100 | Single-process, no Redis, in-memory caches |
| Resilience | 68/100 | Multi-provider fallbacks, but no circuit breakers on external APIs |
| Observability | 85/100 | Health audit, diagnostics, performance log, pick journal, admin audit log |
| **OVERALL** | **76/100** | **Trustworthy with caveats** |

---

# 1. REPOSITORY MAP

## File Inventory (101 files)

### Root (15 files)
| File | Classification | Purpose |
|------|---------------|---------|
| `wolf_app.py` (~5200 lines) | **CRITICAL** | Main FastAPI app — all routes, middleware, scheduler, startup |
| `PROJECT_STATE.py` | HIGH | Accountability ledger — live system state, PR history |
| `PROJECT_STATE.md` | HIGH | Human-readable project state mirror |
| `cockpit.html` | HIGH | WOLF Command Center dashboard |
| `admin.html` | HIGH | Cookie-gated operator console |
| `Makefile` | MEDIUM | Build/test orchestration |
| `Procfile` | CRITICAL | Railway deploy — uvicorn launch |
| `requirements.txt` | CRITICAL | Production dependencies |
| `requirements-dev.txt` | MEDIUM | Dev-only dependencies |
| `runtime.txt` | CRITICAL | Python 3.13.13 |
| `nixpacks.toml` | CRITICAL | Railway build config |
| `pytest.ini` | MEDIUM | Pytest configuration |
| `mypy.ini` | MEDIUM | Mypy type-checker config |
| `playwright.config.ts` | MEDIUM | E2E test config |
| `package.json` | MEDIUM | npm quality-gate scripts |

### Core Engine (43 files in `core/`)
| File | Classification | Purpose |
|------|---------------|---------|
| `signal_engine.py` | **CRITICAL** | v3.2 XGBoost — training, inference, OHLCV fetch chain, regime gates |
| `prediction.py` | **CRITICAL** | Multi-symbol scan loop, objective gate, kill conditions, falsification |
| `prices.py` | **CRITICAL** | Alpaca→yfinance price fetch, intraday OHLC aggregation |
| `db.py` | **CRITICAL** | psycopg2 ThreadedConnectionPool, schema migration |
| `tp_sl_resolve.py` | **CRITICAL** | Shared TP/SL bar-path resolution (training + live) |
| `model.py` | HIGH | Legacy XGBoost model (superseded by signal_engine v3.2) |
| `vol_targets.py` | HIGH | Volatility-based target/stop computation |
| `squeeze_monitor.py` | HIGH | 43-symbol intraday RVOL squeeze radar |
| `squeeze_scorecard.py` | HIGH | Setup/trigger/confirmation scoring |
| `squeeze_ml_v2.py` | HIGH | Logistic blend over scorecard features |
| `squeeze_outcomes.py` | HIGH | EOD squeeze resolution |
| `squeeze_live_drift.py` | HIGH | Alert-buy vs live-quote gap tracking |
| `telegram.py` | HIGH | Telegram/Discord sender |
| `telegram_cards.py` | HIGH | Daily/Silence/Weekly card formatters |
| `news.py` | HIGH | Finnhub fetch + Claude Haiku sentiment |
| `news_store.py` | MEDIUM | Multi-symbol news persistence |
| `news_sentiment.py` | MEDIUM | Regex lexicon fallback scorer |
| `wolf_context.py` | HIGH | WOLF context: short interest, earnings, SEC 8-K, sector |
| `wolf_monitor.py` | HIGH | Autonomous WOLF volume/spike monitor |
| `world_feed_fusion.py` | MEDIUM | RSS + NLP from Reuters/Bloomberg/FT/WSJ/CNBC |
| `health_audit.py` | HIGH | Deep reliability scan with structured findings |
| `scheduler.py` | CRITICAL | Single asyncio background task loop |
| `watchdog.py` | HIGH | 5-min position hit alerts |
| `pnl.py` | HIGH | Realized P&L with limit-fill pricing |
| `portfolio_routes.py` | MEDIUM | Personal portfolio CRUD |
| `risk_discipline.py` | HIGH | 1% position sizing, daily loss lock |
| `ghost_contract.py` | HIGH | Post-falsification product contract |
| `ghost_ask.py` | MEDIUM | Claude Haiku Q&A grounded in live state |
| `regime.py` | MEDIUM | Rule-based regime tag (price vs SMA) |
| `regime_calibration.py` | HIGH | Regime-conditional confidence adjustment |
| `regime_classifier.py` | HIGH | Unified rules + signal-engine regime labels |
| `market_hours.py` | CRITICAL | US equity session clock (CT hardwired) |
| `attribution.py` | MEDIUM | Feature-level WIN/LOSS attribution |
| `backtest.py` | MEDIUM | Sharpe, profit factor, drawdown metrics |
| `feature_audit.py` | MEDIUM | Point-biserial correlation, sign correction |
| `feature_drift.py` | MEDIUM | PSI-like z-shift monitoring |
| `feature_schema.py` | MEDIUM | Feature timestamp recording |
| `performance_log.py` | HIGH | Per-cycle, per-symbol, per-pick lifecycle journaling |
| `pick_review.py` | HIGH | Open pick re-evaluation, withdraw/supersede |
| `prediction_filters.py` | HIGH | SQL WHERE fragments for real-trade filtering |
| `shadow_outcomes.py` | HIGH | Virtual pick resolution for silent model evals |
| `stats_direction.py` | MEDIUM | BUY/SELL win-rate breakdown |
| `stock_engine.py` | MEDIUM | Legacy stock-specific model (superseded) |
| `options_flow.py` | MEDIUM | WOLF options put/call probe |
| `notify.py` | MEDIUM | Email (SMTP) + SMS (Twilio) notifications |
| `daily_forecast_scorecard.py` | MEDIUM | Next-session OHLC forecast |

### Configuration (2 files)
| File | Classification | Purpose |
|------|---------------|---------|
| `config/settings.py` | MEDIUM | Pydantic BaseSettings (partially stale — v3 thresholds don't match live env) |
| `config/symbols.py` | **CRITICAL** | 43-symbol OFFICIAL_WATCHLIST, env pinning |

### API & Routes (2 files)
| File | Classification | Purpose |
|------|---------------|---------|
| `api/wolf_endpoints.py` | HIGH | WOLF command center: context, price, predictions, stats, Ghost Score |
| `routes/schema.py` | LOW | DB schema introspection |

### MCP Server (7 files)
| File | Classification | Purpose |
|------|---------------|---------|
| `mcp/ghost_server.py` | MEDIUM | MCP tool definitions (GET-only) |
| `mcp/jsonrpc.py` | MEDIUM | JSON-RPC Streamable HTTP dispatch |
| `mcp/oauth_routes.py` | MEDIUM | OAuth discovery/authorize/token |
| `mcp/oauth_server.py` | MEDIUM | OAuth 2.1 authorization server (CIMD, JWT, PKCE) |
| `mcp/routes.py` | MEDIUM | MCP FastAPI routes |
| `mcp/security.py` | MEDIUM | Bearer token + MCP token verification |

### Scripts (9 files)
| File | Classification | Purpose |
|------|---------------|---------|
| `scripts/retrain.py` | MEDIUM | XGBoost retraining script |
| `scripts/prelaunch_smoke.py` | HIGH | Pre-deploy HTTP smoke checks |
| `scripts/go-no-go.sh` | HIGH | Deploy go/no-go gate |
| `scripts/verify_health_audit.py` | MEDIUM | Health audit verifier |
| `scripts/verify_live_health.py` | MEDIUM | Live health parity check |
| `scripts/wolf_backtest.py` | MEDIUM | WOLF walk-forward backtest |
| `scripts/squeeze_stress_test.py` | MEDIUM | Squeeze monitor stress test |
| `scripts/watch_wolf_calibration.py` | LOW | Calibration log watcher |
| `scripts/check_error_signatures.py` | LOW | Error signature checker |

### Tests (41 files in `tests/`)
Comprehensive test suite covering: signal engine, prediction, prices, TP/SL resolution, squeeze monitor, market hours, risk discipline, health audit, MCP server, portfolio, shadow outcomes, feature audit, and more.

---

# 2. SYSTEM ARCHITECTURE

```
┌─────────────────────────────────────────────────────────────┐
│                     Railway (tender-benevolence)              │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │              uvicorn → wolf_app.py (FastAPI)             │ │
│  │  ┌───────────────────────────────────────────────────┐  │ │
│  │  │              Middleware Stack                       │  │ │
│  │  │  CORS → Rate Limiter → Security Headers → Routes  │  │ │
│  │  └───────────────────────────────────────────────────┘  │ │
│  │  ┌───────────────────────────────────────────────────┐  │ │
│  │  │              Background Scheduler                  │  │ │
│  │  │  morning_card (24h)  │  market_scan (30-60m)      │  │ │
│  │  │  watchdog (5m)       │  weekly_summary (1h tick)  │  │ │
│  │  │  daily_summary (1h)  │  squeeze_eod (1h)          │  │ │
│  │  │  reconcile (15m)     │  portfolio_refresh (15m)   │  │ │
│  │  │  risk_discipline (5m)│  news (30m)                │  │ │
│  │  │  shadow_outcomes(1h) │  coverage_maintenance(1h)  │  │ │
│  │  │  weekly_retrain (7d) │                            │  │ │
│  │  └───────────────────────────────────────────────────┘  │ │
│  │  ┌───────────────────────────────────────────────────┐  │ │
│  │  │              Async Monitors                        │  │ │
│  │  │  wolf_monitor (WOLF volume/spikes/8-K)            │  │ │
│  │  │  squeeze_monitor (43-symbol RVOL radar)           │  │ │
│  │  └───────────────────────────────────────────────────┘  │ │
│  └─────────────────────────────────────────────────────────┘ │
│                            │                                  │
│              ┌─────────────┼─────────────┐                    │
│              ▼             ▼             ▼                    │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐         │
│  │  PostgreSQL  │ │  In-Memory   │ │  External    │         │
│  │  (Railway)   │ │  Caches      │ │  APIs        │         │
│  │              │ │              │ │              │         │
│  │ predictions  │ │ price cache  │ │ Alpaca       │         │
│  │ paper_trades │ │ OHLCV cache  │ │ Polygon      │         │
│  │ price_cache  │ │ cockpit cache│ │ yfinance     │         │
│  │ ghost_state  │ │ sentiment    │ │ Stooq        │         │
│  │ ghost_v3_model│ │ short cache  │ │ Finnhub      │         │
│  │ user_portfolio│ │ model cache  │ │ Anthropic    │         │
│  │ health_audit  │ │ rate limits  │ │ Telegram     │         │
│  │ perf_logs     │ │              │ │ Discord      │         │
│  │ squeeze_log   │ │              │ │ SMTP/Twilio  │         │
│  └──────────────┘ └──────────────┘ └──────────────┘         │
└─────────────────────────────────────────────────────────────┘
```

## Data Flow: Pick Generation

```
1. Scheduler triggers market_scan_job (every 30-60 min)
2. run_prediction_cycle() iterates STOCK_SYMBOLS
3. For each symbol:
   a. predict_live_ex() → load_model() from ghost_v3_model
   b. _fetch_ohlcv() → Alpaca SIP → IEX → Polygon → yfinance → Stooq
   c. Compute EMA/ADX/ATR/OBV/Stochastic indicators
   d. Regime gate: skip BUY if below EMA200 + ADX<20
   e. Model predict_proba → up_prob
   f. Meta gate: edge, holdout accuracy, walk-forward checks
   g. Confidence: clamp(accuracy + (up_prob - min_p) × 4.0, 0.75, 0.95)
   h. Objective gate: symbol WR vs target, bootstrap min conf
   i. If all gates pass → save pick, fire Telegram alert
4. Watchdog (every 5 min): check open picks vs live prices → resolve WIN/LOSS
5. Reconcile (every 15 min): bar-path resolution on daily OHLC
```

## Database Schema (Key Tables)

| Table | Rows (approx) | Purpose |
|-------|---------------|---------|
| `predictions` | ~223k+ | All picks, v1 legacy + v3.2 (v3.2 era: id ≥ 223438) |
| `ghost_v3_model` | 44+ | Per-symbol XGBoost model pickle + metadata |
| `ghost_state` | ~30 keys | Key-value state: pauses, audit log, lineage, cache timestamps |
| `ghost_squeeze_outcomes` | growing | Intraday squeeze predictions + EOD resolution |
| `ghost_news_articles` | growing | Finnhub + manual news imports |
| `ghost_feature_snapshots` | growing | Point-in-time feature vectors |
| `ghost_perf_cycles` | growing | Per-cycle performance journal |
| `ghost_perf_symbol_evals` | growing | Per-symbol evaluation per cycle |
| `health_audit_runs` | growing | Persistent health audit history |
| `user_portfolio` | user data | Manual/Cash App portfolio imports |
| `wolf_signal_alerts` | growing | Per-pick Telegram alert dedup |

---

# 3. ROUTE INVENTORY (62 endpoints)

## Public (no auth)
| Route | Source | Dependencies | Risk |
|-------|--------|-------------|------|
| `GET /health` | wolf_app.py | DB, price feeds, scheduler | LOW |
| `GET /api/health` | wolf_app.py | Same as /health | LOW |
| `GET /api/_version` | wolf_app.py | Env vars only | LOW |
| `GET /api/stats` | wolf_app.py | DB (predictions) | LOW |
| `GET /api/stats/v32` | wolf_app.py | DB (predictions) | LOW |
| `GET /api/stats/confidence-buckets` | wolf_app.py | DB (predictions) | LOW |
| `GET /api/picks` | wolf_app.py | DB (predictions) | LOW |
| `GET /api/history` | wolf_app.py | DB (predictions) | LOW |
| `GET /api/news` | wolf_app.py | news_store | LOW |
| `GET /api/v3/status` | wolf_app.py | DB, signal_engine | MEDIUM |
| `GET /api/v3/lineage` | wolf_app.py | DB (ghost_state) | LOW |
| `GET /api/coverage` | wolf_app.py | DB, signal_engine | LOW |
| `GET /api/regime` | wolf_app.py | None (no-op in WOLF mode) | LOW |
| `GET /api/objective` | wolf_app.py | prediction module | LOW |
| `GET /api/objective/report` | wolf_app.py | prediction module | LOW |
| `GET /api/schema` | wolf_app.py | DB (information_schema) | LOW |
| `GET /api/symbol-accuracy` | wolf_app.py | DB (ghost_prediction_outcomes) | LOW |
| `GET /api/db-probe` | wolf_app.py | DB (multiple tables) | MEDIUM |
| `GET /api/price/{symbol}` | wolf_app.py | prices module | LOW |
| `GET /api/wolf/gate-status` | wolf_app.py | prediction, signal_engine | MEDIUM |
| `GET /api/wolf/gate-history` | wolf_app.py | DB (ghost_state) | LOW |
| `GET /api/wolf/pick-journal` | wolf_app.py | DB (predictions) | MEDIUM |
| `GET /api/wolf/pnl` | wolf_app.py | DB (predictions), pnl module | LOW |
| `GET /api/wolf/kill-status` | wolf_app.py | prediction module | LOW |
| `GET /api/wolf/daily-summary` | wolf_app.py | DB (ghost_state) | LOW |
| `GET /api/wolf/performance-log/*` | wolf_app.py | DB (perf tables) | LOW |
| `GET /api/shadow-stats` | wolf_app.py | shadow_outcomes | LOW |
| `GET /api/squeeze/status` | wolf_app.py | squeeze_monitor | LOW |
| `GET /api/squeeze/picks` | wolf_app.py | squeeze_monitor | LOW |
| `GET /api/squeeze/daily-log` | wolf_app.py | squeeze_outcomes | LOW |
| `GET /api/ghost/contract` | wolf_app.py | ghost_contract | LOW |
| `GET /api/ghost/blueprint` | wolf_app.py | Multiple phase 1+2 modules | MEDIUM |
| `GET /api/ghost/regime` | wolf_app.py | regime_classifier | LOW |
| `GET /api/ghost/drift` | wolf_app.py | feature_drift | LOW |
| `GET /api/ghost/sentiment` | wolf_app.py | news, news_sentiment | LOW |
| `GET /api/ghost/options` | wolf_app.py | options_flow | LOW |
| `GET /api/telegram/status` | wolf_app.py | DB (wolf_signal_alerts) | LOW |
| `GET /api/cockpit/context` | wolf_app.py | DB, health, v3_status | MEDIUM |
| `GET /api/v1/ghost-score` | wolf_app.py | wolf_endpoints | MEDIUM |
| `GET /cockpit` | wolf_app.py | Static HTML | LOW |
| `GET /version` | wolf_app.py | Env vars | LOW |
| `GET /robots.txt` | wolf_app.py | Static | LOW |
| `GET /sitemap.xml` | wolf_app.py | Static | LOW |

## Cron-Auth (x-cron-secret header)
| Route | Risk |
|-------|------|
| `POST /api/morning-card` | HIGH — triggers full prediction cycle + Telegram |
| `POST /api/run-predictions` | HIGH — triggers prediction cycle |
| `POST /api/reconcile` | MEDIUM — resolves open picks |
| `POST /api/retrain` | HIGH — trains XGBoost model |
| `POST /api/migrate-outcomes` | HIGH — bulk DB migration |
| `POST /api/clean-garbage` | HIGH — destructive DB cleanup |
| `POST /api/watchdog` | MEDIUM — checks open positions |
| `POST /api/wolf/signal-alert/check` | MEDIUM — fires Telegram alerts |
| `POST /api/cron/signal-check` | MEDIUM — cron wrapper for signal alerts |
| `POST /api/dedup-picks` | MEDIUM — expires duplicate picks |
| `POST /api/health/audit` | MEDIUM — deep reliability scan |
| `GET /api/diag/data-sources` | LOW — OHLCV source probe |
| `GET /api/debug-signal/{symbol}` | LOW — signal logic trace |

## Admin Cookie-Gated (404 when unauthenticated)
| Route | Risk |
|-------|------|
| `GET /admin` | HIGH — full operator console |
| `GET /admin/health` | MEDIUM — full health details |
| `GET /api/diagnostics` | MEDIUM — full system diagnostics |
| `GET /api/admin/symbol-universe` | LOW — read-only symbol map |
| `GET /api/admin/audit-log` | LOW — operator action history |
| `GET /api/admin/news/import-format` | LOW — documentation |
| `POST /api/admin/news/import` | MEDIUM — data import |
| `POST /api/admin/delete-model` | HIGH — destructive |
| `POST /api/admin/purge-ghost-portfolio` | HIGH — destructive |
| `POST /api/admin/purge-test-predictions` | HIGH — destructive |
| `POST /api/admin/purge-crypto-junk` | HIGH — destructive |
| `POST /api/admin/fix-stock-expiry` | MEDIUM — data correction |
| `POST /api/admin/resume-engine` | HIGH — clears kill-condition pause |
| `POST /api/admin/squeeze-scan` | MEDIUM — forces squeeze scan |
| `POST /api/admin/squeeze-resolve` | MEDIUM — forces EOD resolution |
| `POST /api/admin/shadow-cycle` | MEDIUM — forces shadow cycle |

## MCP Endpoints (OAuth2/MCP token)
| Route | Risk |
|-------|------|
| `POST /mcp` | MEDIUM — JSON-RPC tool invocation |
| `GET /mcp` | LOW — SSE/streamable HTTP |
| `GET /.well-known/oauth-authorization-server` | LOW — OAuth discovery |
| `POST /mcp/authorize` | MEDIUM — OAuth authorization |
| `POST /mcp/token` | MEDIUM — OAuth token exchange |

---

# 4. MARKET DATA FORENSICS

## Price Fetch Chain (per symbol, per cycle)

```
get_stock_price(symbol)
  ├── In-memory cache (60s TTL) → return if fresh
  ├── Alpaca real-time trade (primary) → return if OK
  └── yfinance (fast_info.last_price → history close) → return if OK
```

## OHLCV Fetch Chain (for model training/inference)

```
_fetch_ohlcv(symbol, period) — 3 retries with escalating delay
  Attempt 1:
    ├── Alpaca SIP bars → return if OK
    ├── Alpaca IEX bars → return if OK
    ├── Polygon bars → return if OK
    ├── yfinance history → return if OK
    └── Stooq CSV → return if OK
  Attempt 2 (0.5s delay): same chain
  Attempt 3 (1.0s delay): same chain
```

## Findings

| Check | Status | Evidence |
|-------|--------|----------|
| Price accuracy | ✅ ADEQUATE | Alpaca real-time during RTH; yfinance close otherwise |
| Timestamp accuracy | ✅ ADEQUATE | Alpaca bars have UTC ISO timestamps, converted to CT |
| Update frequency | ✅ ADEQUATE | 60s cache TTL during market hours; 30-min scan interval |
| Staleness detection | ⚠️ PARTIAL | Cache TTL enforced, but no explicit staleness flag on returned prices |
| Fallback behavior | ✅ GOOD | 5-provider chain for OHLCV; 2-provider for real-time price |
| Rate-limit handling | ⚠️ PARTIAL | 3 retries with backoff, but no circuit-breaker on persistent failures |
| Outage behavior | ⚠️ PARTIAL | Returns None on total failure; prediction cycle skips symbol silently |
| yfinance reliability | ❌ POOR | Regular JSON parse failures overnight ("Expecting value: line 1 column 1") |
| RDFN delisted noise | ❌ WASTE | RDFN delisted June 2025 but still fetched across all providers (PR #64 fix pending deploy) |
| Alpaca SIP 403s | ⚠️ KNOWN | Some symbols get SIP 403; IEX fallback usually works |
| WOLF data thinness | ⚠️ KNOWN | ~250 post-Chapter-11 trading days; peer-pooling compensates |

---

# 5. PORTFOLIO ENGINE AUDIT

## Findings

| Check | Status | Evidence |
|-------|--------|----------|
| Cost basis tracking | ✅ GOOD | `user_portfolio` table with entry price, shares, symbol |
| Current value | ✅ GOOD | Auto-refresh every 15 min via scheduler |
| Gain/loss calculation | ✅ GOOD | `pnl.py` — realized_pnl() with limit-fill pricing |
| Ghost portfolio purge | ✅ GOOD | Boot self-healing removes ZZE2E*/GHOST/TEST rows |
| Position sizing | ✅ GOOD | `risk_discipline.py` — 1% risk per trade, Kelly-inspired |
| Equity curve | ✅ GOOD | Sequential compounding in `pnl.py` |
| Profit factor | ✅ GOOD | Gross wins / gross losses |
| Max drawdown | ✅ GOOD | Peak-to-trough on equity curve |
| Missing assets | ⚠️ N/A | Portfolio is user-managed (manual/Cash App imports) |
| Duplicate detection | ✅ GOOD | Unique index on open predictions per symbol |

---

# 6. SIGNAL ENGINE AUDIT

## v3.2 XGBoost Engine

```
predict_live_ex(symbol, asset_type, scores)
  1. Load model from ghost_v3_model (per-symbol pickle + meta)
  2. Model serve guard: label_type, feature_schema, age checks
  3. Fetch intraday OHLCV (5d/1h) via _fetch_ohlcv chain
  4. Compute indicators: EMA(20/50/200), ADX(14), ATR(14), OBV slope, Stochastic %K/%D
  5. Regime gate: skip BUY if below EMA200 + ADX<20 (downtrend+chop)
  6. Model predict_proba → up_prob
  7. Meta gate: edge ≥ V3_MIN_EDGE, holdout acc ≥ V3_MIN_HOLDOUT_ACC, WF checks
  8. Calibration (optional): isotonic/sigmoid Platt scaling on holdout slice
  9. Confidence: clamp(accuracy + (up_prob − min_p) × 4.0, 0.75, 0.95)
  10. Regime calibration (optional): adjust confidence floor by issuance regime
```

## Findings

| Check | Status | Evidence |
|-------|--------|----------|
| Signal source | ✅ CLEAR | XGBoost on TP/SL daily-bar labels, walk-forward validation |
| Input features | ✅ DOCUMENTED | EMA/ADX/ATR/OBV/Stochastic/volume/momentum + sector rel strength |
| Walk-forward validation | ✅ PROPER | Time-ordered train/calib/gate split; no random shuffle |
| Train/serve parity | ✅ GOOD | Same TP/SL bar-path rules for labels and live resolution |
| Feature schema guard | ✅ GOOD | Model rejected if feature_schema doesn't match current |
| Calibration | ✅ GOOD | Isotonic/sigmoid on separate holdout slice (not reused for gate) |
| Regime gate | ✅ GOOD | EMA200 + ADX trend filter blocks buys in downtrends |
| Confidence formula | ⚠️ MAGIC NUMBER | `× 4.0` multiplier is heuristic, not empirically derived |
| Peer pooling | ⚠️ TRADEOFF | Pools sector peers into training; WOLF rows weighted 3× |
| Model staleness | ✅ GOOD | 14-day retrain window; auto-retrain on coverage gaps |
| Minimum training data | ⚠️ LOW FLOOR | `MIN_TRAIN_ROWS=20` (env), `V3_MIN_TP_SL_WINS=10` (env) — very low for statistical significance |

---

# 7. GHOST SCORE FORENSICS

The "Ghost Score" is a composite 0–100 metric assembled in `api/wolf_endpoints.py → ghost_score_payload_sync()`.

## Components (inferred from code)

| Component | Weight (approx) | Source | Reliability |
|-----------|-----------------|--------|-------------|
| Model confidence | ~30% | signal_engine predict_live_ex | HIGH |
| News sentiment | ~15% | Claude Haiku / keyword fallback | MEDIUM |
| Options flow | ~10% | yfinance options chain | LOW (best-effort) |
| Regime context | ~15% | regime_classifier | MEDIUM |
| Wolf context | ~15% | sector/earnings/short-interest | MEDIUM |
| Squeeze signal | ~10% | squeeze_monitor | MEDIUM |
| Technical indicators | ~5% | RSI/MACD/Bollinger | MEDIUM |

## Findings

| Check | Status | Evidence |
|-------|--------|----------|
| Determinism | ⚠️ PARTIAL | News sentiment (Claude Haiku) is non-deterministic |
| Reproducibility | ⚠️ PARTIAL | Options flow is best-effort; may return empty |
| Data dependencies | ⚠️ FRAGILE | Multiple external APIs; any one failing shifts score |
| Mathematical correctness | ⚠️ UNCLEAR | Exact formula not documented in a single location |
| Confidence alignment | ✅ GOOD | Score correlates with model confidence |

---

# 8. AI DECISION ENGINE AUDIT

## AI Usage in Ghost

| Component | AI Model | Role | Risk |
|-----------|----------|------|------|
| News Sentiment | Claude Haiku 4.5 | Scores headlines -1.0 to +1.0 | LOW — affects sentiment weight only |
| Ghost Ask | Claude Haiku | Q&A grounded in live state | LOW — read-only, daily rate limit |
| World Feed Fusion | NLP (unspecified) | RSS sentiment from Reuters/Bloomberg | LOW — supplementary |

## Findings

| Check | Status | Evidence |
|-------|--------|----------|
| LLM makes trading decisions? | ✅ NO | Claude only scores news sentiment; model makes the call |
| Prompt injection risk | ✅ LOW | Structured prompt with JSON-only output constraint |
| Hallucination protection | ✅ GOOD | Keyword fallback when Claude unavailable; JSON parse with bounds clamping |
| Data flow to AI | ✅ CLEAN | Only headlines sent to Claude, not portfolio or account data |
| Rate limiting | ✅ GOOD | One Claude call per 30-min news cycle, not per prediction |

---

# 9. ALERT SYSTEM AUDIT

## Alert Paths

```
Prediction Fired
  ├── Telegram (primary) — HTML parse_mode, bot token + chat ID
  ├── Discord (fallback) — webhook URL
  ├── Email (SMTP) — env-gated, best-effort
  └── SMS (Twilio) — env-gated, best-effort

Position Hit (Watchdog)
  └── Telegram position alert (WIN/LOSS with P&L)

Daily Card (Morning Cron)
  ├── High-conviction pick → Full Daily Card
  └── No pick → Silence Card

Weekly Summary
  └── Sunday 6 PM CT → Weekly Card

Kill Condition Trip
  └── Telegram health alert (one-time per trip reason)

Risk Discipline
  └── Telegram risk alert (daily loss lock, portfolio exit)
```

## Findings

| Check | Status | Evidence |
|-------|--------|----------|
| Deduplication | ✅ GOOD | Per-pick `wolf_signal_alerts` table; per-day card date check |
| Suppression | ✅ GOOD | Daily cap (2 alerts/day); cooldown (7200s squeeze) |
| Retry logic | ⚠️ NONE | Telegram send is fire-and-forget; no retry on failure |
| Rate limits | ✅ GOOD | Telegram: daily cap; Squeeze: per-symbol cooldown |
| Failure handling | ⚠️ PARTIAL | Logs errors, returns False; no dead-letter queue |
| Alert accuracy | ✅ GOOD | Confidence floor (0.75) filters weak signals |

---

# 10. NEWS ENGINE FORENSICS

## News Flow

```
run_news_cycle() — every 30 min
  1. Select batch of 8 symbols from watchlist (rotating)
  2. Finnhub company-news API (3-day window, 2 retries)
  3. Persist to ghost_news_articles (upsert by title+symbol+date)
  4. Score via Claude Haiku (primary) or keyword lexicon (fallback)
  5. Store per-symbol sentiment in _symbol_sentiment dict
  6. prediction.py reads get_symbol_sentiment() for news influence
```

## Findings

| Check | Status | Evidence |
|-------|--------|----------|
| Wrong news → wrong scores? | ⚠️ POSSIBLE | Finnhub tags articles by company but includes market roundups mentioning other tickers |
| WOLF relevance filter | ✅ GOOD | `_is_wolf_relevant()` checks article text for WOLF/Wolfspeed/SiC keywords |
| Duplicate news | ✅ GOOD | Upsert by title+symbol+date prevents duplicates |
| Stale news | ✅ GOOD | 3-day Finnhub window; articles age out naturally |
| Claude cost | ✅ LOW | One Haiku call per 30-min cycle (~300 tokens) |
| Keyword fallback | ✅ ADEQUATE | Bullish/bearish word lists provide reasonable baseline |

---

# 11. RESILIENCE AUDIT

## Failure Mode Analysis

| Failure | System Behavior | Severity |
|---------|----------------|----------|
| Alpaca API down | Falls through to yfinance → Polygon → Stooq | MEDIUM |
| All price sources down | Returns None; prediction cycle skips symbol silently | HIGH |
| yfinance JSON errors | Logged, falls through to next provider | LOW |
| PostgreSQL down | All DB operations fail; health score drops; app continues serving static routes | CRITICAL |
| Telegram API down | Alert lost; logged; no retry | MEDIUM |
| Claude API down | Falls back to keyword sentiment scoring | LOW |
| Finnhub API down | News cycle skips; stale sentiment scores persist | LOW |
| Scheduler loop crash | All background tasks stop; no auto-restart | CRITICAL |
| Railway restart | In-memory state lost (caches, rate-limit counters); boot self-healing recovers | MEDIUM |
| Memory exhaustion | No monitoring; process would be killed by Railway | HIGH |

## Recovery Mechanisms

| Mechanism | Coverage |
|-----------|----------|
| Boot self-healing | ✅ Morning card recovery, portfolio ghost purge, model purge, pick cleanup |
| Auto-retrain | ✅ Coverage maintenance keeps models above floor |
| Kill-condition auto-resume | ✅ Cooldown-only trips auto-resume; harder trips need manual |
| Health audit auto-fix | ✅ Some findings have auto-fix hooks |
| No circuit breaker on external APIs | ❌ Persistent API failures are not circuit-broken |

---

# 12. CACHE FORENSICS

| Cache | Location | TTL | Invalidation | Risk |
|-------|----------|-----|-------------|------|
| Stock price | `_mem_cache` dict | 60s | Time-based | LOW |
| Intraday OHLC | `_intraday_cache` dict | 900s | Time-based | MEDIUM — stale OHLC during fast moves |
| OHLCV training | `_OHLCV_CACHE` dict | Session | Manual `clear_ohlcv_cache()` | LOW |
| Cockpit DB | `_COCKPIT_DB_CACHE` dict | 8s | Time-based + manual bump | LOW |
| Model | `_model_cache` (signal_engine) | 3600s | Time-based disk reload | LOW |
| Short interest | `_short_cache` dict | 86400s | Time-based | LOW |
| Sentiment | `_symbol_sentiment` dict | 1800s | Per news cycle refresh | LOW |
| Objective mode | `_OBJECTIVE_RUNTIME_MODE_CACHE` | 45s | Time-based + DB read | LOW |
| Rate-limit hits | `_RL_HITS` defaultdict | 60s sliding window | Time-based eviction | LOW |
| Squeeze scan | `_scan_cache_path` JSON file | Session | Per scan overwrite | LOW |

## Findings

| Check | Status | Evidence |
|-------|--------|----------|
| Cross-worker sync | ⚠️ N/A | Single-instance; no cross-worker needed |
| Stale data risk | ⚠️ MODERATE | Intraday OHLC cached 900s; fast moves may render stale |
| Cache poisoning | ✅ LOW | All caches are server-populated, not user-controlled |
| Dashboard drift | ✅ LOW | Cockpit cache TTL is 8s; explicit bump on retrain/purge |

---

# 13. SECURITY AUDIT

## Findings

| Check | Status | Evidence |
|-------|--------|----------|
| Admin authentication | ✅ GOOD | HMAC-signed cookie (SHA256, 8h TTL, HttpOnly, SameSite=Lax) |
| Cron authentication | ✅ GOOD | Constant-time HMAC compare on x-cron-secret header |
| API key storage | ✅ GOOD | Environment variables only; never logged or exposed |
| Swagger/OpenAPI disabled | ✅ GOOD | `DOCS_ENABLED=0` in prod; no `/docs` or `/redoc` |
| Admin routes hidden | ✅ GOOD | `include_in_schema=False`; 404 when unauthenticated |
| CSP headers | ✅ GOOD | `default-src 'self'`; frame-ancestors 'none'; script-src limited |
| Rate limiting | ✅ GOOD | 120 RPM per IP sliding window on `/api/` routes |
| HSTS | ✅ GOOD | `max-age=31536000; includeSubDomains` |
| Clickjacking protection | ✅ GOOD | `X-Frame-Options: DENY` + `frame-ancestors 'none'` |
| MCP OAuth | ✅ GOOD | OAuth 2.1 with PKCE, JWT access tokens, single-user |
| Portfolio data exposure | ⚠️ PARTIAL | Portfolio routes are public; no auth on `/api/portfolio` |
| Secrets in logs | ✅ GOOD | No secrets logged; `_safe_log_snippet` suppresses HTML bodies |
| SQL injection | ✅ GOOD | Parameterized queries throughout; no string concatenation |
| No wallet integration | ✅ N/A | No crypto wallets; stock-only system |

---

# 14. PERFORMANCE AUDIT

## Architecture Limitations

| Concern | Detail |
|---------|--------|
| Single-process | All background tasks, monitors, and HTTP handling in one uvicorn worker |
| No connection pooling for external APIs | Each HTTP call opens a new connection |
| Synchronous DB access in async routes | `db_conn()` is synchronous; may block event loop under load |
| In-memory rate limiting | `_RL_HITS` defaultdict grows unbounded (mitigated by periodic cleanup at 4096 entries) |
| No Redis | All state lost on restart; no shared state for horizontal scaling |
| OHLCV fetch latency | 5-provider chain with 3 retries = up to 15 HTTP calls per symbol per cycle |

## Bottleneck Analysis

| Operation | Est. Latency | Frequency |
|-----------|-------------|-----------|
| OHLCV fetch (full chain) | 2-8s | Per symbol per scan |
| Model inference | <100ms | Per symbol per scan |
| DB query (simple) | 10-50ms | Per route |
| News cycle (Claude) | 1-3s | Every 30 min |
| Squeeze scan (43 symbols) | 30-60s | Every 60s during RTH |
| Morning card assembly | 2-5s | Once daily |

---

# 15. DEAD CODE DETECTION

| Item | Status | Evidence |
|------|--------|----------|
| `core/model.py` | ⚠️ ZOMBIE | Legacy XGBoost model; v3.2 uses `signal_engine.py` instead. `train_model()` and `predict_with_model()` still exist but are not the active path. |
| `core/stock_engine.py` | ⚠️ ZOMBIE | Legacy stock-specific model; superseded by v3.2 signal_engine |
| `core/regime.py` | ⚠️ ZOMBIE | Rule-based regime tag; `regime_classifier.py` is the active path |
| `config/settings.py` | ⚠️ STALE | Pydantic Settings class with v3 thresholds that don't match live env vars |
| `ghost_prediction_outcomes` table | ⚠️ LEGACY | v1 outcomes table; still queried by `/api/symbol-accuracy` and `/api/debug-signal` |
| `ghost_models` table | ⚠️ LEGACY | v1 model table; auto-purged on boot but still checked |
| `paper_trades` table | ⚠️ UNUSED | Created in schema but no code writes to it |
| `price_cache` table | ⚠️ UNUSED | Created in schema but in-memory cache is used instead |
| `/api/retrain` endpoint | ⚠️ LEGACY | Trains on `ghost_prediction_outcomes` (v1 data); v3.2 uses `train_and_validate` |
| `/api/debug-signal/{symbol}` | ⚠️ LEGACY | References v1 circuit breaker logic; dead code block at end of function |
| `engines/startup.py` | ⚠️ ZOMBIE | `_on_startup` is not invoked; lifespan in `wolf_app.py` handles startup |
| `scripts/retrain.py` | ⚠️ STALE | References old feature set; v3.2 training is in signal_engine |

---

# 16. TRUST SCORE REPORT

| Dimension | Score | Rationale |
|-----------|-------|-----------|
| **Market Data Integrity** | 72/100 | Multi-provider chain is robust, but free-tier gaps (Alpaca SIP 403s, yfinance flakiness) and no paid feed mean data quality is adequate but not institutional-grade. |
| **Portfolio Accuracy** | 85/100 | Realized P&L with limit-fill pricing, equity curve compounding, profit factor, max drawdown. Ghost portfolio purge on boot. Position sizing at 1% risk. |
| **Signal Reliability** | 78/100 | Walk-forward XGBoost with proper time-series split. Shared TP/SL rules for train/serve. But WOLF data is thin (~250 days), peer-pooling is a crutch, and confidence formula uses heuristic multiplier. |
| **Ghost Score Reliability** | 70/100 | Composite of multiple probes, but some are best-effort (options flow), one is non-deterministic (Claude sentiment), and exact formula is not documented in one place. |
| **AI Reliability** | 65/100 | Claude Haiku used only for sentiment scoring, not trading decisions. Keyword fallback exists. But LLM sentiment is inherently noisy and non-deterministic. |
| **Alert Reliability** | 80/100 | Multi-channel (Telegram + Discord + email/SMS). Per-pick dedup, daily caps, cooldowns. But no retry on send failure; fire-and-forget. |
| **Security** | 82/100 | HMAC-signed admin cookies, CSP headers, rate limiting, no Swagger in prod, constant-time secret comparison. Portfolio routes are unauthenticated (minor). |
| **Performance** | 75/100 | Single-process architecture is simple and adequate for current load, but no horizontal scaling path. Synchronous DB in async routes. No Redis. |
| **Resilience** | 68/100 | Multi-provider fallbacks for market data. Boot self-healing. But no circuit breakers on external APIs, no dead-letter queue for alerts, in-memory state lost on restart. |
| **Observability** | 85/100 | Health audit with persistent findings, diagnostics endpoint, performance log (cycles/symbols/events), pick journal with Wilson CI, admin audit log, daily summaries. |
| **OVERALL TRUST SCORE** | **76/100** | **CONDITIONALLY TRUSTWORTHY** |

---

# 17. MASTER DEFECT REGISTER

## PROVEN Defects (evidence from code + Railway logs)

| ID | Severity | File | Evidence | Impact | Confidence |
|----|----------|------|----------|--------|------------|
| D-01 | **HIGH** | `wolf_app.py` L5015 | `_RUNNING_PR_VERSION = 64` but Railway deploy is PR #63 (June 15). RDFN still fetched in logs. | PR #64 fix (RDFN removal) not deployed; wasted API calls | 100% |
| D-02 | **MEDIUM** | `core/prices.py` | yfinance JSON parse errors in Railway logs: "Expecting value: line 1 column 1 (char 0)" for WOLF overnight | Price fetch fails regularly; multi-provider chain mitigates | 100% |
| D-03 | **MEDIUM** | `core/wolf_context.py` | Railway log: "EDGAR fetch for WOLF failed: No module named 'core.edgar_integration'" | Missing module; EDGAR/SEC 8-K data unavailable | 100% |
| D-04 | **LOW** | `config/settings.py` | Pydantic Settings has `V3_MIN_CONFIDENCE: float = 0.78` but live env uses `MIN_ALERT_CONFIDENCE=0.75` | Config file stale; not the source of truth | 90% |
| D-05 | **MEDIUM** | `core/signal_engine.py` | Confidence formula uses `× 4.0` multiplier without empirical derivation | Confidence scores may be miscalibrated | 80% |
| D-06 | **LOW** | `core/model.py` | `train_model()` and `predict_with_model()` still exist but v3.2 uses `signal_engine.py` | Dead code; could confuse future developers | 95% |
| D-07 | **LOW** | `wolf_app.py` `/api/debug-signal` | Dead code block at end of function (duplicate logic after return statement) | Unreachable code | 100% |
| D-08 | **MEDIUM** | `core/telegram.py` | `_send()` is fire-and-forget; no retry on failure | Alerts can be silently lost | 85% |
| D-09 | **LOW** | `core/scheduler.py` | Single asyncio loop; if one task blocks, all tasks stall | No task isolation | 70% |
| D-10 | **MEDIUM** | `core/prices.py` | `INTRADAY_QUOTE_TTL_S = 900` (15 min); OHLC can be stale during fast intraday moves | Squeeze radar may miss rapid price changes | 75% |
| D-11 | **LOW** | `wolf_app.py` | `paper_trades` and `price_cache` tables created in schema but never written to | Unused DB tables | 100% |
| D-12 | **LOW** | `engines/startup.py` | `_on_startup` function exists but is never called; lifespan in wolf_app.py handles startup | Dead code | 100% |

---

# 18. BASELINE INTEGRITY CHECK

| Question | Answer | Evidence |
|----------|--------|----------|
| Can current baseline be frozen? | ✅ YES | Code is committed, tagged by PR version, deploy metadata embedded |
| Can current baseline be trusted? | ⚠️ CONDITIONALLY | Trust score 76/100; documented limitations apply |
| Can current baseline be deployed? | ✅ YES | PR #64 is ready to deploy; `railway up` will push it |
| Can it survive API outages? | ⚠️ PARTIALLY | Multi-provider chain handles single failures; total market data outage = silent failure |
| Can it survive stale market data? | ⚠️ PARTIALLY | Cache TTLs prevent indefinite staleness; but no explicit staleness flag |
| Can it survive bad news data? | ✅ YES | Keyword fallback when Claude unavailable; WOLF relevance filter |
| Can it survive partial failures? | ⚠️ PARTIALLY | Graceful degradation in most modules; but no circuit breakers |

---

# FINAL VERDICT

**Ghost Protocol v2 is a serious, honestly-built trading signal system that can be trusted as a directional aid — not as a guaranteed profit machine.**

The developer has done the hard work:
- Pre-registered the 80% claim and abandoned it when data falsified it
- Built walk-forward validation without look-ahead bias
- Implemented defense-in-depth with four-gate chain and kill conditions
- Created comprehensive observability (health audit, performance log, pick journal)
- Hardened security (HMAC cookies, CSP, rate limiting)

The limitations are structural, not careless:
- Free-tier market data (no institutional feeds)
- Thin WOLF history post-Chapter-11
- Single-instance architecture (no Redis, no horizontal scaling)
- Heuristic confidence calibration

**Recommendation**: Trust Ghost as a selective directional filter with ~55-65% accuracy on WOLF. Use position sizing (1% risk per trade). Never bet more than you can lose. The system is honest about its limitations — that honesty is its strongest asset.
