# GHOST PROTOCOL — MAXIMUM ACCURACY BLUEPRINT
## Pure Code Only — Zero Dollars — Free Will Edition
### 2026-06-20

---

# THE PREMISE

No paid APIs. No Redis. No new infrastructure. Just code, math, and free data sources.
Every item below is achievable with `pip install` and the data Ghost already has
or can get for free.

---

# THE 8-PILLAR ARCHITECTURE

```
                    ┌──────────────────────────────────────┐
                    │         META-STACKING ENSEMBLE         │
                    │  XGBoost + LightGBM + CatBoost + RF   │
                    │         → LogisticRegression          │
                    └──────────────┬───────────────────────┘
                                   │
        ┌──────────────┬───────────┼───────────┬──────────────┐
        ▼              ▼           ▼           ▼              ▼
   ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐
   │Technical│  │  Cross- │  │  Macro  │  │  Funda- │  │  Crowd  │
   │  (32)   │  │sectional│  │ Regime  │  │ mental  │  │Sentiment│
   │  EMA,   │  │  (10)   │  │   (8)   │  │  (12)   │  │   (6)   │
   │  RSI,   │  │ Rank in │  │  VIX,   │  │ Revenue │  │ Reddit, │
   │  ADX,   │  │watchlist│  │  FRED,  │  │  Debt,  │  │StockTwit│
   │  ATR... │  │  on 8   │  │  Yield  │  │  Cash   │  │ Google  │
   │         │  │ metrics │  │  Curve  │  │  Flow   │  │ Trends  │
   └─────────┘  └─────────┘  └─────────┘  └─────────┘  └─────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
              ┌──────────┐  ┌──────────┐  ┌──────────┐
              │ 1-Day    │  │ 3-Day    │  │ 5-Day    │
              │ Horizon  │  │ Horizon  │  │ Horizon  │
              └────┬─────┘  └────┬─────┘  └────┬─────┘
                   │             │             │
                   └─────────────┼─────────────┘
                                 ▼
                    ┌──────────────────────┐
                    │  CONFORMAL CALIBRATION │
                    │  Prediction intervals  │
                    │  + calibrated prob     │
                    └──────────┬───────────┘
                               ▼
                    ┌──────────────────────┐
                    │  KELLY + CORRELATION  │
                    │  Position sizing      │
                    │  Portfolio filter     │
                    └──────────────────────┘
```

---

# PILLAR 1: META-STACKING ENSEMBLE (+3-5% accuracy)

## Current State
Single XGBoost model. `_ProbaEnsemble` class exists for XGBoost+RF soft-voting
but is gated behind `V3_ENSEMBLE=on` and not activated in production.

## The Upgrade
Stacking: train 4 base models (XGBoost, LightGBM, CatBoost, RandomForest),
each probability-calibrated on the holdout slice. Then train a LogisticRegression
meta-model on their out-of-fold predictions. The meta-model learns which base
model to trust under which conditions.

## Why It Works
- Different algorithms capture different patterns (tree splits vs gradient boosting vs bagging)
- Stacking is proven in Kaggle — top-1% solutions use it
- LogisticRegression meta-model is simple, interpretable, and doesn't overfit
- Each base model is independently calibrated, so the meta-model sees calibrated probabilities

## Implementation
```python
# Already have _ProbaEnsemble. Extend to:
class _StackingEnsemble:
    base_models: list       # [XGBoost, LightGBM, CatBoost, RandomForest]
    meta_model: LogisticRegression
    def predict_proba(self, X):
        base_probas = [m.predict_proba(X)[:, 1] for m in self.base_models]
        stacked = np.column_stack(base_probas)
        return self.meta_model.predict_proba(stacked)
```

## Dependencies
- `pip install lightgbm catboost` (already have xgboost, scikit-learn)
- Both are pure Python, no paid API

## Activation
Set `V3_ENSEMBLE=stacking` in Railway. Triggers retrain with 4-model stack on next cycle.

---

# PILLAR 2: CROSS-SECTIONAL FEATURES (+2-3% accuracy)

## Current State
32 technical features per symbol, all computed in isolation. No awareness of
how a symbol ranks relative to peers.

## The Upgrade
For each symbol in the 43-symbol watchlist, compute 8 cross-sectional features:
1. **RSI rank** — percentile of RSI within watchlist (0=most oversold, 1=most overbought)
2. **Volume ratio rank** — percentile of relative volume
3. **Momentum rank** — percentile of 4h price momentum
4. **Distance from SMA rank** — percentile of (price - SMA_20) / SMA_20
5. **ATR rank** — percentile of volatility
6. **ADX rank** — percentile of trend strength
7. **Short-float rank** — percentile of short interest (when available)
8. **Sector correlation** — average correlation with peer symbols over 20 days

## Why It Works
- "Most oversold stock in the watchlist" is a stronger signal than "RSI=32"
- Cross-sectional features are orthogonal to time-series features — no multicollinearity
- Quant funds use cross-sectional rankings extensively (the "quantile" approach)
- Free — just compute across the 43 symbols Ghost already fetches

## Implementation
During each scan cycle, after all 43 symbols have their technical features computed,
do a second pass to compute percentile ranks. Append 8 cross-sectional features
to the feature vector. Total features: 32 + 8 = 40.

---

# PILLAR 3: MACRO REGIME FROM FRED (+1-2% accuracy)

## Current State
No macroeconomic context. The model doesn't know if we're in a bull market,
bear market, high-rate environment, or recession scare.

## The Upgrade
Fetch 8 free macro features from FRED (Federal Reserve Economic Data):
1. **VIX** — volatility index (via yfinance ^VIX or FRED)
2. **10Y-2Y yield spread** — recession indicator (FRED series T10Y2Y)
3. **Fed funds rate** — interest rate regime (FRED series DFF)
4. **DXY** — dollar strength (via yfinance DX-Y.NYB)
5. **SPY 20-day return** — broad market direction
6. **SPY vs SMA_50** — market trend
7. **NYSE advance/decline** — market breadth (when available)
8. **Sector ETF relative strength** — SMH (semis) vs SPY

## Why It Works
- Stocks don't move in isolation — macro regime is the tide that lifts or sinks all boats
- VIX > 30 = fear regime, different signal dynamics
- Yield curve inversion = recession probability, affects all equities
- All data is FREE — FRED has no API key requirement, yfinance covers the rest

## Implementation
Add `core/macro_regime.py` — fetches FRED + yfinance macro data once per day,
caches for 24h. Appends 8 macro features to every symbol's feature vector.
Total features: 40 + 8 = 48.

---

# PILLAR 4: FUNDAMENTAL FEATURES FROM SEC EDGAR (+1-2% accuracy)

## Current State
EDGAR integration now fetches 8-K filings (material events). But no fundamental
data from quarterly filings (10-Q, 10-K).

## The Upgrade
Expand the EDGAR client to parse the latest 10-Q/10-K for:
1. **Revenue growth YoY** — from income statement
2. **Gross margin trend** — revenue - COGS / revenue
3. **Debt/equity ratio** — from balance sheet
4. **Free cash flow** — operating CF - capex
5. **Cash burn rate** — for unprofitable companies (most of the watchlist)
6. **Shares outstanding change** — dilution detection
7. **EPS surprise** — actual vs estimated (from earnings 8-K item 2.02)
8. **Days since last filing** — staleness indicator
9. **Insider trading signal** — from Form 4 filings (officer/director buys/sells)
10. **Institutional ownership trend** — from 13F filings
11. **Segment revenue breakdown** — which business lines are growing
12. **Risk factor changes** — from 10-K item 1A updates

## Why It Works
- Fundamentals are lagging but provide context the technical model lacks
- "Revenue growing 40% YoY with improving margins" is a different regime than "cash burn accelerating"
- SEC EDGAR is FREE — no API key, no rate limit concerns at 1 request/day/symbol
- XBRL-formatted filings are machine-readable (SEC requires XBRL since 2009)

## Implementation
Add `core/fundamental_features.py` — uses the existing EDGAR client to fetch
the latest 10-Q/10-K, parses XBRL facts for key metrics. Cached for 24h per symbol.
Appends 12 fundamental features. Total features: 48 + 12 = 60.

---

# PILLAR 5: CROWD SENTIMENT FROM FREE SOURCES (+1-2% accuracy)

## Current State
Sentiment from Finnhub headlines + Claude Haiku (or VADER fallback). Single source.

## The Upgrade
Aggregate sentiment from 6 free sources:
1. **Reddit** — r/WallStreetBets, r/stocks, r/investing mention counts + sentiment (praw, free)
2. **StockTwits** — symbol-specific message sentiment (free API, rate-limited)
3. **Google Trends** — search volume for ticker (pytrends, free)
4. **Wikipedia page views** — attention proxy (free API)
5. **News volume spike** — already have Finnhub, add count-spike detection
6. **Social volume anomaly** — when mentions spike 3σ above baseline

## Why It Works
- Crowd sentiment captures attention and narrative — orthogonal to technicals and fundamentals
- "WOLF trending on WSB" is a real signal for short-squeeze candidates
- Google Trends search spikes often precede price moves (retail attention leads price)
- All sources are FREE with rate-limited but adequate access

## Implementation
Add `core/crowd_sentiment.py` — aggregates 6 sources into a single sentiment vector.
Each source gets a z-score relative to its own 30-day baseline. Final feature:
crowd_sentiment composite (mean of z-scores). Appends 6 features.
Total features: 60 + 6 = 66.

---

# PILLAR 6: MULTI-HORIZON PREDICTION (+2-3% accuracy)

## Current State
Single 3-day horizon. One model predicts "UP in ~3 days."

## The Upgrade
Train separate models for 1-day, 3-day, and 5-day horizons. At inference time,
run all three. The final signal is a weighted vote:
- 1-day model: weight 0.2 (noisiest)
- 3-day model: weight 0.5 (primary, most data)
- 5-day model: weight 0.3 (smoothest)

## Why It Works
- Different horizons capture different dynamics (intraday momentum vs swing trend)
- Ensemble across horizons reduces noise
- If all three agree → high conviction. If only one fires → lower conviction.
- Same TP/SL math, just different hold_bars (1, 3, 5)

## Implementation
Extend `train_and_validate()` to train per-horizon. Store as `model_WOLF_1d`,
`model_WOLF_3d`, `model_WOLF_5d` in `ghost_v3_model`. At inference, run all three,
weighted vote. No new dependencies.

---

# PILLAR 7: CONFORMAL CALIBRATION (+1-2% accuracy)

## Current State
Confidence formula: `clamp(accuracy + (up_prob - min_p) × 4.0, 0.75, 0.95)`.
Heuristic multiplier, not empirically calibrated.

## The Upgrade
Replace with conformal prediction:
1. Hold out a calibration set (already have the calib slice in train/calib/gate split)
2. For each calibration sample, compute nonconformity score: `s = 1 - up_prob` for WIN, `s = up_prob` for LOSS
3. At inference, compute prediction interval: `[up_prob - q, up_prob + q]` where q is the (1-α) quantile of calibration scores
4. The calibrated probability is the lower bound of the interval → conservative, honest

## Why It Works
- Conformal prediction produces VALID prediction intervals (coverage guarantee)
- No assumptions about distribution — distribution-free
- The lower bound is a conservative probability that's mathematically guaranteed
- Replaces the heuristic ×4.0 with a principled statistical method

## Implementation
Add `core/conformal_calibration.py`. After training, compute calibration quantiles
from the holdout slice. Store with model metadata. At inference, apply conformal
adjustment to raw up_prob. No new dependencies — just numpy.

---

# PILLAR 8: KELLY + CORRELATION POSITION SIZING (+0-1% accuracy, +risk management)

## Current State
Fixed 1% risk per trade. No correlation awareness.

## The Upgrade
1. **Kelly criterion**: `f* = edge / odds` where edge = (win_rate × avg_win - loss_rate × avg_loss) / avg_win
2. **Correlation filter**: don't fire simultaneous picks on symbols with >0.7 correlation
3. **Portfolio heat**: scale down position sizes when multiple picks are open
4. **Volatility-adjusted sizing**: smaller positions in high-VIX regimes

## Why It Works
- Kelly is mathematically optimal for maximizing long-term growth
- Correlation filter prevents over-concentration (e.g., don't bet on AMC + GME simultaneously)
- Portfolio heat management prevents over-betting during high-signal periods

## Implementation
Extend `core/risk_discipline.py` with Kelly sizing and correlation filter.
Correlation matrix computed from 60-day returns across the watchlist (free, already have the data).

---

# FEATURE VECTOR: 32 → 66

| Category | Count | Features |
|----------|-------|----------|
| Technical (existing) | 32 | RSI, MACD, BB, ADX, ATR, OBV, Stoch, EMA stack, momentum, volume... |
| Cross-sectional (new) | 8 | RSI rank, volume rank, momentum rank, SMA distance rank, ATR rank, ADX rank, short-float rank, sector correlation |
| Macro regime (new) | 8 | VIX, yield spread, Fed rate, DXY, SPY return, SPY vs SMA, breadth, sector rel strength |
| Fundamental (new) | 12 | Revenue growth, gross margin, debt/equity, FCF, cash burn, shares outstanding, EPS surprise, filing staleness, insider signal, inst ownership, segment revenue, risk factor changes |
| Crowd sentiment (new) | 6 | Reddit mentions, StockTwits sentiment, Google Trends, Wikipedia views, news volume spike, social volume anomaly |
| **Total** | **66** | |

---

# ACCURACY TRAJECTORY

| Phase | What | Est. Gain | Cumulative |
|-------|------|-----------|------------|
| Baseline | Current v3.2 XGBoost (32 features, single model, 3d horizon) | — | ~55-60% |
| Pillar 1 | Meta-stacking ensemble (4 models → LR meta) | +3-5% | ~58-65% |
| Pillar 2 | Cross-sectional features (8 rank features) | +2-3% | ~60-68% |
| Pillar 6 | Multi-horizon (1d/3d/5d weighted vote) | +2-3% | ~62-71% |
| Pillar 7 | Conformal calibration (principled prob) | +1-2% | ~63-73% |
| Pillar 3 | Macro regime (FRED + VIX + yield curve) | +1-2% | ~64-75% |
| Pillar 4 | Fundamental features (SEC EDGAR 10-Q/10-K) | +1-2% | ~65-77% |
| Pillar 5 | Crowd sentiment (Reddit + StockTwits + Trends) | +1-2% | ~66-79% |
| Pillar 8 | Kelly + correlation sizing | +0-1% | ~66-80% |

**Realistic ceiling for directional stock prediction: ~65-70% accuracy.**
The best quant funds achieve ~55-60% directional accuracy. Ghost would be
at the frontier with this architecture.

---

# IMPLEMENTATION ORDER (by impact/effort ratio)

| # | Pillar | Impact | Effort | Ratio | Dependencies |
|---|--------|--------|--------|-------|-------------|
| 1 | Meta-stacking ensemble | +3-5% | 4-6h | ⭐⭐⭐⭐⭐ | `pip install lightgbm catboost` |
| 2 | Cross-sectional features | +2-3% | 2-3h | ⭐⭐⭐⭐⭐ | None (pure compute) |
| 3 | Multi-horizon | +2-3% | 3-4h | ⭐⭐⭐⭐ | None (same data, different labels) |
| 4 | Conformal calibration | +1-2% | 2-3h | ⭐⭐⭐⭐ | None (pure numpy) |
| 5 | Macro regime (FRED) | +1-2% | 3-4h | ⭐⭐⭐ | `pip install fredapi` (free key) |
| 6 | Crowd sentiment | +1-2% | 5-8h | ⭐⭐ | `pip install praw pytrends` |
| 7 | Fundamental (EDGAR) | +1-2% | 8-12h | ⭐⭐ | XBRL parsing is complex |
| 8 | Kelly + correlation | +0-1% | 2-3h | ⭐⭐⭐ | None (pure math) |

---

# WHAT STAYS THE SAME

- All 62 API routes — unchanged
- Cockpit + admin UI — unchanged
- Scheduler + monitors — unchanged
- Database schema — unchanged (feature vector stored in existing JSONB columns)
- Telegram alerts — unchanged
- Circuit breakers + degraded mode — unchanged
- Health audit + diagnostics — unchanged
- All security mechanisms — unchanged

---

# THE HONEST CEILING

Even with all 8 pillars, Ghost would top out at ~70% directional accuracy.
Why? Because:

1. **Financial markets are non-stationary** — the distribution changes. No model can predict structural breaks.
2. **WOLF has ~250 trading days** — thin data. More features help but can't create information that doesn't exist.
3. **Free data has limits** — FRED is macro only, Reddit is noisy, EDGAR is lagging.
4. **Directional prediction is hard** — the best hedge funds with billions in infrastructure hit ~55-60%.

**70% directional accuracy on a 43-stock watchlist with zero-cost infrastructure would be world-class.**
