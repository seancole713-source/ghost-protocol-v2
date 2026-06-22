# GHOST PROTOCOL v2 — IMPROVEMENT & UPGRADE PLAN
**Based on:** Master Forensic System Audit (2026-06-19)
**Trust Score Baseline:** 76/100
**Target Trust Score:** 88/100
**Repo State:** PR #64 (main), PR #63 on Railway

---

# PRIORITY MATRIX

| Tier | Definition | Count |
|------|-----------|-------|
| 🔴 **P0 — Immediate** | Production issue, data integrity, or deploy gap. Ship within 24h. | 3 |
| 🟠 **P1 — High** | Material trust/reliability improvement. Ship within 1 week. | 5 |
| 🟡 **P2 — Medium** | Meaningful improvement, not urgent. Ship within 1 month. | 6 |
| 🟢 **P3 — Low** | Nice-to-have, cleanup, or future architecture. | 5 |

---

# 🔴 P0 — IMMEDIATE (Ship within 24h)

## P0-1: Deploy PR #64 to Railway
**Defect:** D-01 | **Current State:** Repo at `f9164c3` (PR #64), Railway at `66da1f9` (PR #63)
**Evidence:** RDFN (delisted June 2025) still being fetched across 4 providers × 3 retries = 12 failing HTTP calls per cycle. Wasted API quota, log spam.
**Fix:** `railway up` from repo root. Already committed, just needs deploy.
**Impact:** Eliminates RDFN fetch spam. `fetch_failed_symbols` exposed to cockpit. `_pr_version` becomes 64.
**Risk:** Near-zero — PR #64 is already on main, tests pass.

## P0-2: Add yfinance Circuit Breaker
**Defect:** D-02 | **Current State:** yfinance JSON parse errors every 5 minutes overnight for WOLF. No backoff.
**Evidence:** Railway logs: `"Failed to get ticker 'WOLF' reason: Expecting value: line 1 column 1 (char 0)"` repeating every 5 min.
**Fix:** Add a circuit breaker in `core/prices.py` — after N consecutive yfinance failures, skip yfinance for M minutes, fall through to next provider immediately.
**Implementation:**
```python
_yfinance_fail_streak = 0
_yfinance_circuit_open_until = 0
_YFINANCE_CB_THRESHOLD = 5      # consecutive failures to open circuit
_YFINANCE_CB_COOLDOWN_S = 600   # 10 min cooldown

def _yfinance(symbol):
    global _yfinance_fail_streak, _yfinance_circuit_open_until
    now = time.time()
    if _yfinance_circuit_open_until and now < _yfinance_circuit_open_until:
        return None  # circuit open, skip
    try:
        # ... existing yfinance logic ...
        _yfinance_fail_streak = 0
        return price
    except Exception:
        _yfinance_fail_streak += 1
        if _yfinance_fail_streak >= _YFINANCE_CB_THRESHOLD:
            _yfinance_circuit_open_until = now + _YFINANCE_CB_COOLDOWN_S
            LOGGER.warning("yfinance circuit breaker OPEN for %ss", _YFINANCE_CB_COOLDOWN_S)
        return None
```
**Impact:** Stops 12+ wasted yfinance calls/hour overnight. Reduces log noise.
**Risk:** Low — multi-provider chain already handles yfinance absence.

## P0-3: Stub EDGAR Module
**Defect:** D-03 | **Current State:** `core.edgar_integration` referenced but doesn't exist.
**Evidence:** Railway log: `"EDGAR fetch for WOLF failed: No module named 'core.edgar_integration'"`
**Fix:** Create `core/edgar_integration.py` as a clean stub that returns `{"available": False, "reason": "not yet implemented"}`. This stops the import error from polluting logs and makes the gap explicit.
**Impact:** Eliminates recurring ImportError in logs. Makes SEC 8-K gap visible in `/api/ghost/blueprint`.
**Risk:** Zero — stub only, no new functionality.

---

# 🟠 P1 — HIGH (Ship within 1 week)

## P1-1: Empirically Calibrate Confidence Multiplier
**Defect:** D-05 | **Current State:** `conf = clamp(accuracy + (up_prob − min_p) × 4.0, 0.75, 0.95)`
**Problem:** The `× 4.0` multiplier is heuristic. No evidence it produces well-calibrated probabilities.
**Fix:** Run a calibration study on resolved picks:
1. Collect all (up_prob, outcome) pairs from resolved v3.2 picks
2. Fit isotonic regression: `realized_wr = f(up_prob)`
3. Compute the slope that best maps up_prob → realized win rate
4. Replace `4.0` with the empirically derived slope
5. Add a `CONFIDENCE_SLOPE` env var so it's tunable without deploy
**Impact:** Confidence scores that actually predict win probability. Improves Signal Reliability score from 78→85.
**Risk:** Medium — changes confidence values; should be A/B validated against current formula before permanent switch.

## P1-2: Telegram Alert Retry with Dead-Letter Queue
**Defect:** D-08 | **Current State:** `_send()` is fire-and-forget. No retry.
**Fix:**
1. Add retry logic: 3 attempts with 2s/4s/8s backoff
2. On final failure, write alert to `ghost_state.telegram_dead_letter` queue
3. Add `/api/admin/telegram/dead-letter` endpoint to view/replay failed alerts
4. Add `telegram_alert_failures` metric to health audit
**Impact:** Alerts no longer silently lost. Improves Alert Reliability from 80→88.
**Risk:** Low — retry is standard; dead-letter is append-only.

## P1-3: Add External API Circuit Breakers
**Defect:** None specific, systemic gap | **Current State:** No circuit breakers on Finnhub, Polygon, Alpaca, Anthropic.
**Fix:** Create `core/circuit_breaker.py` — generic sliding-window circuit breaker:
```python
@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    cooldown_seconds: int = 300
    half_open_max: int = 2
    # Tracks failures in sliding window, opens circuit, half-open probing
```
Apply to: Finnhub (news), Polygon (OHLCV), Alpaca (prices), Anthropic (sentiment).
**Impact:** Prevents cascading waste when an API is down. Improves Resilience from 68→78.
**Risk:** Low — each API already has fallback; circuit breaker just skips faster.

## P1-4: Add Staleness Flag to Price Cache
**Defect:** None specific, audit finding | **Current State:** Cache TTL enforced but no explicit staleness metadata.
**Fix:** Extend `get_stock_price()` and `get_intraday_session()` to return `stale: true` when cache is older than TTL but still served (e.g., all providers failed). Add `data_freshness` field to `/api/squeeze/picks` and `/api/cockpit/context`.
**Impact:** Operator can see when prices are stale. Improves Market Data Integrity from 72→78.
**Risk:** Low — additive field, no breaking change.

## P1-5: Portfolio Route Authentication
**Defect:** Security audit finding | **Current State:** `/api/portfolio` routes are public.
**Fix:** Gate portfolio routes behind the same admin-cookie auth as `/admin`. Portfolio data is personal financial information — it should not be publicly readable.
**Impact:** Closes personal data exposure. Improves Security from 82→88.
**Risk:** Low — cookie auth already exists; just apply to portfolio router.

---

# 🟡 P2 — MEDIUM (Ship within 1 month)

## P2-1: Replace In-Memory Caches with Redis
**Current State:** All caches (price, OHLCV, cockpit, rate-limit, sentiment, short-interest) in process memory.
**Problem:** Lost on Railway restart. No shared state for horizontal scaling.
**Fix:**
1. Add `redis` to `requirements.txt` and `nixpacks.toml`
2. Create `core/cache_store.py` — Redis-backed TTL cache with in-memory fallback
3. Migrate: price cache, OHLCV cache, cockpit cache, rate-limit counters, sentiment cache
4. Keep: model cache (pickle too large for Redis), short-interest cache (86400s TTL, acceptable to lose)
**Impact:** Survives restarts. Enables future horizontal scaling. Improves Resilience from 68→80, Performance from 75→82.
**Risk:** Medium — adds Redis dependency; needs Railway Redis plugin or external Redis.

## P2-2: Task Isolation in Scheduler
**Defect:** D-09 | **Current State:** Single asyncio loop; one blocking task stalls all.
**Fix:**
1. Wrap each task execution in `asyncio.wait_for()` with per-task timeout
2. Add `task_timeout_s` to Task dataclass
3. Log and count timeouts separately from errors
4. Add scheduler task timeout metrics to `/api/diagnostics`
**Impact:** One slow OHLCV fetch no longer delays watchdog resolution. Improves Performance from 75→80.
**Risk:** Low — timeout wrapper is standard; per-task timeouts are env-tunable.

## P2-3: Clean Up Dead Code
**Defects:** D-06, D-07, D-11, D-12
**Items:**
1. Remove `core/model.py` `train_model()` and `predict_with_model()` (v3.2 uses signal_engine)
2. Remove dead code block at end of `/api/debug-signal` (after return statement)
3. Drop unused `paper_trades` and `price_cache` tables from `_ensure_tables()`
4. Remove `engines/startup.py` `_on_startup` (never called)
5. Archive `core/stock_engine.py` and `core/regime.py` with deprecation comments
**Impact:** Cleaner codebase, less confusion for future developers.
**Risk:** Low — each item verified as unreferenced in audit.

## P2-4: Sync `config/settings.py` with Live Env
**Defect:** D-04 | **Current State:** Pydantic Settings has stale defaults.
**Fix:** Update `config/settings.py` to match live Railway env vars:
- `V3_MIN_CONFIDENCE` → remove (use `MIN_ALERT_CONFIDENCE`)
- Add all v3.2 gate env vars with their production defaults
- Add `OBJECTIVE_MODE`, `OBJECTIVE_AUTO_MODE_ENABLED`
- Add squeeze monitor env vars
**Impact:** Single source of truth for configuration. Helps onboarding.
**Risk:** Low — settings.py is not the runtime source; env vars win.

## P2-5: Add Model Explainability
**Current State:** Feature importances logged but not exposed via API.
**Fix:**
1. Add `feature_importance` to `/api/v3/status` per-symbol model meta
2. Add SHAP waterfall endpoint: `GET /api/v3/explain/{symbol}` — returns top 5 features driving latest prediction
3. Add feature importance trend to `/api/v3/lineage` (how importance shifts across retrains)
**Impact:** Operator can understand WHY a prediction was made. Improves Signal Reliability from 78→83.
**Risk:** Low — SHAP is read-only on trained model; no inference change.

## P2-6: Intraday OHLC Cache Invalidation on Significant Move
**Defect:** D-10 | **Current State:** `INTRADAY_QUOTE_TTL_S = 900` (15 min). Fast moves render OHLC stale.
**Fix:** Add a price-change trigger: if live price moves >2% from cached OHLC mid-session, force-refresh OHLC regardless of TTL.
**Impact:** Squeeze radar catches fast moves sooner. Improves Market Data Integrity from 72→76.
**Risk:** Low — adds at most a few extra Alpaca calls per volatile symbol.

---

# 🟢 P3 — LOW (Future / Nice-to-Have)

## P3-1: Paid Market Data Integration
**Current State:** Free-tier Alpaca (SIP 403s), Polygon (free), yfinance (flakey), Stooq (last resort).
**Goal:** Add Polygon paid plan or Alpaca paid plan for reliable SIP data, analyst ratings, short interest.
**Impact:** Eliminates "Key Stats / Analyst / Short Interest still empty" gap. Improves Market Data Integrity from 72→88.
**Cost:** $29-99/month depending on plan.

## P3-2: Horizontal Scaling Path
**Current State:** Single uvicorn worker on Railway.
**Goal:** Document multi-worker setup with Redis-backed state. Add `--workers` flag to Procfile when Redis is available.
**Impact:** Handles higher load. Survives worker crashes.
**Risk:** Needs Redis (P2-1) first.

## P3-3: Real-Time WebSocket Feed for Cockpit
**Current State:** Cockpit polls `/api/cockpit/context` on page load.
**Goal:** Add WebSocket endpoint `/ws/cockpit` pushing live price updates, squeeze alerts, and prediction changes.
**Impact:** Live-updating dashboard without manual refresh.
**Risk:** Medium — adds WebSocket infrastructure.

## P3-4: Full EDGAR/SEC Integration
**Current State:** Stub only (P0-3).
**Goal:** Implement `core/edgar_integration.py` — fetch SEC 8-K filings for WOLF, parse material events, feed into WolfContext.
**Impact:** Material event detection (earnings, restatements, delisting risk, insider trades).
**Risk:** Medium — SEC EDGAR API is rate-limited; needs careful parsing.

## P3-5: Multi-Timeframe Model Ensemble
**Current State:** Single XGBoost model on daily bars.
**Goal:** Add intraday (1h/4h bars) and weekly models. Ensemble vote across timeframes.
**Impact:** More robust signals. Reduces false positives from daily noise.
**Risk:** High — significant new feature engineering and validation needed.

---

# TRUST SCORE TRAJECTORY

| Dimension | Current | After P0 | After P1 | After P2 | Target |
|-----------|---------|----------|----------|----------|--------|
| Market Data Integrity | 72 | 72 | 78 | 82 | 85 |
| Portfolio Accuracy | 85 | 85 | 85 | 85 | 88 |
| Signal Reliability | 78 | 78 | 85 | 88 | 88 |
| Ghost Score Reliability | 70 | 70 | 70 | 75 | 80 |
| AI Reliability | 65 | 65 | 65 | 65 | 70 |
| Alert Reliability | 80 | 80 | 88 | 88 | 90 |
| Security | 82 | 82 | 88 | 88 | 90 |
| Performance | 75 | 75 | 75 | 82 | 85 |
| Resilience | 68 | 75 | 78 | 82 | 85 |
| Observability | 85 | 85 | 88 | 90 | 92 |
| **OVERALL** | **76** | **77** | **82** | **85** | **88** |

---

# EFFORT ESTIMATE

| Tier | Items | Est. Hours | Cumulative Trust Gain |
|------|-------|-----------|----------------------|
| P0 | 3 | 2-4h | +1 (76→77) |
| P1 | 5 | 15-25h | +5 (77→82) |
| P2 | 6 | 30-50h | +3 (82→85) |
| P3 | 5 | 40-80h | +3 (85→88) |
| **Total** | **19** | **87-159h** | **+12 (76→88)** |

---

# RECOMMENDED EXECUTION ORDER

```
Week 1:  P0-1 (Deploy PR #64) → P0-2 (yfinance CB) → P0-3 (EDGAR stub)
         ─── Gateway: system is clean, no known waste ───

Week 2:  P1-1 (Calibrate confidence) → P1-2 (Alert retry) → P1-4 (Staleness flag)
Week 3:  P1-3 (API circuit breakers) → P1-5 (Portfolio auth)
         ─── Gateway: trust score 82, all P1 shipped ───

Month 2: P2-1 (Redis) → P2-2 (Task isolation) → P2-3 (Dead code cleanup)
Month 3: P2-4 (Config sync) → P2-5 (SHAP explainability) → P2-6 (OHLC invalidation)
         ─── Gateway: trust score 85, all P2 shipped ───

Future:  P3 items as budget/time allows
```

---

# NOTES

- **No code modifications are proposed in this document.** This is a planning artifact only.
- All P0 items are non-breaking and backward-compatible.
- P1-1 (confidence calibration) should be validated against historical resolved picks before permanent switch.
- P2-1 (Redis) is the largest architectural change; it unlocks P3-2 (horizontal scaling).
- P3-1 (paid data) is the single highest-impact item for trust score but requires ongoing cost.
