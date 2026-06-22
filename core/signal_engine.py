""" core/signal_engine.py - Ghost Protocol v3.2 Signal Engine
UPGRADES (2026-03-30) from GitHub research:
  - EMA(20/50/200): no BUY below EMA200 (FarisZnf repo - #1 fix)
  - ADX(14): no BUY in choppy/sideways markets ADX<20 (FarisZnf repo)
  - ATR: volatility-aware feature for XGBoost
  - OBV slope: accumulation/distribution signal
  - Stochastic %K/%D: momentum confirmation
  - Regime gate in predict_live blocking BUYs in downtrend+choppy
  - Better XGBoost hyperparams: 200 estimators, depth 4, min_child 3

v3.2: Training labels match live paper trades — WIN = hit vol-based target before stop
within N daily bars (see V3_LABEL_HOLD_BARS), same TP/SL math as core.vol_targets.
"""
import os, time, logging, json
import numpy as np
from typing import Any, Dict, List, Optional
from core.vol_targets import base_vol_pct, stop_pct_from_vol

LOGGER = logging.getLogger("ghost.signal_v3")

# PR #14 deploy-marker — if this line isn't in Railway logs at app startup,
# the deployment did NOT pick up code at or after PR #14. The marker also
# helps disambiguate cached vs fresh Python module loads after redeploy.
LOGGER.info("[signal_engine] MODULE_LOADED PR17_DIAG ohlcv_chain=sip|iex|polygon|yfinance|stooq")

LABEL_TYPE = "tp_sl_daily"
# Phase 5: calendar forward bars + shared resolve path (see core.tp_sl_resolve.LABEL_SCHEMA).
def _v3_label_schema() -> str:
    from core.tp_sl_resolve import LABEL_SCHEMA
    return LABEL_SCHEMA

# Daily bars only: approximate 48h stock hold with this many forward bars (24h each).
V3_LABEL_HOLD_BARS = max(1, int(os.getenv("V3_LABEL_HOLD_BARS", "3")))


# Training thresholds. All read from env at call time so tests can
# monkeypatch.setenv and so ops can ratchet defaults from Railway without
# a code change. Defaults were lowered (post-restructure WOLF only has
# ~250 trading days of SIP data, so the prior pre-bankruptcy defaults
# locked out training entirely).
def _min_train_rows() -> int:
    """Min labeled samples required to attempt training (was 80)."""
    return max(1, int(os.getenv("MIN_TRAIN_ROWS", "20")))


def _min_backtest_bars() -> int:
    """Min OHLCV rows from the feed before backtest_symbol bothers to label (was 100)."""
    return max(1, int(os.getenv("MIN_BACKTEST_BARS", "50")))


def _backtest_window() -> int:
    """Trailing-history window each labeled sample sees (was hardcoded 220).
    Smaller window = more labeled samples from limited data but noisier features."""
    return max(20, int(os.getenv("V3_BACKTEST_WINDOW", "120")))


def _effective_backtest_window(n_bars: int) -> int:
    """Shrink the feature window when history is thin so labeling still produces rows."""
    margin = V3_LABEL_HOLD_BARS + 1
    min_rows = _min_train_rows()
    cap = max(20, int(n_bars) - margin - min_rows)
    return min(_backtest_window(), cap)


def _v3_ohlcv_period() -> str:
    """Default OHLCV lookback for training backtests (2y gives more labeled samples)."""
    return (os.getenv("V3_OHLCV_PERIOD", "2y") or "2y").strip()


def _v3_ohlcv_fetch_retries() -> int:
    return max(1, int(os.getenv("V3_OHLCV_FETCH_RETRIES", "3")))


def _v3_train_symbol_delay_sec() -> float:
    """Pause between symbols during batch train to avoid Alpaca rate-limit empty responses."""
    return max(0.0, float(os.getenv("V3_TRAIN_SYMBOL_DELAY_SEC", "0.35")))


def _v3_watchlist_peer_pool_enabled() -> bool:
    return (os.getenv("V3_WATCHLIST_PEER_POOL", "on") or "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


def _v3_watchlist_peer_pool_max() -> int:
    return max(0, int(os.getenv("V3_WATCHLIST_PEER_POOL_MAX", "12")))


_OHLCV_CACHE: dict = {}


def clear_ohlcv_cache() -> None:
    """Drop in-memory OHLCV cache (call at start of each batch train)."""
    global _OHLCV_CACHE
    _OHLCV_CACHE = {}

MODEL_DB_KEY = "ghost_v3_model_pkl"
FEATURES_DB_KEY = "ghost_v3_features_json"


def _v3_min_holdout_acc() -> float:
    return float(os.getenv("V3_MIN_HOLDOUT_ACC", "0.55"))


def _v3_min_edge() -> float:
    return float(os.getenv("V3_MIN_EDGE", "0.05"))


def _v3_min_wf_edge() -> float:
    """Walk-forward edge floor (can be slightly negative for thin watchlist names)."""
    return float(os.getenv("V3_MIN_WF_EDGE", "-0.05"))


def _v3_min_win_proba() -> float:
    return float(os.getenv("V3_MIN_WIN_PROBA", "0.55"))


def _v3_min_tp_sl_wins() -> int:
    return max(5, int(os.getenv("V3_MIN_TP_SL_WINS", "15")))


def _v3_min_wf_folds() -> int:
    return max(2, int(os.getenv("V3_MIN_WF_FOLDS", "3")))


def _v3_min_wf_acc_mean() -> float:
    return float(os.getenv("V3_MIN_WF_ACC_MEAN", "0.60"))


def _v3_wf_acc_min_slack() -> float:
    return float(os.getenv("V3_WF_ACC_MIN_SLACK", "0.05"))


def _v3_split_train_frac() -> float:
    return float(os.getenv("V3_SPLIT_TRAIN", "0.70"))


def _v3_split_calib_frac() -> float:
    return float(os.getenv("V3_SPLIT_CALIB", "0.15"))


def _v3_holdout_slices(n: int) -> tuple:
    """Time-ordered train | calib | gate index bounds (calib and gate never overlap).

    Default 70/15/15. Calibration fits on the middle slice; promotion gates use
    only the final slice so holdout accuracy is not reused for Platt/isotonic.
    """
    n = int(n)
    if n < 3:
        return 1, max(1, n - 1)
    train_end = max(1, int(n * _v3_split_train_frac()))
    calib_end = max(train_end + 1, int(n * (_v3_split_train_frac() + _v3_split_calib_frac())))
    if calib_end >= n:
        calib_end = n - 1
    if calib_end <= train_end:
        calib_end = min(n - 1, train_end + 1)
    return train_end, calib_end


def _v3_wf_acc_min_overrides() -> dict:
    """
    Optional per-symbol absolute floor overrides for wf_acc_min.
    Env format: V3_WF_ACC_MIN_OVERRIDES="WOLF=0.55"
    """
    raw = (os.getenv("V3_WF_ACC_MIN_OVERRIDES", "") or "").strip()
    out = {}
    if not raw:
        return out
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        sym = (k or "").strip().upper()
        if not sym:
            continue
        try:
            out[sym] = float(v.strip())
        except Exception:
            continue
    return out


def _v3_holdout_acc_overrides() -> dict:
    """Optional per-symbol holdout accuracy floors (thin gate slices).

    Env format: V3_HOLDOUT_ACC_OVERRIDES=\"TLRY=0.47,SPCE=0.52\"
    """
    raw = (os.getenv("V3_HOLDOUT_ACC_OVERRIDES", "") or "").strip()
    out = {}
    if not raw:
        return out
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        sym = (k or "").strip().upper()
        if not sym:
            continue
        try:
            out[sym] = float(v.strip())
        except Exception:
            continue
    return out


def _v3_wf_min_train_floor() -> int:
    """Absolute minimum training-window size for walk-forward folds.

    Was hardcoded to 120, which produced ZERO folds on WOLF's
    post-restructure dataset (127 samples → 120 train + 20 test
    overshoots n). Env-tunable so small-dataset tickers can run WF;
    larger defaults can be set back on production once WOLF accumulates
    more history.
    """
    return max(20, int(os.getenv("V3_WF_MIN_TRAIN", "60")))


def _v3_wf_min_train_frac() -> float:
    """Floor as fraction of total samples (was 0.50)."""
    try:
        return float(os.getenv("V3_WF_MIN_TRAIN_FRAC", "0.40"))
    except Exception:
        return 0.40


def _v3_wf_test_size_floor() -> int:
    """Absolute minimum per-fold test-window size (was hardcoded to 20)."""
    return max(5, int(os.getenv("V3_WF_TEST_SIZE", "15")))


def _v3_wf_test_size_frac() -> float:
    """Test-window size as fraction of total samples (was 0.10, unchanged)."""
    try:
        return float(os.getenv("V3_WF_TEST_FRAC", "0.10"))
    except Exception:
        return 0.10


def _v3_calibration_enabled() -> bool:
    """Whether to wrap the trained model with probability calibration.

    Calibration turns raw XGBoost predict_proba into a true win-probability so
    the V3_MIN_WIN_PROBA firing threshold and the displayed confidence track
    the realized win-rate. On by default; set V3_CALIBRATION=off to disable.
    """
    return (os.getenv("V3_CALIBRATION", "on") or "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


def _v3_calibration_method() -> str:
    """Calibration map: isotonic | sigmoid | auto.

    auto picks sigmoid (Platt) for small calibration sets and isotonic once
    there is enough data for a stable non-parametric fit — isotonic overfits
    on the ~25-sample WOLF holdout, sigmoid does not.
    """
    return (os.getenv("V3_CALIBRATION_METHOD", "auto") or "auto").strip().lower()


def _v3_pool_training_enabled() -> bool:
    """Whether to pool peer/sector samples into the model's training set (W1).

    WOLF's post-Ch.11 history yields only ~127 labeled samples — too few for a
    stable model. Pooling labeled samples from sector peers multiplies the
    training data; all quality gates still judge the model on WOLF's own
    holdout. Enabling pooling also price-normalizes macd_hist (the one raw
    price-unit feature) so it is comparable across tickers. On by default; set
    V3_POOL_TRAINING=off for the prior WOLF-only behavior.
    """
    return (os.getenv("V3_POOL_TRAINING", "on") or "on").strip().lower() not in (
        "0", "off", "false", "no",
    )


def _v3_peer_symbols() -> list:
    """Sector-peer tickers pooled into training (W1).

    Default basket = liquid US-listed SiC / power-semiconductor names in WOLF's
    sector. Peers that fail to fetch or have too few bars are skipped at
    collection, so a stale default ticker is harmless. Override via
    PEER_SYMBOLS="ON,STM,POWI".
    """
    raw = (os.getenv("PEER_SYMBOLS", "ON,STM,POWI,NVTS,ALGM,MPWR,MCHP,AOSL") or "").strip()
    out, seen = [], set()
    for part in raw.split(","):
        sym = part.strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def _v3_wolf_sample_weight() -> float:
    """Training weight on the target's own rows relative to peer rows (W1).

    Peers broaden the fit but the model must still specialize to WOLF, so WOLF
    samples are up-weighted. Default 3.0; set V3_WOLF_SAMPLE_WEIGHT to tune.
    """
    try:
        return max(1.0, float(os.getenv("V3_WOLF_SAMPLE_WEIGHT", "3.0")))
    except Exception:
        return 3.0


def _v3_sector_feature_enabled() -> bool:
    """Whether to add the sector relative-strength feature to the model (W3).

    Off by default: it changes the model's feature vector and needs offline
    validation before it can be trusted, so the operator opts in via
    V3_SECTOR_FEATURE=on and re-validates. When off the column isn't added at
    all, so model shape and behavior are unchanged.
    """
    return (os.getenv("V3_SECTOR_FEATURE", "off") or "off").strip().lower() in (
        "1", "on", "true", "yes",
    )


def _v3_sector_proxy() -> str:
    """Ticker whose price series stands in for the sector (W3 relative strength)."""
    return (os.getenv("SECTOR_PROXY", "SMH") or "SMH").strip().upper()


def _v3_sector_lookback() -> int:
    """Bars over which sector relative strength is measured (W3)."""
    return max(2, int(os.getenv("V3_SECTOR_LOOKBACK", "20")))


def _v3_ensemble_enabled() -> bool:
    """Whether to blend a second model with XGBoost (W5).

    Off by default: it changes the persisted model and must be validated before
    it's trusted. V3_ENSEMBLE=on enables a soft-voting blend of XGBoost with a
    RandomForest, each individually probability-calibrated.
    """
    return (os.getenv("V3_ENSEMBLE", "off") or "off").strip().lower() in (
        "1", "on", "true", "yes",
    )


def _v3_prune_features() -> set:
    """Feature columns to drop from the model (W5 feature pruning).

    Operator-driven from the attribution view: set V3_PRUNE_FEATURES to a comma
    list of column names that don't separate winners from losers. Empty by
    default. Skew-safe without a schema bump because prediction reads the trained
    column set back from model meta.
    """
    raw = (os.getenv("V3_PRUNE_FEATURES", "") or "").strip()
    return {p.strip() for p in raw.split(",") if p.strip()}


def _active_feature_cols() -> list:
    """Feature columns the model trains and predicts on.

    Appends the W3 sector relative-strength column only when V3_SECTOR_FEATURE
    is on, and drops any V3_PRUNE_FEATURES columns (W5). Toggling the sector
    column changes feature_schema (forcing a retrain); pruning is handled via
    the persisted meta feature_cols, so training persists this list and
    prediction reads it back — the two never disagree on the column set.
    """
    prune = _v3_prune_features()
    cols = [c for c in FEATURE_COLS if c not in prune]
    if _v3_sector_feature_enabled() and "sector_rel_strength" not in prune:
        cols.append("sector_rel_strength")
    return cols


class _ProbaEnsemble:
    """Soft-voting blend of fitted classifiers — averages predict_proba (W5).

    Top-level (picklable) so it persists exactly like a bare model, and exposes
    the small slice of the sklearn classifier surface that predict_live_ex and
    the calibration wrapper rely on (classes_, predict_proba, predict). Members
    must all order their classes the same way ([0, 1] here).
    """

    def __init__(self, models, weights=None):
        self.models = models
        self.weights = weights or [1.0] * len(models)
        self.classes_ = getattr(models[0], "classes_", np.array([0, 1]))

    def predict_proba(self, X):
        w = np.array(self.weights, dtype=float)
        w = w / w.sum()
        return sum(wi * m.predict_proba(X) for wi, m in zip(w, self.models))

    def predict(self, X):
        return self.classes_[np.argmax(self.predict_proba(X), axis=1)]


def _v3_feature_schema() -> str:
    """Version tag for the feature vector's *semantics* (not just its columns).

    load_model rejects any persisted model whose feature_schema differs from the
    current one, forcing a retrain. This prevents train/serve skew when a
    feature's scale/meaning changes — e.g. the macd_hist price-normalization
    that flips with V3_POOL_TRAINING, or the W3 sector column toggling. A model
    trained under one schema must not be served features computed under another.
    """
    macd = "macd_pct_v1" if _v3_pool_training_enabled() else "macd_raw_v0"
    sector = "sec1" if _v3_sector_feature_enabled() else "sec0"
    audit = "fa1" if _v3_feature_audit_enabled() else "fa0"
    return f"{macd}+{sector}+{audit}"


def _v3_feature_audit_enabled() -> bool:
    from core.feature_audit import _v3_feature_audit_enabled as _enabled
    return _enabled()


def _v3_wf_purge() -> int:
    """Bars to purge between each walk-forward train block and its test block.

    The triple-barrier label looks ahead V3_LABEL_HOLD_BARS bars, so the last
    few training samples before a test block carry outcomes that resolve inside
    the test period — naive walk-forward leaks that future into training. Purging
    hold_bars samples at the boundary removes the overlap (López de Prado purged
    CV). Defaults to the label horizon; set V3_WF_PURGE=0 to disable.
    """
    return max(0, int(os.getenv("V3_WF_PURGE", str(V3_LABEL_HOLD_BARS))))


def _wf_fold_bounds(n, min_train, test_size, step, purge,
                    min_train_floor, test_size_floor):
    """Index bounds for purged, expanding-window walk-forward folds.

    Returns a list of (train_end, test_start, test_end). The train block is
    X[:train_end] with train_end = test_start - purge, leaving a purge-bar gap
    so look-ahead labels can't leak across the boundary. Pure/index-only so it
    is unit-testable without xgboost/sklearn installed.
    """
    bounds = []
    start = min_train + purge          # so the first train block still ~min_train
    while start + test_size <= n:
        train_end = start - purge
        test_start, test_end = start, start + test_size
        if train_end >= min_train_floor and (test_end - test_start) >= test_size_floor:
            bounds.append((train_end, test_start, test_end))
        start += step
    return bounds


def _walk_forward_scores(X, y, X_peer=None, y_peer=None, wolf_weight=1.0):
    """
    Rolling walk-forward validation over time-ordered samples.
    Returns dict with fold_count / mean and minimum fold scores.

    W1: when peer samples (X_peer/y_peer) are supplied, each fold pools them
    into its TRAINING set (up-weighting the target via wolf_weight) while the
    TEST window stays target-only. This makes the walk-forward validate the
    same peer-pooled model that actually ships, and gives each thin target fold
    far more training data — instead of validating a WOLF-only model the engine
    never deploys. With X_peer=None it behaves exactly as the WOLF-only version.

    PR #21: train-window and test-window floors are env-tunable so the
    function actually produces folds for small datasets. WOLF's 127
    post-restructure samples couldn't generate folds with the prior
    hardcoded floors (120 / 20).

    W4: a purge gap of V3_WF_PURGE bars (default = label horizon) sits between
    each train block and its test block so look-ahead labels can't leak across
    the boundary (purged walk-forward).

    Example fold layout for n=127 with defaults (60 / 15, purge=3):
      fold 1: train=X[:60],  test=X[63:78]
      fold 2: train=X[:75],  test=X[78:93]
      fold 3: train=X[:90],  test=X[93:108]
      fold 4: train=X[:105], test=X[108:123]
    """
    from xgboost import XGBClassifier
    from sklearn.metrics import accuracy_score

    n = len(X)
    min_train_floor = _v3_wf_min_train_floor()
    min_train_frac = _v3_wf_min_train_frac()
    test_size_floor = _v3_wf_test_size_floor()
    test_size_frac = _v3_wf_test_size_frac()
    purge = _v3_wf_purge()
    # Thin tickers (e.g. recent IPOs) cannot satisfy default WF floors — scale down.
    if n < min_train_floor + test_size_floor + purge + 5:
        min_train_floor = max(20, int(n * 0.45))
        test_size_floor = max(5, min(test_size_floor, int(n * 0.18)))
    min_train = max(min_train_floor, int(n * min_train_frac))
    test_size = max(test_size_floor, int(n * test_size_frac))
    step = test_size
    bounds = _wf_fold_bounds(n, min_train, test_size, step, purge,
                             min_train_floor, test_size_floor)
    has_peers = X_peer is not None and len(X_peer) > 0
    folds = []
    for train_end, test_start, test_end in bounds:
        X_train, y_train = X[:train_end], y[:train_end]
        X_test, y_test = X[test_start:test_end], y[test_start:test_end]
        sample_weight = None
        if has_peers:
            sample_weight = np.concatenate([
                np.full(train_end, float(wolf_weight)), np.ones(len(X_peer))
            ])
            X_train = np.vstack([X_train, X_peer])
            y_train = np.concatenate([y_train, y_peer])
        pos_ct = int(np.sum(y_train))
        neg_ct = int(len(y_train) - pos_ct)
        if pos_ct <= 0:
            continue
        natural_rate = float(np.mean(y_test))
        model = XGBClassifier(
            n_estimators=200,
            max_depth=4,
            learning_rate=0.03,
            subsample=0.8,
            colsample_bytree=0.7,
            min_child_weight=3,
            scale_pos_weight=min(25.0, max(1.0, float(neg_ct / pos_ct))),
            eval_metric="logloss",
            random_state=42,
        )
        model.fit(X_train, y_train, sample_weight=sample_weight)
        acc = float(accuracy_score(y_test, model.predict(X_test)))
        folds.append({"acc": acc, "nat": natural_rate, "edge": acc - natural_rate})

    if not folds:
        return {"fold_count": 0, "acc_mean": 0.0, "acc_min": 0.0, "edge_mean": 0.0, "edge_min": 0.0}

    return {
        "fold_count": len(folds),
        "acc_mean": float(np.mean([f["acc"] for f in folds])),
        "acc_min": float(np.min([f["acc"] for f in folds])),
        "edge_mean": float(np.mean([f["edge"] for f in folds])),
        "edge_min": float(np.min([f["edge"] for f in folds])),
    }


def _simulate_up_tp_sl(rows: list, entry_idx: int, hold_bars: int, vol_pct: float) -> str:
    """Path simulation on daily OHLC — delegates to shared tp_sl_resolve (Phase 5)."""
    from core.tp_sl_resolve import simulate_tp_sl_label
    return simulate_tp_sl_label(rows, entry_idx, hold_bars, vol_pct, "UP")

def _rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0: return 100.0
    return float(100 - (100 / (1 + avg_gain / avg_loss)))

def _macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal: return 0.0, 0.0, 0.0
    def ema(data, n):
        k = 2/(n+1); r = [data[0]]
        for v in data[1:]: r.append(v*k + r[-1]*(1-k))
        return np.array(r)
    ml = ema(closes, fast) - ema(closes, slow)
    if len(ml) < signal: return 0.0, 0.0, 0.0
    sl = ema(ml, signal)
    return float(ml[-1]), float(sl[-1]), float(ml[-1] - sl[-1])

def _bollinger(closes, period=20):
    if len(closes) < period: return 0.5, 0.0
    w = closes[-period:]; mid = np.mean(w); std = np.std(w)
    if std == 0: return 0.5, 0.0
    upper = mid + 2*std; lower = mid - 2*std
    pct_b = float((closes[-1] - lower) / (upper - lower)) if (upper - lower) > 0 else 0.5
    return pct_b, float((upper - lower) / mid)

def _volume_ratio(volumes, period=20):
    if len(volumes) < period + 1: return 1.0
    avg = np.mean(volumes[-period-1:-1])
    return float(volumes[-1] / avg) if avg > 0 else 1.0

def _price_momentum(closes, periods=[1, 3, 5]):
    result = {}
    for p in periods:
        if len(closes) > p and closes[-p-1] > 0:
            result[f'mom_{p}h'] = float((closes[-1] - closes[-p-1]) / closes[-p-1])
        else:
            result[f'mom_{p}h'] = 0.0
    return result

def _ema(closes, period):
    if len(closes) < 2: return float(closes[-1])
    k = 2.0 / (period + 1); v = float(closes[0])
    for c in closes[1:]: v = c * k + v * (1 - k)
    return v

def _adx(highs, lows, closes, period=14):
    if len(closes) < period * 2: return 25.0
    trs, pdms, ndms = [], [], []
    for i in range(1, len(closes)):
        h, l, pc = highs[i], lows[i], closes[i-1]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
        ph = highs[i] - highs[i-1]; nl = lows[i-1] - lows[i]
        pdms.append(max(ph, 0) if ph > nl else 0)
        ndms.append(max(nl, 0) if nl > ph else 0)
    def wilder(data, p):
        s = sum(data[:p]); r = [s]
        for v in data[p:]: s = s - s/p + v; r.append(s)
        return r
    dxs = []
    for a, p, n in zip(wilder(trs, period), wilder(pdms, period), wilder(ndms, period)):
        if a == 0: continue
        pdi, ndi = 100*p/a, 100*n/a
        if pdi + ndi == 0: continue
        dxs.append(100 * abs(pdi - ndi) / (pdi + ndi))
    return float(np.mean(dxs[-period:])) if dxs else 25.0

def _atr(highs, lows, closes, period=14):
    if len(closes) < period + 1: return float(closes[-1] * 0.02)
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, len(closes))]
    return float(np.mean(trs[-period:]))

def _obv_slope(closes, volumes, period=10):
    if len(closes) < period + 1: return 0.0
    obv = 0.0; obvs = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]: obv += volumes[i]
        elif closes[i] < closes[i-1]: obv -= volumes[i]
        obvs.append(obv)
    r = obvs[-period:]
    if len(r) < 2: return 0.0
    slope = (r[-1] - r[0]) / (len(r) * max(abs(r[0]), 1e-9))
    return float(np.clip(slope, -1.0, 1.0))

def _stochastic(highs, lows, closes, k_period=14, d_period=3):
    if len(closes) < k_period: return 50.0, 50.0
    ks = []
    for i in range(k_period-1, len(closes)):
        hh = max(highs[i-k_period+1:i+1]); ll = min(lows[i-k_period+1:i+1])
        ks.append(100*(closes[i]-ll)/(hh-ll) if hh != ll else 50.0)
    k = ks[-1]
    d = float(np.mean(ks[-d_period:])) if len(ks) >= d_period else k
    return float(k), float(d)

def _calculate_features(df):
    closes = np.array([c['close'] for c in df], dtype=float)
    volumes = np.array([c['volume'] for c in df], dtype=float)
    highs = np.array([c['high'] for c in df], dtype=float)
    lows = np.array([c['low'] for c in df], dtype=float)

    rsi = _rsi(closes)
    macd_line, macd_sig, macd_hist = _macd(closes)
    pct_b, band_width = _bollinger(closes)
    vol_ratio = _volume_ratio(volumes)
    momentum = _price_momentum(closes)
    rh = np.max(highs[-24:]) if len(highs) >= 24 else highs[-1]
    rl = np.min(lows[-24:]) if len(lows) >= 24 else lows[-1]
    price_in_range = float((closes[-1] - rl) / (rh - rl + 1e-9))

    import datetime as _dt
    ts = df[-1].get('ts','') if df else ''
    try:
        _d = _dt.datetime.fromisoformat(str(ts).replace('Z','+00:00'))
        hod, dow = _d.hour, _d.weekday()
    except Exception:
        hod, dow = 12, 0

    cur = float(closes[-1])
    n = len(closes)
    ema20 = _ema(closes, 20)
    # Young-ticker EMA fallback. With too little history for the longer EMAs,
    # fall back to the longest EMA that IS valid (NOT `cur`). The old `else cur`
    # made above_emaX = (cur>cur) = 0 and ema_trend_bullish = 0 *permanently*,
    # which kept the BUY-only regime gate in WATCHING for new tickers like
    # post-Ch.11 WOLF (~168 trading days < 200). Applied inside _calculate_features
    # so train and serve stay consistent (no skew).
    ema50 = _ema(closes, 50) if n >= 50 else ema20
    ema200 = _ema(closes, 200) if n >= 200 else ema50
    # Long-trend flags degrade gracefully:
    #   n >= 200 -> full 20>50>200 stack
    #   50 <= n < 200 -> valid 20>50 stack (ema200 is a fallback, so the strict
    #                    ema50>ema200 self-comparison would wrongly read 0)
    #   20 <= n < 50 -> price-vs-ema20 (only ema20 is a true EMA)
    #   n < 20 -> neutral (1): not enough history to judge; don't block BUYs
    if n < 20:
        above_ema200_flag = 1
        ema_trend_bullish = 1
    else:
        above_ema200_flag = 1 if cur > ema200 else 0
        if n >= 200:
            ema_trend_bullish = 1 if (ema20 > ema50 and ema50 > ema200) else 0
        elif n >= 50:
            ema_trend_bullish = 1 if (cur > ema20 and ema20 > ema50) else 0
        else:
            ema_trend_bullish = 1 if cur > ema20 else 0
    adx = _adx(highs, lows, closes)
    atr = _atr(highs, lows, closes)
    obv_slope = _obv_slope(closes, volumes)
    stoch_k, stoch_d = _stochastic(highs, lows, closes)

    # macd_hist is in raw price units, so its scale tracks the share price. For
    # cross-ticker pooling (W1) that makes a $5 stock incomparable to a $200
    # one, so express it as a fraction of price. Read at runtime in both the
    # train and live paths, so the two stay consistent regardless of the flag.
    macd_hist_feat = (macd_hist / cur) if (_v3_pool_training_enabled() and cur > 0) else macd_hist

    return {
        'rsi': rsi,
        'rsi_oversold': 1 if rsi < 35 else 0,
        'rsi_overbought': 1 if rsi > 65 else 0,
        'macd_hist': macd_hist_feat,
        'macd_bullish': 1 if macd_hist > 0 else 0,
        'pct_b': pct_b,
        'bb_squeeze': 1 if band_width < 0.05 else 0,
        'volume_ratio': min(vol_ratio, 5.0),
        'volume_spike': 1 if vol_ratio > 1.5 else 0,
        'mom_4h': momentum['mom_1h'],
        'mom_8h': momentum['mom_3h'],
        'mom_24h': momentum['mom_5h'],
        'price_in_range': price_in_range,
        'near_low': 1 if price_in_range < 0.25 else 0,
        'near_high': 1 if price_in_range > 0.75 else 0,
        'hour_of_day': hod,
        'day_of_week': dow,
        'is_weekend': 1 if dow >= 5 else 0,
        'above_ema20': 1 if cur > ema20 else 0,
        'above_ema50': 1 if cur > ema50 else 0,
        'above_ema200': above_ema200_flag,
        'ema_trend_bullish': ema_trend_bullish,
        'ema20_vs_ema50': float((ema20 - ema50) / ema50) if ema50 > 0 else 0.0,
        'adx': adx,
        'adx_trending': 1 if adx > 20 else 0,
        'adx_strong': 1 if adx > 30 else 0,
        'atr_pct': float(atr / cur) if cur > 0 else 0.02,
        'obv_slope': obv_slope,
        'obv_accumulating': 1 if obv_slope > 0 else 0,
        'stoch_k': stoch_k,
        'stoch_d': stoch_d,
        'stoch_oversold': 1 if stoch_k < 20 else 0,
        'stoch_overbought': 1 if stoch_k > 80 else 0,
    }

FEATURE_COLS = [
    'rsi','rsi_oversold','rsi_overbought','macd_hist','macd_bullish',
    'pct_b','bb_squeeze','volume_ratio','volume_spike',
    'mom_4h','mom_8h','mom_24h','price_in_range','near_low','near_high',
    'hour_of_day','day_of_week','is_weekend',
    'above_ema20','above_ema50','above_ema200','ema_trend_bullish','ema20_vs_ema50',
    'adx','adx_trending','adx_strong',
    'atr_pct',
    'obv_slope','obv_accumulating',
    'stoch_k','stoch_d','stoch_oversold','stoch_overbought',
]


def _date_key(ts) -> str:
    """Date portion (YYYY-MM-DD) of an ISO/Alpaca timestamp, used for alignment."""
    return str(ts or "")[:10]


def _align_sector_closes(target_rows, sector_rows):
    """Sector closes aligned 1:1 to target_rows by date (W3).

    For each target bar, take the sector close on the same date; if the sector
    has no bar that day (holiday/feed mismatch), forward-fill the most recent
    *prior* sector close. Returns a list parallel to target_rows (None before
    any sector data exists). Only same-or-earlier sector bars are ever used, so
    there is no look-ahead. Assumes both series are in ascending date order
    (as the feed returns them). Pure / unit-testable.
    """
    by_date = {}
    for r in sector_rows or []:
        by_date[_date_key(r.get("ts"))] = float(r.get("close", 0.0))
    sector_sorted = sorted(by_date.items())
    out, last, si = [], None, 0
    for tr in target_rows:
        d = _date_key(tr.get("ts"))
        while si < len(sector_sorted) and sector_sorted[si][0] <= d:
            last = sector_sorted[si][1]
            si += 1
        out.append(by_date.get(d, last))
    return out


def _sector_rel_at(target_rows, aligned_sector, i, lookback):
    """Point-in-time sector relative strength at bar i (W3).

    target trailing return over `lookback` bars minus the sector's, using only
    bars at or before i. Returns 0.0 when there isn't enough history or the
    aligned sector close is missing — a neutral value, never a guess from the
    future.
    """
    if i < lookback:
        return 0.0
    t_past = float(target_rows[i - lookback]["close"])
    t_cur = float(target_rows[i]["close"])
    s_past = aligned_sector[i - lookback]
    s_cur = aligned_sector[i]
    if s_past is None or s_cur is None or t_past <= 0 or s_past <= 0:
        return 0.0
    return float((t_cur - t_past) / t_past - (s_cur - s_past) / s_past)


def _fetch_sector_series(period='1y'):
    """Sector proxy OHLCV for the W3 relative-strength feature (best-effort)."""
    try:
        return _fetch_ohlcv(_v3_sector_proxy(), "stock", period=period) or []
    except Exception as e:
        LOGGER.info(f"sector series fetch failed: {str(e)[:80]}")
        return []


def _fetch_ohlcv_once(symbol, asset_type, period='1y', interval='1d'):
    """Fetch daily OHLCV bars from Alpaca.

    PR #14 diag: emits "_fetch_ohlcv ENTERED" at the top so we can confirm
    in Railway logs whether the function is being called at all. If you see
    "PR14_DIAG MODULE_LOADED" but not "_fetch_ohlcv ENTERED" during training,
    something upstream is short-circuiting before reaching this function.

    Feed selection: tries SIP first (consolidated tape, the only feed that
    carries post-restructuring WOLF shares since 2025-09-29), falls back to
    IEX if SIP returns no rows or the account isn't entitled to SIP.

    Default lookback is 1 year. WOLF emerged from Chapter 11 with new shares
    trading from 2025-09-29, so >1y of data doesn't exist for the new ticker;
    fetching 2y would waste a round-trip and could confuse downstream logic.
    """
    LOGGER.info(f"[_fetch_ohlcv] PR14_DIAG ENTERED symbol={symbol} asset_type={asset_type} period={period}")
    import os, requests as _req
    from datetime import datetime, timedelta, timezone
    key = os.getenv("ALPACA_KEY_ID", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        return None
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    days_map = {'3m': 90, '6m': 180, '1y': 365, '2y': 730}
    lookback_days = days_map.get(period, 365)
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)
    start_str = start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    end_str = end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    def _try_feed(feed):
        try:
            url = (f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/bars"
                   f"?timeframe=1Day&limit=1000&feed={feed}"
                   f"&start={start_str}&end={end_str}")
            r = _req.get(url, headers=headers, timeout=30)
            if r.status_code != 200:
                LOGGER.info(f"Alpaca feed={feed} {symbol}: HTTP {r.status_code}")
                return None
            bars = r.json().get('bars', [])
            rows = [{'ts': b.get('t', ''), 'open': float(b.get('o', 0)),
                     'high': float(b.get('h', 0)), 'low': float(b.get('l', 0)),
                     'close': float(b.get('c', 0)), 'volume': float(b.get('v', 0))}
                    for b in bars if b.get('c', 0) > 0]
            return rows if rows else None
        except Exception as e:
            LOGGER.warning(f"Alpaca feed={feed} {symbol}: {e}")
            return None

    rows = _try_feed('sip')
    feed_used = 'sip'
    if not rows:
        LOGGER.info(f"Alpaca SIP returned nothing for {symbol}, trying IEX fallback")
        rows = _try_feed('iex')
        feed_used = 'iex'
    if not rows:
        # Polygon third tier — paid feed, used by scripts/wolf_backtest.py
        # for the same reason: covers post-restructure WOLF where Alpaca's
        # tiers don't. Requires POLYGON_API_KEY env; the helper logs at every
        # branch (missing key, HTTP error, non-OK status, empty results,
        # parse failure, or success) so ops can tell exactly what happened.
        LOGGER.info(f"Alpaca IEX returned nothing for {symbol}, trying Polygon fallback")
        rows = _try_polygon_ohlcv(symbol, period)
        feed_used = 'polygon'
    if not rows:
        # yfinance fourth tier — no API key, broad coverage. Post-restructure
        # WOLF can trip Yahoo's 'delisted' code path on long periods, so
        # _try_yfinance_ohlcv tries progressively shorter periods + explicit
        # date ranges before giving up.
        LOGGER.info(f"Polygon returned nothing for {symbol}, trying yfinance fallback")
        rows = _try_yfinance_ohlcv(symbol, period)
        feed_used = 'yfinance'
    if not rows:
        # Stooq fifth tier — last-resort fallback. Free CSV endpoint, no API
        # key, no rate-limit issues, and a completely different data aggregator
        # from Alpaca/Polygon/Yahoo so it tends to work when the others don't
        # (e.g. for post-restructure tickers that Yahoo flags as delisted).
        LOGGER.info(f"yfinance returned nothing for {symbol}, trying Stooq fallback")
        rows = _try_stooq_ohlcv(symbol, period)
        feed_used = 'stooq'
    if rows:
        LOGGER.info(f"Daily {symbol}: {len(rows)} bars (feed={feed_used}, lookback={lookback_days}d)")
        return rows
    return None


def _sma5_from_daily_bars(daily_rows):
    """Mean close over the last up-to-5 daily bars."""
    if not daily_rows:
        return None
    closes = []
    for row in daily_rows:
        try:
            val = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if val > 0:
            closes.append(val)
    tail = closes[-5:] if len(closes) >= 5 else closes
    return (sum(tail) / len(tail)) if tail else None


def _block_up_below_sma5(symbol, asset_type, current_price):
    """Return (blocked, sma_5d, current_price) for counter-trend UP gate."""
    if os.getenv("V3_BLOCK_UP_BELOW_SMA5", "1").strip().lower() in ("0", "false", "no", "off"):
        return False, None, float(current_price or 0)
    cur = float(current_price or 0)
    if cur <= 0:
        return False, None, cur
    daily = _fetch_ohlcv(symbol, asset_type, period="1mo", interval="1d")
    sma = _sma5_from_daily_bars(daily)
    if sma is None or sma <= 0:
        return False, sma, cur
    return cur < sma, sma, cur


def _fetch_ohlcv(symbol, asset_type, period=None, interval='1d'):
    """Fetch daily OHLCV with in-run cache + retries on empty responses."""
    period = period or _v3_ohlcv_period()
    sym = (symbol or "").upper()
    atype = (asset_type or "stock").strip().lower()
    cache_key = (sym, atype, period)
    if cache_key in _OHLCV_CACHE:
        return _OHLCV_CACHE[cache_key]
    retries = _v3_ohlcv_fetch_retries()
    for attempt in range(retries):
        rows = _fetch_ohlcv_once(symbol, asset_type, period, interval)
        if rows:
            _OHLCV_CACHE[cache_key] = rows
            return rows
        if attempt + 1 < retries:
            delay = 0.5 * (2 ** attempt)
            LOGGER.info(
                f"_fetch_ohlcv {sym}: empty on attempt {attempt + 1}/{retries}, retry in {delay:.1f}s"
            )
            time.sleep(delay)
    return None


def _try_stooq_ohlcv(symbol, period):
    """Fetch daily OHLCV from stooq.com — fifth-tier fallback.

    Stooq serves a CSV directly with no API key. The URL pattern is:
      https://stooq.com/q/d/l/?s={symbol}.us&i=d

    Columns: Date,Open,High,Low,Close,Volume

    Every code path emits an INFO log (same pattern as PR #13 Polygon path)
    so ops can tell exactly what happened: HTTP error, empty body, parse
    failure, no rows in window, or success.
    """
    import requests as _req
    import csv as _csv
    import io as _io
    from datetime import datetime, timedelta, timezone

    days_map = {'3m': 90, '6m': 180, '1y': 365, '2y': 730}
    lookback_days = days_map.get(period, 365)
    cutoff_date = datetime.now(timezone.utc).date() - timedelta(days=lookback_days)
    LOGGER.info(f"Stooq {symbol}: requesting CSV cutoff>={cutoff_date} (lookback={lookback_days}d)")
    try:
        url = f"https://stooq.com/q/d/l/?s={symbol.lower()}.us&i=d"
        r = _req.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (ghost-protocol)"})
        if r.status_code != 200:
            LOGGER.info(f"Stooq {symbol}: HTTP {r.status_code}")
            return None
        text = (r.text or "").strip()
        if not text or "No data" in text or len(text) < 30:
            LOGGER.info(f"Stooq {symbol}: empty/no-data response (len={len(text)})")
            return None
        reader = _csv.DictReader(_io.StringIO(text))
        rows = []
        skipped_pre_cutoff = 0
        for row in reader:
            try:
                date_str = row.get("Date") or ""
                close = float(row.get("Close", 0) or 0)
                if close <= 0:
                    continue
                row_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if row_date < cutoff_date:
                    skipped_pre_cutoff += 1
                    continue
                rows.append({
                    "ts": row_date.strftime("%Y-%m-%dT00:00:00Z"),
                    "open": float(row.get("Open", 0) or 0),
                    "high": float(row.get("High", 0) or 0),
                    "low": float(row.get("Low", 0) or 0),
                    "close": close,
                    "volume": float(row.get("Volume", 0) or 0),
                })
            except Exception:
                continue
        if not rows:
            LOGGER.info(f"Stooq {symbol}: parsed 0 rows in window (skipped {skipped_pre_cutoff} pre-cutoff)")
            return None
        LOGGER.info(f"Stooq {symbol}: parsed {len(rows)} bars in window (skipped {skipped_pre_cutoff} pre-cutoff)")
        return rows
    except Exception as e:
        LOGGER.warning(f"Stooq {symbol}: {e}")
        return None


def _try_polygon_ohlcv(symbol, period):
    """Fetch daily OHLCV from Polygon REST. Same shape as Alpaca path.

    Requires POLYGON_API_KEY env. Endpoint:
      /v2/aggs/ticker/{SYM}/range/1/day/{from}/{to}?adjusted=true

    Every code path emits an INFO log so ops can tell exactly what happened
    (missing key, HTTP error, status != OK, no results, or success).
    """
    import os, requests as _req
    from datetime import datetime, timedelta, timezone
    api_key = os.getenv("POLYGON_API_KEY", "")
    if not api_key:
        LOGGER.info(f"Polygon {symbol}: POLYGON_API_KEY not set on this deployment, skipping")
        return None
    days_map = {'3m': 90, '6m': 180, '1y': 365, '2y': 730}
    lookback_days = days_map.get(period, 365)
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=lookback_days + 30)  # +30 for weekend/holiday buffer
    LOGGER.info(f"Polygon {symbol}: requesting bars {start_date}..{end_date} (lookback={lookback_days}d)")
    try:
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol.upper()}/range/1/day/"
            f"{start_date.isoformat()}/{end_date.isoformat()}"
            f"?adjusted=true&sort=asc&limit=5000&apiKey={api_key}"
        )
        r = _req.get(url, timeout=30)
        if r.status_code != 200:
            LOGGER.info(f"Polygon {symbol}: HTTP {r.status_code} body={r.text[:200]!r}")
            return None
        data = r.json()
        status = data.get("status")
        if status not in ("OK", "DELAYED"):
            LOGGER.info(f"Polygon {symbol}: status={status} body={str(data)[:200]!r}")
            return None
        results = data.get("results") or []
        if not results:
            LOGGER.info(f"Polygon {symbol}: status=OK but results=[] (no bars in range)")
            return None
        rows = []
        for bar in results:
            try:
                close = float(bar.get("c", 0))
                if close <= 0:
                    continue
                ts = datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                rows.append({
                    "ts": ts,
                    "open": float(bar.get("o", 0)),
                    "high": float(bar.get("h", 0)),
                    "low": float(bar.get("l", 0)),
                    "close": close,
                    "volume": float(bar.get("v", 0)),
                })
            except Exception:
                continue
        if not rows:
            LOGGER.info(f"Polygon {symbol}: {len(results)} raw bars returned but all failed parsing")
            return None
        LOGGER.info(f"Polygon {symbol}: parsed {len(rows)} bars from response")
        return rows
    except Exception as e:
        LOGGER.warning(f"Polygon {symbol}: {e}")
        return None


def _try_yfinance_ohlcv(symbol, period):
    """yfinance fallback with multi-strategy retry.

    For post-restructure tickers (Sept 2025 WOLF re-listing), Yahoo's symbol
    resolver flags the long-period request as 'delisted' even though the new
    shares are actively trading. Progressively shorter periods can succeed
    because they cover only the post-restructure data window.

    Strategy order:
      1. Requested period (e.g. '1y')
      2. '6mo' (skipped if primary was already shorter)
      3. '3mo' (skipped if primary was already shorter)
      4. Explicit start/end via tk.history(start=..., end=...) — ~240 days
         back, covers post-restructure WOLF era only

    Returns the standard row shape {ts, open, high, low, close, volume}
    or None if all strategies fail.
    """
    import datetime as _dt
    yf_period_primary = {'3m': '3mo', '6m': '6mo', '1y': '1y', '2y': '2y'}.get(period, '1y')
    period_candidates = [yf_period_primary]
    if yf_period_primary not in ('6mo', '3mo'):
        period_candidates.append('6mo')
    if yf_period_primary != '3mo':
        period_candidates.append('3mo')
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
        for p in period_candidates:
            rows = _yf_rows_from_history(tk, period=p)
            if rows:
                LOGGER.info(f"yfinance {symbol}: {len(rows)} bars (period={p})")
                return rows
        end = _dt.datetime.now(_dt.timezone.utc)
        start = end - _dt.timedelta(days=240)
        rows = _yf_rows_from_history(tk, start=start, end=end)
        if rows:
            LOGGER.info(f"yfinance {symbol}: {len(rows)} bars (start={start.date()})")
            return rows
        return None
    except Exception as e:
        LOGGER.warning(f"yfinance fallback {symbol}: {e}")
        return None


def _yf_rows_from_history(tk, period=None, start=None, end=None):
    """Run tk.history() with either a period or start/end pair; normalise
    empty DataFrame / exception to None, and the OHLCV DataFrame to the
    standard row shape consumed by backtest_symbol.
    """
    try:
        if period is not None:
            h = tk.history(period=period, interval='1d')
        else:
            h = tk.history(start=start, end=end, interval='1d')
        if h is None or getattr(h, "empty", False):
            return None
        rows = []
        for ix, row in h.iterrows():
            try:
                close = float(row["Close"])
                if close <= 0:
                    continue
                ts = ix.strftime('%Y-%m-%dT%H:%M:%SZ') if hasattr(ix, 'strftime') else str(ix)
                rows.append({
                    'ts': ts,
                    'open': float(row["Open"]),
                    'high': float(row["High"]),
                    'low': float(row["Low"]),
                    'close': close,
                    'volume': float(row.get("Volume", 0) or 0),
                })
            except Exception:
                continue
        return rows if rows else None
    except Exception:
        return None


def backtest_symbol(symbol, asset_type):
    rows = _fetch_ohlcv(symbol, asset_type)
    min_bars = _min_backtest_bars()
    if not rows or len(rows) < min_bars:
        return []
    vol_pct = base_vol_pct(symbol, asset_type)
    labeled = []
    window = _effective_backtest_window(len(rows))
    margin = V3_LABEL_HOLD_BARS + 1
    # W3: align a sector series to these bars once, then read it point-in-time
    # per bar below. Only computed when the feature is enabled.
    sector_on = _v3_sector_feature_enabled()
    aligned_sector = _align_sector_closes(rows, _fetch_sector_series()) if sector_on else None
    sector_lookback = _v3_sector_lookback()
    for i in range(window, len(rows) - margin):
        hist = rows[max(0, i - window) : i + 1]
        features = _calculate_features(hist)
        from core.feature_schema import attach_feature_asof
        attach_feature_asof(features, rows[i].get("ts"))
        features["symbol"] = symbol
        features["asset_type"] = asset_type
        if sector_on:
            features["sector_rel_strength"] = _sector_rel_at(rows, aligned_sector, i, sector_lookback)
        outcome = _simulate_up_tp_sl(rows, i, V3_LABEL_HOLD_BARS, vol_pct)
        labeled.append({"features": features, "label": 1 if outcome == "WIN" else 0, "outcome": outcome})
    wins = sum(1 for r in labeled if r["label"] == 1)
    LOGGER.info(
        f"Backtest {symbol}: {len(labeled)} samples (TP/SL labels, {V3_LABEL_HOLD_BARS}d bars), "
        f"{round(wins/len(labeled)*100,1) if labeled else 0}% natural WIN rate"
    )
    return labeled

def build_training_data(symbols_and_types):
    all_rows = []
    for symbol, asset_type in symbols_and_types:
        all_rows.extend(backtest_symbol(symbol, asset_type))
    if len(all_rows) < _min_train_rows(): return None, None, []
    active_cols = _active_feature_cols()
    X = np.array([[r['features'].get(c, 0.0) for c in active_cols] for r in all_rows])
    y = np.array([r['label'] for r in all_rows])
    return X, y, active_cols

def _persist_train_details(details_list) -> None:
    """Persist per-symbol training diagnostics to ghost_state.last_train_details.

    Lets the v3_train_sync endpoint surface gate-fail reasons in its response
    instead of forcing the operator to grep Railway logs for RETRAIN lines.
    """
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS ghost_state (key TEXT PRIMARY KEY, val TEXT)")
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('last_train_details', %s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (json.dumps({"ts": int(time.time()), "symbols": details_list}),),
            )
            # Model lineage (audit): append this run to a rolling history (last 50)
            # so /admin can show how accuracy/edge evolved across retrains.
            cur.execute("SELECT val FROM ghost_state WHERE key='model_lineage'")
            _row = cur.fetchone()
            _hist = []
            if _row and _row[0]:
                try:
                    _hist = json.loads(_row[0])
                except Exception:
                    _hist = []
            if not isinstance(_hist, list):
                _hist = []
            _hist.append({"ts": int(time.time()), "symbols": details_list})
            _hist = _hist[-50:]
            cur.execute(
                "INSERT INTO ghost_state(key,val) VALUES('model_lineage', %s) "
                "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
                (json.dumps(_hist),),
            )
    except Exception as _e:
        LOGGER.warning("train details persist failed: " + str(_e)[:120])


def _v3_max_calibration_brier() -> float:
    """Max acceptable Brier on the final holdout (gate) slice after calibration."""
    _PHASE5_FLOOR = 0.31  # tp_sl_fwd_v1 watchlist clears through ~0.305 on prod
    try:
        raw = os.getenv("V3_MAX_CALIBRATION_BRIER")
        val = float(raw) if raw not in (None, "") else _PHASE5_FLOOR
    except Exception:
        val = _PHASE5_FLOOR
    if val < _PHASE5_FLOOR:
        LOGGER.info(
            "V3_MAX_CALIBRATION_BRIER=%s below Phase 5 floor; using %s",
            val, _PHASE5_FLOOR,
        )
        return _PHASE5_FLOOR
    return val


def _reliability_bins(y_true, y_prob, n_bins: int = 5) -> List[Dict[str, Any]]:
    """Reliability diagram bins: predicted prob bucket vs realized win rate."""
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)
    if len(y_true) == 0:
        return []
    n_bins = max(2, min(int(n_bins), 10))
    bins: List[Dict[str, Any]] = []
    for i in range(n_bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        if i == n_bins - 1:
            mask = (y_prob >= lo) & (y_prob <= hi)
        else:
            mask = (y_prob >= lo) & (y_prob < hi)
        cnt = int(np.sum(mask))
        if cnt == 0:
            continue
        bins.append({
            "bin_lo": round(lo, 3),
            "bin_hi": round(hi, 3),
            "n": cnt,
            "mean_pred": round(float(np.mean(y_prob[mask])), 4),
            "observed_rate": round(float(np.mean(y_true[mask])), 4),
        })
    return bins


def _evaluate_calibration_holdout(model, X_gate, y_gate) -> Dict[str, Any]:
    """Evaluate the deployed (calibrated) model on the untouched gate slice."""
    from sklearn.metrics import accuracy_score, brier_score_loss

    y_gate = np.asarray(y_gate)
    if len(y_gate) == 0:
        return {
            "holdout_acc": 0.0,
            "edge": 0.0,
            "natural_rate": 0.0,
            "gate_brier": None,
            "reliability_bins": [],
            "gate_n": 0,
        }
    proba = model.predict_proba(X_gate)[:, 1]
    natural_rate = float(np.mean(y_gate))
    preds = (proba >= 0.5).astype(int)
    holdout_acc = float(accuracy_score(y_gate, preds))
    edge = holdout_acc - natural_rate
    gate_brier = None
    if np.unique(y_gate).size >= 2:
        gate_brier = round(float(brier_score_loss(y_gate, proba)), 4)
    return {
        "holdout_acc": holdout_acc,
        "edge": edge,
        "natural_rate": natural_rate,
        "gate_brier": gate_brier,
        "reliability_bins": _reliability_bins(y_gate, proba),
        "gate_n": int(len(y_gate)),
    }


def _maybe_calibrate(model, X_calib, y_calib):
    """Wrap a fitted base model with prefit probability calibration.

    The base model was fit on the training slice and never saw X_calib, so the
    held-out slice is a valid post-hoc calibration set (strictly time-ordered:
    it is the most recent ~20% of the series). Returns (final_model, info).

    Falls back to the raw model (info["calibrated"]=False) whenever calibration
    isn't viable — disabled, too few points, or a single-class calib slice — so
    training never breaks on this. Calibration quality itself is validated live
    via the confidence-bucket calibration curve, not offline here.
    """
    info = {"calibrated": False, "method": None, "n_calib": int(len(X_calib))}
    if not _v3_calibration_enabled():
        info["skip_reason"] = "disabled"
        return model, info
    if len(X_calib) < 10 or np.unique(y_calib).size < 2:
        info["skip_reason"] = "insufficient_calib_data"
        return model, info
    method = _v3_calibration_method()
    if method == "auto":
        method = "isotonic" if len(X_calib) >= 200 else "sigmoid"
    if method not in ("isotonic", "sigmoid"):
        method = "sigmoid"
    try:
        from sklearn.calibration import CalibratedClassifierCV
        calibrated = CalibratedClassifierCV(model, method=method, cv="prefit")
        calibrated.fit(X_calib, y_calib)
        info.update({"calibrated": True, "method": method})
        return calibrated, info
    except Exception as e:
        info["skip_reason"] = "exception: " + str(e)[:120]
        return model, info


def _build_ensemble(xgb_model, X_fit, y_fit, sample_weight, X_calib, y_calib):
    """Soft-voting blend of the fitted XGB model with a RandomForest (W5).

    Each component is individually probability-calibrated on the WOLF holdout,
    then their probabilities are averaged. Returns (model, calib_info) with the
    same calib_info shape _maybe_calibrate produces, plus ensemble metadata.
    Falls back to the calibrated single XGB model if anything goes wrong, so
    enabling the ensemble can never break a training run.
    """
    try:
        from sklearn.ensemble import RandomForestClassifier
        rf = RandomForestClassifier(
            n_estimators=300, max_depth=6, min_samples_leaf=3,
            class_weight="balanced", random_state=42,
        )
        rf.fit(X_fit, y_fit, sample_weight=sample_weight)
        cal_xgb, info_x = _maybe_calibrate(xgb_model, X_calib, y_calib)
        cal_rf, _info_r = _maybe_calibrate(rf, X_calib, y_calib)
        ens = _ProbaEnsemble([cal_xgb, cal_rf])
        info = {
            "calibrated": bool(info_x.get("calibrated", False)),
            "method": info_x.get("method"),
            "n_calib": int(len(X_calib)),
            "ensemble": True,
            "members": ["xgboost", "random_forest"],
        }
        return ens, info
    except Exception as e:
        final_model, info = _maybe_calibrate(xgb_model, X_calib, y_calib)
        info["ensemble"] = False
        info["ensemble_skip_reason"] = "exception: " + str(e)[:120]
        return final_model, info


def _assemble_pooled_training(
    X_train, y_train, peer_rows, feature_cols, wolf_weight, target_train_rows=None,
):
    """Stack the target's training slice with peer samples for pooled fitting (W1).

    Returns (X_pooled, y_pooled, sample_weight). Target rows keep wolf_weight;
    peer rows get regime-aware weights (Phase 3) so mismatched vol/momentum/%B
    peers contribute less. Pure/numpy so it's unit-testable without xgboost.
    """
    from core.feature_audit import peer_regime_weight, regime_profile

    wolf_sw = np.full(len(X_train), float(wolf_weight))
    if not peer_rows:
        return X_train, y_train, wolf_sw
    profile = regime_profile(target_train_rows or []) if target_train_rows else {}
    Xp = np.array([[r["features"].get(c, 0.0) for c in feature_cols] for r in peer_rows])
    yp = np.array([r["label"] for r in peer_rows])
    peer_sw = np.array([
        peer_regime_weight(profile, r.get("features") or {})
        for r in peer_rows
    ], dtype=float)
    X_pooled = np.vstack([X_train, Xp])
    y_pooled = np.concatenate([y_train, yp])
    sample_weight = np.concatenate([wolf_sw, peer_sw])
    return X_pooled, y_pooled, sample_weight


def _collect_peer_rows(target_symbol):
    """Labeled samples from sector peers for pooled training (W1).

    Each peer is labeled by the same triple-barrier backtest as the target,
    using the peer's own volatility, so labels are consistent across symbols.
    Peers that fail to fetch or have too few labeled rows are skipped. Returns
    (pooled_rows, peers_used) where peers_used is a per-peer sample-count list.
    """
    target = (target_symbol or "").upper()
    peers = [p for p in _v3_peer_symbols() if p != target]
    if _v3_watchlist_peer_pool_enabled():
        try:
            from config.symbols import watchlist_symbol_pairs
            cap = _v3_watchlist_peer_pool_max()
            added = 0
            for sym, _atype in watchlist_symbol_pairs(include_portfolio=False):
                sym = (sym or "").upper()
                if not sym or sym == target or sym in peers:
                    continue
                peers.append(sym)
                added += 1
                if cap and added >= cap:
                    break
        except Exception as e:
            LOGGER.info(f"pool: watchlist peers skipped ({str(e)[:80]})")
    pooled, used = [], []
    min_rows = _min_train_rows()
    for p in peers:
        try:
            rows = backtest_symbol(p, "stock")
        except Exception as e:
            LOGGER.info(f"pool: peer {p} skipped ({str(e)[:80]})")
            continue
        if rows and len(rows) >= min_rows:
            pooled.extend(rows)
            used.append({"symbol": p, "n": len(rows)})
        else:
            LOGGER.info(f"pool: peer {p} skipped (only {len(rows) if rows else 0} rows)")
    return pooled, used


def train_and_validate(symbols_and_types):
    try:
        from xgboost import XGBClassifier
        from sklearn.metrics import accuracy_score
        import pickle, base64
        from core.db import db_conn
    except ImportError as e:
        LOGGER.error("Missing dep: "+str(e)); return None, 0.0, False
    total_passed = 0
    details: list = []
    clear_ohlcv_cache()
    symbol_delay = _v3_train_symbol_delay_sec()
    for idx, (symbol, asset_type) in enumerate(symbols_and_types):
        symbol_detail = {"symbol": symbol, "asset_type": asset_type}
        try:
            rows = backtest_symbol(symbol, asset_type)
            n_samples = len(rows) if rows else 0
            min_rows = _min_train_rows()
            if not rows or n_samples < min_rows:
                LOGGER.info(f"RETRAIN [{symbol}]: first pass n={n_samples}, retrying after backoff")
                time.sleep(2.0)
                rows = backtest_symbol(symbol, asset_type)
                n_samples = len(rows) if rows else 0
            if not rows or n_samples < min_rows:
                fail_msg = f"n_samples<{min_rows} ({n_samples})"
                LOGGER.info(
                    f"RETRAIN [{symbol}]: acc=NA edge=NA wf_folds=NA wf_acc_mean=NA wf_edge_mean=NA wf_acc_min=NA "
                    f"| FAIL: {fail_msg}"
                )
                symbol_detail.update({
                    "passed": False, "fail_reason": fail_msg,
                    "n_samples": n_samples, "stage": "pre_train",
                })
                details.append(symbol_detail)
                continue
            active_cols = _active_feature_cols()
            wins_ct = int(np.sum([r["label"] for r in rows]))
            min_wins = _v3_min_tp_sl_wins()
            train_end, calib_end = _v3_holdout_slices(len(rows))
            feature_audit: List[Dict[str, Any]] = []
            invert_cols: set = set()
            if _v3_feature_audit_enabled() and (len(rows) - calib_end) >= 10:
                from core.feature_audit import (
                    apply_inversions_to_features,
                    audit_gate_features,
                    select_inverted_features,
                )
                gate_preview = rows[calib_end:]
                X_gate_preview = np.array([
                    [r["features"].get(c, 0.0) for c in active_cols] for r in gate_preview
                ])
                y_gate_preview = np.array([r["label"] for r in gate_preview])
                feature_audit = audit_gate_features(
                    X_gate_preview, y_gate_preview, active_cols,
                )
                invert_cols = select_inverted_features(feature_audit)
                if invert_cols:
                    for r in rows:
                        apply_inversions_to_features(r["features"], invert_cols)
            X = np.array([[r["features"].get(c, 0.0) for c in active_cols] for r in rows])
            y = np.array([r["label"] for r in rows])
            train_end, calib_end = _v3_holdout_slices(len(X))
            X_train, y_train = X[:train_end], y[:train_end]
            X_calib, y_calib = X[train_end:calib_end], y[train_end:calib_end]
            X_gate, y_gate = X[calib_end:], y[calib_end:]
            natural_rate = float(np.mean(y_gate)) if len(y_gate) else 0.0
            # W1: pool peer/sector samples into the FIT set to break the
            # small-data wall. Calib + gate slices stay target-only; calib fits
            # Platt/isotonic, gate drives holdout accuracy / edge promotion.
            peer_rows, peers_used = ([], [])
            if _v3_pool_training_enabled():
                peer_rows, peers_used = _collect_peer_rows(symbol)
            X_fit, y_fit, sample_weight = _assemble_pooled_training(
                X_train, y_train, peer_rows, active_cols, _v3_wolf_sample_weight(),
                target_train_rows=rows[:train_end],
            )
            pool_info = {
                "enabled": _v3_pool_training_enabled(),
                "peer_sample_count": int(len(peer_rows)),
                "peers": peers_used,
                "pooled_train_n": int(len(X_fit)),
                "wolf_train_n": int(len(X_train)),
                "wolf_sample_weight": _v3_wolf_sample_weight(),
            }
            pos_ct = int(np.sum(y_fit))
            neg_ct = int(len(y_fit) - pos_ct)
            spw = (neg_ct / pos_ct) if pos_ct > 0 else 1.0
            min_acc = _v3_min_holdout_acc()
            min_edge = _v3_min_edge()
            holdout_overrides = _v3_holdout_acc_overrides()
            symbol_min_acc = holdout_overrides.get(symbol.upper(), min_acc)
            model = XGBClassifier(
                n_estimators=200, max_depth=4, learning_rate=0.03,
                subsample=0.8, colsample_bytree=0.7, min_child_weight=3,
                scale_pos_weight=min(25.0, max(1.0, float(spw))),
                eval_metric='logloss', random_state=42
            )
            model.fit(X_fit, y_fit, sample_weight=sample_weight)
            # P3 (audit): stacking ensemble — 4 base models + LR meta when V3_ENSEMBLE=stacking
            if is_stacking_enabled():
                from core.stacking_ensemble import build_stacking_ensemble
                final_model, calib_info = build_stacking_ensemble(
                    X_fit, y_fit, X_calib, y_calib,
                    sample_weight=sample_weight,
                    feature_cols=active_cols,
                )
                if final_model is None:
                    # Fallback to calibrated single XGB
                    final_model, calib_info = _maybe_calibrate(model, X_calib, y_calib)
                    calib_info["ensemble"] = False
                    calib_info["ensemble_skip_reason"] = calib_info.get("skip_reason", "stacking_failed")
            elif _v3_ensemble_enabled():
                final_model, calib_info = _build_ensemble(
                    model, X_fit, y_fit, sample_weight, X_calib, y_calib)
            else:
                final_model, calib_info = _maybe_calibrate(model, X_calib, y_calib)
            holdout = _evaluate_calibration_holdout(final_model, X_gate, y_gate)
            accuracy = float(holdout["holdout_acc"])
            edge = float(holdout["edge"])
            natural_rate = float(holdout["natural_rate"])
            from core.feature_audit import reliability_bins_monotonic
            reliability_mono = reliability_bins_monotonic(
                holdout.get("reliability_bins") or [],
            )
            calib_info.update({
                "gate_brier": holdout.get("gate_brier"),
                "reliability_bins": holdout.get("reliability_bins") or [],
                "gate_n": holdout.get("gate_n", 0),
                "reliability_monotonic": reliability_mono,
            })
            # P7 (audit): conformal calibration from holdout predictions.
            # Stored in model meta and applied in predict_live_ex when present.
            try:
                from core.conformal_calibration import calibrate_conformal
                if len(X_gate) >= 10:
                    gate_probs = final_model.predict_proba(X_gate)[:, 1]
                    conformal = calibrate_conformal(gate_probs, y_gate)
                else:
                    conformal = {
                        "ok": False,
                        "error": f"Need >=10 gate samples, have {len(X_gate)}",
                        "samples": int(len(X_gate)),
                    }
            except Exception as _conf_e:
                conformal = {
                    "ok": False,
                    "error": str(_conf_e)[:120],
                    "samples": int(len(X_gate)),
                }
            calib_info["conformal"] = conformal
            # W1: feed the same peer pool into the walk-forward folds, so the
            # gate validates the deployed pooled model rather than a WOLF-only
            # one the engine never serves (and each thin fold trains on more).
            if peer_rows:
                X_peer_wf = np.array([[r["features"].get(c, 0.0) for c in active_cols] for r in peer_rows])
                y_peer_wf = np.array([r["label"] for r in peer_rows])
                wf = _walk_forward_scores(X, y, X_peer_wf, y_peer_wf, _v3_wolf_sample_weight())
            else:
                wf = _walk_forward_scores(X, y)
            min_wf_acc = _v3_min_wf_acc_mean()
            min_wf_folds = _v3_min_wf_folds()
            # Allow modest fold variance while keeping strict mean WF quality.
            wf_slack = _v3_wf_acc_min_slack()
            min_wf_acc_min = (min_wf_acc - wf_slack)
            symbol_overrides = _v3_wf_acc_min_overrides()
            symbol_wf_acc_min = symbol_overrides.get(symbol.upper(), min_wf_acc_min)
            max_brier = _v3_max_calibration_brier()
            min_wf_edge = _v3_min_wf_edge()
            gate_checks = [
                ("n_samples", n_samples >= min_rows, f"n_samples<{min_rows} ({n_samples})"),
                ("tp_sl_wins", wins_ct >= min_wins, f"tp_sl_wins<{min_wins} ({wins_ct})"),
                ("holdout_acc", accuracy >= symbol_min_acc, f"holdout_acc < {symbol_min_acc*100:.1f}% ({accuracy*100:.1f}%)"),
                ("edge", edge >= min_edge, f"edge < {min_edge*100:.1f}% ({edge*100:.1f}%)"),
                ("wf_folds", wf["fold_count"] >= min_wf_folds, f"wf_folds < {min_wf_folds} ({wf['fold_count']})"),
                ("wf_acc_mean", wf["acc_mean"] >= min_wf_acc, f"wf_acc_mean < {min_wf_acc*100:.1f}% ({wf['acc_mean']*100:.1f}%)"),
                ("wf_edge_mean", wf["edge_mean"] >= min_wf_edge, f"wf_edge_mean < {min_wf_edge*100:.1f}% ({wf['edge_mean']*100:.1f}%)"),
                ("wf_acc_min", wf["acc_min"] >= symbol_wf_acc_min, f"wf_acc_min < {symbol_wf_acc_min*100:.1f}% ({wf['acc_min']*100:.1f}%)"),
            ]
            if calib_info.get("calibrated") and int(holdout.get("gate_n") or 0) >= 10:
                brier = calib_info.get("gate_brier")
                brier_ok = brier is not None and float(brier) < max_brier
                gate_checks.append((
                    "calibration_brier",
                    brier_ok,
                    f"gate_brier {brier} >= {max_brier}" if not brier_ok else f"gate_brier {brier} < {max_brier}",
                ))
            fail_reason = next((msg for _, ok, msg in gate_checks if not ok), None)
            passes = fail_reason is None
            LOGGER.info(
                f"RETRAIN [{symbol}]: acc={accuracy*100:.1f}% edge={edge*100:.1f}% "
                f"brier={calib_info.get('gate_brier')} wf_folds={wf['fold_count']} "
                f"wf_acc_mean={wf['acc_mean']*100:.1f}% "
                f"wf_edge_mean={wf['edge_mean']*100:.1f}% wf_acc_min={wf['acc_min']*100:.1f}% "
                f"| {'PASS' if passes else 'FAIL: ' + fail_reason}"
            )
            symbol_detail.update({
                "passed": bool(passes),
                "fail_reason": fail_reason,
                "stage": "trained",
                "n_samples": n_samples,
                "wins_ct": wins_ct,
                "natural_rate": round(natural_rate, 4),
                "holdout_acc": round(accuracy, 4),
                "edge": round(edge, 4),
                "calibration": calib_info,
                "holdout_slices": {
                    "train_n": int(len(X_train)),
                    "calib_n": int(len(X_calib)),
                    "gate_n": int(len(X_gate)),
                },
                "wf_fold_count": int(wf["fold_count"]),
                "wf_acc_mean": round(wf["acc_mean"], 4),
                "wf_acc_min": round(wf["acc_min"], 4),
                "wf_edge_mean": round(wf["edge_mean"], 4),
                "wf_edge_min": round(wf["edge_min"], 4),
                "pool": pool_info,
                "feature_audit": feature_audit,
                "feature_inversions": sorted(invert_cols),
                "reliability_monotonic": reliability_mono,
                "gates": [{"name": n, "passed": bool(p), "msg": m} for n, p, m in gate_checks],
                "thresholds": {
                    "min_train_rows": min_rows,
                    "min_tp_sl_wins": min_wins,
                    "min_holdout_acc": symbol_min_acc,
                    "min_edge": min_edge,
                    "min_wf_folds": min_wf_folds,
                    "min_wf_acc_mean": min_wf_acc,
                    "min_wf_acc_min": symbol_wf_acc_min,
                    "min_wf_edge": min_wf_edge,
                },
            })
            details.append(symbol_detail)
            if passes:
                model_bytes = base64.b64encode(pickle.dumps(final_model)).decode('ascii')
                meta = json.dumps({
                    "feature_cols": active_cols, "accuracy": accuracy,
                    "natural_rate": natural_rate, "edge": edge,
                    "trained_at": time.time(), "n_samples": len(rows),
                    "engine_version": "v3.2_tp_sl_daily",
                    "label_type": LABEL_TYPE,
                    "label_schema": _v3_label_schema(),
                    "feature_schema": _v3_feature_schema(),
                    "label_hold_bars": V3_LABEL_HOLD_BARS,
                    "calibrated": calib_info["calibrated"],
                    "calibration_method": calib_info.get("method"),
                    "gate_brier": calib_info.get("gate_brier"),
                    "reliability_bins": calib_info.get("reliability_bins") or [],
                    "conformal_ok": bool((calib_info.get("conformal") or {}).get("ok", False)),
                    "conformal_q_hat": (calib_info.get("conformal") or {}).get("q_hat"),
                    "conformal_alpha": (calib_info.get("conformal") or {}).get("alpha"),
                    "conformal_samples": (calib_info.get("conformal") or {}).get("samples", 0),
                    "conformal_brier_raw": (calib_info.get("conformal") or {}).get("brier_raw"),
                    "conformal_brier_calibrated": (calib_info.get("conformal") or {}).get("brier_calibrated"),
                    "conformal_brier_improvement": (calib_info.get("conformal") or {}).get("brier_improvement"),
                    "ensemble": bool(calib_info.get("ensemble", False)),
                    "ensemble_members": calib_info.get("members"),
                    "pool_enabled": pool_info["enabled"],
                    "pool_peer_sample_count": pool_info["peer_sample_count"],
                    "pool_peers": [p["symbol"] for p in peers_used],
                    "wf_fold_count": wf["fold_count"],
                    "wf_acc_mean": wf["acc_mean"],
                    "wf_acc_min": wf["acc_min"],
                    "wf_edge_mean": wf["edge_mean"],
                    "wf_edge_min": wf["edge_min"],
                    "feature_audit": feature_audit,
                    "feature_inversions": sorted(invert_cols),
                    "reliability_monotonic": reliability_mono,
                })
                with db_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("CREATE TABLE IF NOT EXISTS ghost_v3_model "
                                "(key TEXT PRIMARY KEY, value TEXT, updated_at BIGINT)")
                    cur.execute("INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s) "
                                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
                                (f"model_{symbol}", model_bytes, int(time.time())))
                    cur.execute("INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s) "
                                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
                                (f"meta_{symbol}", meta, int(time.time())))
                total_passed += 1
        except Exception as e:
            LOGGER.warning(f"Training failed {symbol}: {e}")
            symbol_detail.update({
                "passed": False, "fail_reason": "exception: " + str(e)[:200],
                "stage": "exception",
            })
            details.append(symbol_detail)
        if symbol_delay > 0 and idx + 1 < len(symbols_and_types):
            time.sleep(symbol_delay)
    LOGGER.info(f"v3.2 training: {total_passed}/{len(symbols_and_types)} passed")
    _persist_train_details(details)
    return None, total_passed / max(len(symbols_and_types), 1), total_passed > 0


def model_serve_guard(meta: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return a reject code if meta fails load_model guards; None if serveable."""
    if not meta or not isinstance(meta, dict):
        return "missing_meta"
    if meta.get("label_type") != LABEL_TYPE:
        return "label_type_mismatch"
    if meta.get("label_schema") != _v3_label_schema():
        return "label_schema_stale"
    if meta.get("feature_schema") != _v3_feature_schema():
        return "feature_schema_stale"
    if time.time() - meta.get("trained_at", 0) > 14 * 86400:
        return "model_expired"
    return None


def load_model(symbol=None):
    if not symbol: return None, None, None
    try:
        import pickle, base64
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM ghost_v3_model WHERE key=%s", (f"model_{symbol}",))
            row = cur.fetchone()
            if not row: return None, None, None
            model = pickle.loads(base64.b64decode(row[0]))
            cur.execute("SELECT value FROM ghost_v3_model WHERE key=%s", (f"meta_{symbol}",))
            mrow = cur.fetchone()
            if not mrow: return None, None, None
            meta = json.loads(mrow[0])
            reject = model_serve_guard(meta)
            if reject:
                LOGGER.info("load_model %s: rejected (%s)", symbol, reject)
                return None, None, None
            return model, meta.get('feature_cols', FEATURE_COLS), meta
    except Exception as e:
        LOGGER.warning(f"load_model {symbol}: {e}"); return None, None, None

def predict_live_ex(symbol, asset_type, scores=None):
    """
    Like predict_live but returns (signal_tuple_or_None, reason_code_or_None).
    reason_code is for diagnostics/metrics only.

    If a mutable `scores` dict is passed, it is populated on the success path
    with the specialist score vector + regime-at-issuance (blueprint §4: the
    pick journal must capture the full score vector, not just the outcome).
    Callers that omit it are unaffected.
    """
    model, feature_cols, meta = load_model(symbol)
    if model is None:
        return None, "no_model"

    rows = _fetch_ohlcv(symbol, asset_type, period='5d', interval='1h')
    if not rows or len(rows) < 30:
        return None, "intraday_data"

    premarket_ctx = None
    try:
        from core.prediction import _is_premarket, _premarket_scan_enabled
        if _is_premarket() and _premarket_scan_enabled():
            from core.prices import get_extended_session
            premarket_ctx = get_extended_session(symbol)
            sp = premarket_ctx.get("session_price") or premarket_ctx.get("live_price")
            if sp and float(sp) > 0 and rows:
                # Overlay extended-hours price on the last daily bar so momentum
                # features reflect the gap without retraining on intraday bars.
                last = dict(rows[-1])
                px = float(sp)
                last["close"] = px
                last["high"] = max(float(last.get("high") or px), px)
                last["low"] = min(float(last.get("low") or px), px)
                rows = rows[:-1] + [last]
    except Exception as _pm_e:
        LOGGER.debug("premarket overlay skipped %s: %s", symbol, str(_pm_e)[:80])

    features = _calculate_features(rows)
    from core.feature_schema import attach_feature_asof
    attach_feature_asof(features, rows[-1].get("ts") if rows else None)

    # P2 (audit): cross-sectional rank features — percentile within watchlist
    try:
        from core.cross_sectional import compute_cross_sectional, CS_FEATURE_NAMES
        _all_sym_feats = getattr(predict_live_ex, '_last_scan_features', None)
        if _all_sym_feats and symbol in _all_sym_feats:
            cs = compute_cross_sectional(symbol, features, _all_sym_feats)
            for k, v in cs.items():
                features[k] = v
    except Exception:
        pass

    # P3 (audit): macro regime features — VIX, yield curve, Fed rate, etc.
    try:
        from core.macro_regime import get_macro_features, MACRO_FEATURE_NAMES
        macro = get_macro_features()
        for k, v in macro.items():
            features[k] = v
    except Exception:
        pass

    # W3: same point-in-time sector relative strength as training, for the
    # current (last) bar. Only when enabled, matching the persisted feature set.
    if _v3_sector_feature_enabled():
        aligned_sector = _align_sector_closes(rows, _fetch_sector_series())
        features["sector_rel_strength"] = _sector_rel_at(
            rows, aligned_sector, len(rows) - 1, _v3_sector_lookback()
        )

    above_ema200 = features.get('above_ema200', 1)
    adx_trending = features.get('adx_trending', 1)
    adx_val = features.get('adx', 25)
    ema_trend_bullish = features.get('ema_trend_bullish', 1)
    rsi = features.get('rsi', 50)
    stoch_k = features.get('stoch_k', 50)

    # Coarse regime label from the gate indicators — placeholder until the HMM
    # specialist (blueprint §3). Captured even when a gate later blocks the pick,
    # so the operator can see the regime at issuance on every cycle.
    if ema_trend_bullish == 1 and adx_trending == 1 and above_ema200 == 1:
        regime_label = "Trend-up"
    elif ema_trend_bullish == 0 and above_ema200 == 0:
        regime_label = "Trend-down"
    elif adx_trending == 0:
        regime_label = "Chop"
    else:
        regime_label = "Neutral"
    if scores is not None:
        scores["schema"] = 1
        if premarket_ctx:
            scores["extended_session"] = premarket_ctx
        scores["regime"] = {
            "label": regime_label,
            "above_ema200": int(above_ema200),
            "adx": round(float(adx_val), 2),
            "adx_trending": int(adx_trending),
            "ema_trend_bullish": int(ema_trend_bullish),
            "rsi": round(float(rsi), 2),
            "stoch_k": round(float(stoch_k), 2),
        }
        # Full indicator vector at issuance (audit §4 pick journal): RSI, MACD,
        # Bollinger, ATR, volume, momentum, EMA/ADX/OBV/stochastic. Captured even
        # when a gate later blocks the pick, so every cycle is journaled.
        scores["features"] = {
            k: (round(float(v), 6) if isinstance(v, (int, float)) else v)
            for k, v in features.items()
        }

    # Regime gates — decided here but ENFORCED after model scoring, so every
    # scan eval carries up_prob (shadow scoring + gate diagnostics score all 44
    # symbols, not just the ones that clear regime). Firing behavior unchanged:
    # a regime block still returns before any signal is emitted.
    regime_block = False
    # Gate 1: below EMA200 + choppy = high-probability loss setup
    if above_ema200 == 0 and adx_trending == 0:
        LOGGER.info(f"REGIME GATE [{symbol}]: below EMA200 + ADX={adx_val:.1f}<20 — skip BUY")
        regime_block = True

    # Gate 2: full bearish alignment, not oversold
    if not regime_block and ema_trend_bullish == 0 and rsi > 40 and stoch_k > 30:
        LOGGER.info(f"REGIME GATE [{symbol}]: bearish EMA stack, RSI={rsi:.1f} not oversold — skip")
        regime_block = True

    # Gate 3: price below 5-day SMA. Only checked when gates 1-2 passed (the
    # SMA fetch costs a daily-bars call; preserves the original early-out cost).
    if not regime_block:
        cur_px = float(rows[-1].get("close") or 0)
        blocked_sma, sma_5d, cur_px = _block_up_below_sma5(symbol, asset_type, cur_px)
        if scores is not None and isinstance(scores.get("regime"), dict):
            if sma_5d is not None:
                scores["regime"]["sma_5d"] = round(float(sma_5d), 4)
                scores["regime"]["price_vs_sma5_pct"] = round((cur_px - sma_5d) / sma_5d * 100, 2)
        if blocked_sma:
            bypass = False
            try:
                from core.regime_calibration import sma5_gate_trend_up_bypass
                if sma5_gate_trend_up_bypass() and regime_label == "Trend-up":
                    bypass = True
                    LOGGER.info(
                        f"REGIME GATE [{symbol}]: SMA5 bypass in Trend-up (price {cur_px:.2f} vs SMA {sma_5d:.2f})"
                    )
            except Exception:
                pass
            if not bypass:
                LOGGER.info(
                    f"REGIME GATE [{symbol}]: price {cur_px:.2f} below 5d SMA {sma_5d:.2f} — skip BUY"
                )
                regime_block = True

    invert_cols = meta.get("feature_inversions") or []
    if invert_cols:
        from core.feature_audit import apply_inversions_to_features
        apply_inversions_to_features(features, invert_cols)
        if scores is not None and isinstance(scores.get("features"), dict):
            apply_inversions_to_features(scores["features"], invert_cols)

    X = np.array([[features.get(c, 0.0) for c in feature_cols]])
    proba = model.predict_proba(X)[0]
    up_prob = float(proba[1])
    min_edge = _v3_min_edge()
    min_acc = _v3_min_holdout_acc()
    min_p = _v3_min_win_proba()
    if scores is not None and isinstance(scores.get("regime"), dict):
        try:
            from core.regime_calibration import effective_min_win_proba, regime_calibration_meta
            rl = scores["regime"].get("label")
            min_p = effective_min_win_proba(rl, base=min_p)
            scores["regime_calibration"] = regime_calibration_meta(rl, base=_v3_min_win_proba())
        except Exception:
            pass
    min_wf_acc = _v3_min_wf_acc_mean()
    edge = meta.get('edge', 0)
    wf_acc_mean = float(meta.get("wf_acc_mean", meta.get("accuracy", 0)))
    wf_edge_mean = float(meta.get("wf_edge_mean", meta.get("edge", 0)))
    wf_fold_count = int(meta.get("wf_fold_count", 0))
    accuracy = meta.get('accuracy', min_acc)

    # Capture the model score vector NOW — before the meta gates and the prob_low
    # return — so /api/wolf/gate-status can show where up_prob landed relative to
    # the gates even on cycles that do not fire.
    if scores is not None:
        fires = up_prob > min_p
        scores["up_prob"] = round(up_prob, 4)
        scores["specialists"] = {
            "daily_swing": {"model": "xgboost_v3", "up_prob": round(up_prob, 4),
                            "vote": "UP" if fires else "none"},
        }
        scores["specialist_count"] = 1
        scores["specialist_agree_up"] = 1 if fires else 0
        scores["model_meta"] = {
            "accuracy": round(float(accuracy), 4),
            "edge": round(float(edge), 4),
            "wf_acc_mean": round(wf_acc_mean, 4),
            "wf_edge_mean": round(wf_edge_mean, 4),
            "wf_fold_count": wf_fold_count,
            "min_win_proba": round(float(min_p), 4),
            "calibrated": bool(meta.get("calibrated", False)),
            "calibration_method": meta.get("calibration_method"),
            "gate_brier": meta.get("gate_brier"),
            "conformal_ok": bool(meta.get("conformal_ok", False)),
            "conformal_q_hat": meta.get("conformal_q_hat"),
            "ensemble": bool(meta.get("ensemble", False)),
        }
        scores["direction_source"] = "classifier_up_prob"
        scores["trade_signal_source"] = "classifier_up_prob"

    # Regime block enforced here — after the score capture above, so the eval
    # journal and shadow scoring see up_prob even on regime-blocked cycles.
    if regime_block:
        return None, "regime_gate"

    if edge < min_edge:
        return None, "meta_gate"
    if meta.get('accuracy', 0) < min_acc:
        return None, "meta_gate"
    if wf_fold_count > 0 and (wf_acc_mean < min_wf_acc or wf_edge_mean < min_edge):
        return None, "meta_gate"

    # Phase 2: confidence = calibrated P(win); Brier/journal treat this as the stated probability.
    # P7 (audit): conformal calibration when available — replaces heuristic with
    # mathematically guaranteed prediction intervals.
    if up_prob > min_p:
        q_hat = meta.get("conformal_q_hat")
        if q_hat is not None and float(q_hat) > 0:
            from core.conformal_calibration import conformal_confidence
            conf = conformal_confidence(up_prob, float(q_hat), float(accuracy), float(min_p))
        else:
            conf = round(min(0.98, max(0.0, up_prob)), 3)
        if scores is not None:
            scores["confidence"] = conf
            if q_hat is not None:
                scores["conformal_q_hat"] = float(q_hat)
        return ("UP", conf), None
    # DOWN signals disabled — 1.5% WR on 274 trades, not viable
    # Ghost is BUY-only system
    return None, "prob_low"


def predict_live(symbol, asset_type):
    """
    Regime gate (jakejk1285 + FarisZnf research):
    - Skip BUY if price below EMA200 AND ADX<20 (downtrend + choppy)
    - Skip BUY if full bearish EMA alignment (20<50<200) unless deep oversold
    """
    sig, _reason = predict_live_ex(symbol, asset_type)
    return sig

def get_model_status():
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM ghost_v3_model WHERE key LIKE 'meta_%' ORDER BY key")
            rows = cur.fetchall()
            if not rows: return {"trained": False, "reason": "No models — run /api/v3/train"}
            model_keys = set()
            cur.execute("SELECT key FROM ghost_v3_model WHERE key LIKE 'model_%'")
            for (k,) in cur.fetchall():
                model_keys.add(k.replace("model_", ""))
            symbols = {}
            stored = {}
            for key, val in rows:
                sym = key.replace('meta_',''); m = json.loads(val)
                reject = model_serve_guard(m)
                if sym not in model_keys:
                    reject = reject or "missing_pickle"
                summary = {
                    "accuracy": round(m.get("accuracy",0)*100,1),
                    "natural_rate": round(m.get("natural_rate",0)*100,1),
                    "edge": round(m.get("edge",0)*100,1),
                    "wf_acc_mean": round(m.get("wf_acc_mean",0)*100,1),
                    "wf_acc_min": round(m.get("wf_acc_min",0)*100,1),
                    "wf_edge_mean": round(m.get("wf_edge_mean",0)*100,1),
                    "wf_edge_min": round(m.get("wf_edge_min",0)*100,1),
                    "wf_fold_count": m.get("wf_fold_count",0),
                    "n_samples": m.get("n_samples",0),
                    "engine": m.get("engine_version","v3.0"),
                    "label_type": m.get("label_type", ""),
                    "label_schema": m.get("label_schema", ""),
                    "label_hold_bars": m.get("label_hold_bars", V3_LABEL_HOLD_BARS),
                    "feature_schema": m.get("feature_schema", ""),
                    "ensemble": bool(m.get("ensemble", False)),
                    "ensemble_members": m.get("ensemble_members"),
                    "conformal_ok": bool(m.get("conformal_ok", False)),
                    "conformal_q_hat": m.get("conformal_q_hat"),
                    "serveable": reject is None,
                }
                if reject:
                    summary["serve_reject"] = reject
                stored[sym] = summary
                if reject is None:
                    symbols[sym] = summary
            out = {
                "trained": bool(symbols),
                "models": len(symbols),
                "models_stored": len(stored),
                "symbols": symbols,
                "stored_symbols": stored,
            }
            if not symbols:
                out["reason"] = "No serveable models — retrain in /admin"
            gate = get_last_train_gate_summary()
            if gate:
                out["last_train_gate"] = gate
            return out
    except Exception as e:
        return {"trained": False, "reason": str(e)}


def get_last_train_gate_summary() -> Dict[str, Any]:
    """Counts from ghost_state.last_train_details — loaded models may exceed gate-passed."""
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT val FROM ghost_state WHERE key='last_train_details'")
            row = cur.fetchone()
        if not row or not row[0]:
            return {}
        payload = json.loads(row[0])
        symbols = payload.get("symbols") or []
        if not isinstance(symbols, list):
            return {}
        attempted = len(symbols)
        passed = sum(1 for s in symbols if isinstance(s, dict) and s.get("passed"))
        return {
            "gate_passed": int(passed),
            "gate_attempted": int(attempted),
            "ts": payload.get("ts"),
        }
    except Exception:
        return {}


def get_last_train_fail_for_symbol(symbol: str) -> Optional[str]:
    """Most recent gate fail reason for a symbol from last_train_details."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT val FROM ghost_state WHERE key='last_train_details'")
            row = cur.fetchone()
        if not row or not row[0]:
            return None
        payload = json.loads(row[0])
        for entry in reversed(payload.get("symbols") or []):
            if not isinstance(entry, dict):
                continue
            if (entry.get("symbol") or "").upper() == sym:
                if entry.get("passed"):
                    return None
                return entry.get("fail_reason") or "gate_failed"
        return None
    except Exception:
        return None
