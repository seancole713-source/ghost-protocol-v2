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
import os, time, logging, json, threading, math
from core.quiet import note_suppressed
import numpy as np
from typing import Any, Dict, List, Optional
from core.vol_targets import base_vol_pct, stop_pct_from_vol
from core.db import ensure_ghost_state
from core.engine_config import (  # noqa: F401 — facade re-exports (PR #130)
    V3_LABEL_HOLD_BARS,
    _min_backtest_bars,
    _backtest_window,
    _v3_ohlcv_period,
    _v3_ohlcv_fetch_retries,
    _v3_train_symbol_delay_sec,
    _v3_scan_symbol_delay_sec,
    _v3_adx_trending_threshold,
    _v3_watchlist_peer_pool_enabled,
    _v3_watchlist_peer_pool_max,
    _model_payload_max_bytes,
    _v3_min_holdout_acc,
    _v3_min_edge,
    _v3_min_wf_edge,
    _v3_min_win_proba,
    _v3_min_tp_sl_wins,
    _v3_min_wf_folds,
    _v3_min_wf_acc_mean,
    _v3_wf_acc_min_slack,
    _v3_split_train_frac,
    _v3_split_calib_frac,
    _v3_holdout_slices,
    _purged_holdout_bounds,
    _v3_wf_acc_min_overrides,
    _v3_holdout_acc_overrides,
    _v3_wf_min_train_floor,
    _v3_wf_min_train_frac,
    _v3_wf_test_size_floor,
    _v3_wf_test_size_frac,
    _v3_calibration_enabled,
    _v3_calibration_method,
    _v3_pool_training_enabled,
    _v3_down_signals_enabled,
    _v3_wolf_sample_weight,
    _v3_sector_feature_enabled,
    _v3_sector_proxy,
    _v3_sector_lookback,
    _v3_ensemble_enabled,
    _v3_prune_features,
    _v3_feature_schema,
    _v3_feature_audit_enabled,
    _v3_wf_purge,
    _v3_max_calibration_brier,
)
from core.engine_features import (  # noqa: F401 — facade re-exports (PR #130)
    FEATURE_COLS,
    _calculate_features,
    _date_key,
    _align_sector_closes,
    _sector_rel_at,
)
from core.engine_calibration import (  # noqa: F401 — facade re-exports (PR #130)
    _reliability_bins,
    _evaluate_calibration_holdout,
    _maybe_calibrate,
    _build_ensemble,
)
from core.engine_indicators import (  # noqa: F401 — facade re-exports (PR #130)
    _rsi,
    _macd,
    _bollinger,
    _volume_ratio,
    _price_momentum,
    _ema,
    _adx,
    _atr,
    _obv_slope,
    _stochastic,
)


LOGGER = logging.getLogger("ghost.signal_v3")

# PR #14 deploy-marker — if this line isn't in Railway logs at app startup,
# the deployment did NOT pick up code at or after PR #14. The marker also
# helps disambiguate cached vs fresh Python module loads after redeploy.
LOGGER.info("[signal_engine] MODULE_LOADED PR17_DIAG ohlcv_chain=sip|iex|polygon|yfinance|stooq")

LABEL_TYPE = "tp_sl_daily"
LABEL_TYPE_DIRECTION = "direction_daily"
VALIDATION_SCHEMA = "honest_oos_v3"  # compatibility export; use call-time helper below


def _v3_label_type() -> str:
    """Active label type — re-exported from engine_config for schema identity."""
    from core.engine_config import _v3_label_type as _lt
    return _lt()


def _v3_validation_schema() -> str:
    """Validation identity including effective split and purge semantics."""
    return (
        f"{VALIDATION_SCHEMA}:train={_v3_split_train_frac():.12g}"
        f":calib={_v3_split_calib_frac():.12g}:purge={_v3_wf_purge()}"
    )


# Phase 5: calendar forward bars + shared resolve path (see core.tp_sl_resolve.LABEL_SCHEMA).
def _v3_label_schema() -> str:
    """Active deterministic label contract including label type and geometry."""
    from core.tp_sl_resolve import LABEL_SCHEMA
    label_type = _v3_label_type()
    if label_type == "direction":
        return f"direction_v1:hold={V3_LABEL_HOLD_BARS}"
    if label_type == "cross_sectional":
        return f"cross_sectional_v1:hold={V3_LABEL_HOLD_BARS}"
    from core.vol_targets import tp_sl_geometry_schema
    return f"{LABEL_SCHEMA}:{tp_sl_geometry_schema(hold_bars=V3_LABEL_HOLD_BARS)}"

# Daily bars only: approximate 48h stock hold with this many forward bars (24h each).


# Training thresholds. All read from env at call time so tests can
# monkeypatch.setenv and so ops can ratchet defaults from Railway without
# a code change. Defaults were lowered (post-restructure WOLF only has
# ~250 trading days of SIP data, so the prior pre-bankruptcy defaults
# locked out training entirely).
def _min_train_rows() -> int:
    """Min labeled samples required to attempt training (was 80)."""
    return max(1, int(os.getenv("MIN_TRAIN_ROWS", "20")))


def _v3_research_tier_enabled() -> bool:
    """Store gate-failing models as tier=research so shadow evals keep an
    up_prob source while the fleet has no proven models to mint (operator-
    approved 2026-07-16). Research models are hard-blocked from firing in
    _evaluate_lane and never overwrite a proven serveable model."""
    return os.getenv("V3_RESEARCH_TIER", "1") == "1"






def _effective_backtest_window(n_bars: int) -> int:
    """Shrink the feature window when history is thin so labeling still produces rows."""
    margin = V3_LABEL_HOLD_BARS + 1
    min_rows = _min_train_rows()
    cap = max(20, int(n_bars) - margin - min_rows)
    return min(_backtest_window(), cap)
















_OHLCV_CACHE: dict = {}
_OHLCV_CACHE_LOCK = threading.Lock()
_OHLCV_KEY_LOCKS: dict = {}


def _ohlcv_cache_ttl_s() -> int:
    """Success-entry TTL. Daily bars move slowly; 15 min keeps intraday
    resolution fresh while collapsing repeat fetches across resolvers."""
    return max(0, int(os.getenv("V3_OHLCV_CACHE_TTL_S", "900")))


def _ohlcv_neg_cache_ttl_s() -> int:
    """Failure-entry TTL. A symbol with no data anywhere (delisted etc.) used
    to re-run the full 5-tier x 3-retry chain on EVERY resolver pass — a
    permanent 429 storm. Cache the miss so it retries at most once per TTL."""
    return max(0, int(os.getenv("V3_OHLCV_NEG_CACHE_TTL_S", "600")))


def clear_ohlcv_cache() -> None:
    """Drop in-memory OHLCV cache (call at start of each batch train)."""
    with _OHLCV_CACHE_LOCK:
        _OHLCV_CACHE.clear()


def _ohlcv_key_lock(cache_key) -> "threading.Lock":
    """Per-key lock so concurrent callers (reconcile + watchdog + squeeze)
    never run the fetch chain for the same symbol simultaneously — the second
    caller waits briefly and reuses the first one's cached result."""
    with _OHLCV_CACHE_LOCK:
        lock = _OHLCV_KEY_LOCKS.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            _OHLCV_KEY_LOCKS[cache_key] = lock
        return lock

MODEL_DB_KEY = "ghost_v3_model_pkl"
FEATURES_DB_KEY = "ghost_v3_features_json"

# ── model cache ──────────────────────────────────────────────────────────────
# Every scan cycle used to re-read + re-unpickle every model from Postgres
# (43 symbols × 2 directions × 2 SELECTs ≈ 172 round-trips per cycle) even
# though models only change on retrain. Cache deserialized models per
# (symbol, direction) with a TTL; writers invalidate explicitly.
_MODEL_CACHE: dict = {}
_MODEL_CACHE_LOCK = threading.Lock()

# Free-tier Alpaca keys are never SIP-entitled: after the first 403, skip SIP
# for a while instead of burning one guaranteed-403 call per symbol per sweep.
_SIP_FORBIDDEN = {"until": 0.0}


def _model_cache_ttl_s() -> int:
    return max(0, int(os.getenv("V3_MODEL_CACHE_TTL_S", "3600")))


def invalidate_model_cache(symbol: str = None) -> None:
    """Drop cached models — all of them, or just one symbol's directions.

    Called after train_and_validate persists new models and from the admin
    delete/purge paths, so a stale in-memory model is never served after its
    DB row changed.
    """
    sym = (symbol or "").upper()
    with _MODEL_CACHE_LOCK:
        if not sym:
            _MODEL_CACHE.clear()
            return
        for key in [k for k in _MODEL_CACHE if k[0] == sym]:
            del _MODEL_CACHE[key]
















































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
    # PR #165: point-in-time SEC fundamentals (off by default; toggling
    # changes feature_schema and forces a retrain, same as the sector col).
    from core.fundamental_features import FUNDAMENTAL_FEATURE_NAMES
    from core.fundamental_features import enabled as _fund_enabled
    if _fund_enabled():
        cols.extend(c for c in FUNDAMENTAL_FEATURE_NAMES if c not in prune)
    # Phase 4: news sentiment + options flow features (off by default)
    from core.engine_config import _v3_news_features_enabled, _v3_options_features_enabled
    if _v3_news_features_enabled():
        for c in ('news_sentiment','news_bullish','news_bearish'):
            if c not in prune:
                cols.append(c)
    if _v3_options_features_enabled():
        for c in ('opt_put_call_ratio','opt_skew_elevated_puts','opt_skew_elevated_calls'):
            if c not in prune:
                cols.append(c)
    from core.engine_config import _v3_intraday_features_enabled
    if _v3_intraday_features_enabled():
        from core.intraday_features import INTRADAY_FEATURE_NAMES
        cols.extend(c for c in INTRADAY_FEATURE_NAMES if c not in prune)
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


def _walk_forward_scores(
    X, y, X_peer=None, y_peer=None, wolf_weight=1.0, *,
    target_ts=None, peer_ts=None, peer_label_ts=None, feature_cols=None,
):
    """
    Rolling walk-forward validation over time-ordered samples.
    Returns dict with fold_count / mean and minimum fold scores.

    W1: when peer samples (X_peer/y_peer) are supplied, each fold pools them
    into its TRAINING set (up-weighting the target via wolf_weight) while the
    TEST window stays target-only. When target_ts/peer_ts are supplied, peer
    rows are bounded to the fold's training cutoff; future peer history never
    leaks into an earlier fold. With X_peer=None it behaves exactly as the
    target-only version.

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
    peer_ts_arr = np.asarray(peer_ts, dtype=object) if peer_ts is not None else None
    peer_label_ts_arr = np.asarray(peer_label_ts, dtype=object) if peer_label_ts is not None else None
    target_ts_arr = np.asarray(target_ts, dtype=object) if target_ts is not None else None
    if peer_ts_arr is not None and len(peer_ts_arr) != len(X_peer):
        raise ValueError("peer_ts must align with X_peer")
    if peer_label_ts_arr is not None and len(peer_label_ts_arr) != len(X_peer):
        raise ValueError("peer_label_ts must align with X_peer")
    if target_ts_arr is not None and len(target_ts_arr) != len(X):
        raise ValueError("target_ts must align with X")
    folds = []
    for train_end, test_start, test_end in bounds:
        X_train, y_train = X[:train_end], y[:train_end]
        X_test, y_test = X[test_start:test_end], y[test_start:test_end]
        fold_X_peer, fold_y_peer = None, None
        if has_peers:
            if (
                target_ts_arr is not None and peer_ts_arr is not None
                and peer_label_ts_arr is not None
            ):
                cutoff = _valid_asof_ts(target_ts_arr[train_end - 1])
                peer_mask = np.array([
                    cutoff > 0
                    and 0 < _valid_asof_ts(feature_ts) <= cutoff
                    and 0 < _valid_asof_ts(label_ts) <= cutoff
                    for feature_ts, label_ts in zip(peer_ts_arr, peer_label_ts_arr)
                ])
                fold_X_peer, fold_y_peer = X_peer[peer_mask], y_peer[peer_mask]
            else:
                # Unknown chronology is not evidence. Exclude all peers rather
                # than treating future rows as available to historical folds.
                fold_X_peer = X_peer[:0]
                fold_y_peer = y_peer[:0]

        # Sign selection is model selection. Recompute it inside each fold from
        # that fold's target training labels only; later target labels and peer
        # labels must not influence an earlier out-of-time score.
        if feature_cols and _v3_feature_audit_enabled() and len(X_train) >= 10:
            from core.feature_audit import (
                apply_inversions_to_matrix,
                audit_gate_features,
                select_inverted_features,
            )
            fold_audit = audit_gate_features(X_train, y_train, feature_cols)
            fold_inversions = select_inverted_features(fold_audit)
            X_train = apply_inversions_to_matrix(X_train, feature_cols, fold_inversions)
            X_test = apply_inversions_to_matrix(X_test, feature_cols, fold_inversions)
            if fold_X_peer is not None:
                fold_X_peer = apply_inversions_to_matrix(
                    fold_X_peer, feature_cols, fold_inversions,
                )

        sample_weight = None
        if has_peers:
            sample_weight = np.concatenate([
                np.full(train_end, float(wolf_weight)), np.ones(len(fold_X_peer))
            ])
            if len(fold_X_peer):
                X_train = np.vstack([X_train, fold_X_peer])
                y_train = np.concatenate([y_train, fold_y_peer])
        pos_ct = int(np.sum(y_train))
        neg_ct = int(len(y_train) - pos_ct)
        if pos_ct <= 0:
            continue
        natural_rate = float(np.mean(y_test))
        no_skill_accuracy = max(natural_rate, 1.0 - natural_rate)
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
        folds.append({
            "acc": acc,
            "nat": natural_rate,
            "no_skill": no_skill_accuracy,
            "edge": acc - no_skill_accuracy,
        })

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


def _simulate_down_tp_sl(rows: list, entry_idx: int, hold_bars: int, vol_pct: float) -> str:
    """DOWN label generator — delegates to shared tp_sl_resolve (Phase 2, PR #116)."""
    from core.tp_sl_resolve import simulate_down_tp_sl_label
    return simulate_down_tp_sl_label(rows, entry_idx, hold_bars, vol_pct)




















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
    days_map = {'3m': 90, '6m': 180, '1y': 365, '2y': 730, '5y': 1825}
    lookback_days = days_map.get(period, 365)
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)
    start_str = start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    end_str = end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    def _try_feed(feed):
        try:
            url = (f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/bars"
                   f"?timeframe=1Day&limit=10000&feed={feed}"
                   f"&start={start_str}&end={end_str}")
            r = _req.get(url, headers=headers, timeout=30)
            if r.status_code != 200:
                LOGGER.info(f"Alpaca feed={feed} {symbol}: HTTP {r.status_code}")
                if feed == 'sip' and r.status_code == 403:
                    # Free-tier keys are never SIP-entitled — remember and stop
                    # burning one guaranteed-403 call per symbol per sweep.
                    _SIP_FORBIDDEN["until"] = time.time() + 6 * 3600
                return None
            # Alpaca returns HTTP 200 with "bars": null for symbols with no
            # data (e.g. delisted) — .get('bars', []) keeps the null, then
            # iteration raises "'NoneType' object is not iterable".
            bars = r.json().get('bars') or []
            rows = [{'ts': b.get('t', ''), 'open': float(b.get('o', 0)),
                     'high': float(b.get('h', 0)), 'low': float(b.get('l', 0)),
                     'close': float(b.get('c', 0)), 'volume': float(b.get('v', 0))}
                    for b in bars if b.get('c', 0) > 0]
            return rows if rows else None
        except Exception as e:
            LOGGER.warning(f"Alpaca feed={feed} {symbol}: {e}")
            return None

    rows = None
    feed_used = 'sip'
    if time.time() >= _SIP_FORBIDDEN["until"]:
        rows = _try_feed('sip')
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


def _normalize_daily_ohlcv(rows) -> Optional[List[Dict[str, Any]]]:
    """Canonicalize provider bars or reject the response as invalid evidence.

    Provider order is not chronology. Bars are sorted by parsed timestamp, but
    duplicate timestamps and impossible/non-finite OHLCV reject the complete
    response because silently dropping a bar changes label horizons.
    """
    from core.feature_schema import feature_asof_unix

    normalized = []
    seen = set()
    try:
        for raw in rows or []:
            ts = _valid_asof_ts(feature_asof_unix(raw.get("ts")))
            o = float(raw.get("open"))
            hi = float(raw.get("high"))
            lo = float(raw.get("low"))
            close = float(raw.get("close"))
            volume = float(raw.get("volume", 0) or 0)
            values = (o, hi, lo, close, volume)
            if (
                ts <= 0 or ts in seen
                or not all(np.isfinite(v) for v in values)
                or min(o, hi, lo, close) <= 0 or volume < 0
                or lo > min(o, close) or hi < max(o, close) or lo > hi
            ):
                return None
            seen.add(ts)
            normalized.append({
                "ts": raw.get("ts"), "open": o, "high": hi,
                "low": lo, "close": close, "volume": volume,
            })
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    normalized.sort(key=lambda row: feature_asof_unix(row["ts"]))
    return normalized or None


def _fetch_ohlcv(symbol, asset_type, period=None, interval='1d'):
    """Fetch canonical daily OHLCV with caching and in-flight dedupe."""
    period = period or _v3_ohlcv_period()
    sym = (symbol or "").upper()
    atype = (asset_type or "stock").strip().lower()
    cache_key = (sym, atype, period)

    def _cached():
        with _OHLCV_CACHE_LOCK:
            hit = _OHLCV_CACHE.get(cache_key)
        if hit is not None:
            expires_at, rows = hit
            if time.time() < expires_at:
                return True, rows
        return False, None

    ok, rows = _cached()
    if ok:
        return rows
    with _ohlcv_key_lock(cache_key):
        # Double-check: another thread may have fetched while we waited.
        ok, rows = _cached()
        if ok:
            return rows
        retries = _v3_ohlcv_fetch_retries()
        for attempt in range(retries):
            raw_rows = _fetch_ohlcv_once(symbol, asset_type, period, interval)
            rows = _normalize_daily_ohlcv(raw_rows) if raw_rows else None
            if rows:
                with _OHLCV_CACHE_LOCK:
                    _OHLCV_CACHE[cache_key] = (time.time() + _ohlcv_cache_ttl_s(), rows)
                return rows
            if raw_rows:
                LOGGER.warning("_fetch_ohlcv %s: rejected malformed OHLCV response", sym)
            if attempt + 1 < retries:
                delay = 0.5 * (2 ** attempt)
                LOGGER.info(
                    f"_fetch_ohlcv {sym}: empty on attempt {attempt + 1}/{retries}, retry in {delay:.1f}s"
                )
                time.sleep(delay)
        neg_ttl = _ohlcv_neg_cache_ttl_s()
        if neg_ttl > 0:
            with _OHLCV_CACHE_LOCK:
                _OHLCV_CACHE[cache_key] = (time.time() + neg_ttl, None)
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

    days_map = {'3m': 90, '6m': 180, '1y': 365, '2y': 730, '5y': 1825}
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
                # PR #80: reject NaN/Inf — same fix as yfinance path (PR #79)
                import math as _math
                if not _math.isfinite(close) or close <= 0:
                    continue
                row_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if row_date < cutoff_date:
                    skipped_pre_cutoff += 1
                    continue
                o = float(row.get("Open", 0) or 0)
                h = float(row.get("High", 0) or 0)
                l = float(row.get("Low", 0) or 0)
                v = float(row.get("Volume", 0) or 0)
                if not all(_math.isfinite(x) and x >= 0 for x in (o, h, l, v)):
                    continue
                rows.append({
                    "ts": row_date.strftime("%Y-%m-%dT00:00:00Z"),
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": close,
                    "volume": v,
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
    days_map = {'3m': 90, '6m': 180, '1y': 365, '2y': 730, '5y': 1825}
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
                # PR #80: reject NaN/Inf
                import math as _math
                if not _math.isfinite(close) or close <= 0:
                    continue
                ts = datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                o = float(bar.get("o", 0))
                h = float(bar.get("h", 0))
                l = float(bar.get("l", 0))
                v = float(bar.get("v", 0))
                if not all(_math.isfinite(x) and x >= 0 for x in (o, h, l, v)):
                    continue
                rows.append({
                    "ts": ts,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": close,
                    "volume": v,
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
    from core.circuit_breaker import _yfinance_cb

    # Circuit breaker gate — skip yfinance entirely when circuit is open
    if not _yfinance_cb.allow():
        LOGGER.info(f"yfinance {symbol}: circuit OPEN, skipping")
        return None

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
                _yfinance_cb.record_success()
                return rows
        end = _dt.datetime.now(_dt.timezone.utc)
        start = end - _dt.timedelta(days=240)
        rows = _yf_rows_from_history(tk, start=start, end=end)
        if rows:
            LOGGER.info(f"yfinance {symbol}: {len(rows)} bars (start={start.date()})")
            _yfinance_cb.record_success()
            return rows
        # Empty history = no data for ticker (delisted/thin) — not a failure
        return None
    except Exception as e:
        es = str(e)
        # 429 / rate-limit = real outage, count as breaker failure
        if "429" in es or "Too Many Requests" in es or "rate limit" in es.lower():
            LOGGER.warning(f"yfinance {symbol}: RATE LIMITED (429) — counting as breaker failure")
            _yfinance_cb.record_failure()
        elif "connection" in es.lower() or "timeout" in es.lower() or "timed out" in es.lower():
            LOGGER.warning(f"yfinance {symbol}: connection/timeout — counting as breaker failure: {e}")
            _yfinance_cb.record_failure()
        else:
            # JSON parse errors (empty response / Yahoo blocking Railway IP) — count as breaker failure
            if "Expecting value" in es or "JSON" in es or "json" in es.lower() or "parse" in es.lower():
                LOGGER.warning(f"yfinance {symbol}: JSON parse error (empty response) — counting as breaker failure: {e}")
                _yfinance_cb.record_failure()
            else:
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
                # PR #79: reject NaN/Inf — NaN <= 0 is False in Python,
                # so non-finite values would pass the old check and produce
                # NaN features that crash JSONB inserts.
                import math as _math
                if not _math.isfinite(close) or close <= 0:
                    continue
                ts = ix.strftime('%Y-%m-%dT%H:%M:%SZ') if hasattr(ix, 'strftime') else str(ix)
                o = float(row["Open"])
                h = float(row["High"])
                l = float(row["Low"])
                v = float(row.get("Volume", 0) or 0)
                if not all(_math.isfinite(x) and x >= 0 for x in (o, h, l, v)):
                    continue
                rows.append({
                    'ts': ts,
                    'open': o,
                    'high': h,
                    'low': l,
                    'close': close,
                    'volume': v,
                })
            except Exception:
                continue
        return rows if rows else None
    except Exception as e:
        es = str(e)
        # Re-raise rate-limit errors so the caller can count them as breaker failures
        if "429" in es or "Too Many Requests" in es or "rate limit" in es.lower():
            raise
        return None


def backtest_symbol(symbol, asset_type):
    rows = _fetch_ohlcv(symbol, asset_type)
    min_bars = _min_backtest_bars()
    if not rows or len(rows) < min_bars:
        return [], []
    vol_pct = base_vol_pct(symbol, asset_type)
    labeled_up = []
    labeled_down = []
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
        # Phase 1: point-in-time macro features for this training bar's date
        try:
            bar_date = str(rows[i].get("ts", ""))[:10]
            if bar_date:
                from core.macro_regime import get_macro_features_for_date
                macro = get_macro_features_for_date(bar_date)
                for k, v in macro.items():
                    features[k] = v
        except Exception:
            note_suppressed()
        # PR #165: point-in-time SEC fundamentals for this bar's date —
        # a quarter is visible only once its SEC filed date has passed.
        try:
            from core.fundamental_features import enabled as _fund_on
            if _fund_on():
                bar_date = str(rows[i].get("ts", ""))[:10]
                if bar_date:
                    from core.fundamental_features import get_fundamental_features_for_date
                    features.update(get_fundamental_features_for_date(symbol, bar_date))
        except Exception:
            note_suppressed()
        # Phase 4: point-in-time news sentiment + options flow features
        try:
            from core.engine_config import _v3_news_features_enabled, _v3_options_features_enabled
            if _v3_news_features_enabled():
                features['news_sentiment'] = 0.0
                features['news_bullish'] = 0
                features['news_bearish'] = 0
                try:
                    from core.news_sentiment import fetch_news_sentiment
                    ns = fetch_news_sentiment(symbol, limit=5)
                    if ns.get('ok'):
                        features['news_sentiment'] = float(ns.get('sentiment_score', 0.0))
                        label = str(ns.get('sentiment_label', '')).upper()
                        features['news_bullish'] = 1 if label == 'BULLISH' else 0
                        features['news_bearish'] = 1 if label == 'BEARISH' else 0
                except Exception:
                    pass
            if _v3_options_features_enabled():
                features['opt_put_call_ratio'] = 1.0
                features['opt_skew_elevated_puts'] = 0
                features['opt_skew_elevated_calls'] = 0
                try:
                    from core.options_flow import probe_options_flow
                    opts = probe_options_flow(symbol)
                    if opts.get('ok') and opts.get('available'):
                        pcr = opts.get('put_call_volume_ratio')
                        features['opt_put_call_ratio'] = float(pcr) if pcr is not None else 1.0
                        skew = str(opts.get('skew_hint', ''))
                        features['opt_skew_elevated_puts'] = 1 if skew == 'elevated_puts' else 0
                        features['opt_skew_elevated_calls'] = 1 if skew == 'elevated_calls' else 0
                except Exception:
                    pass
        except Exception:
            note_suppressed()
        # Phase 5: intraday microstructure features from 1-hour bars
        try:
            from core.engine_config import _v3_intraday_features_enabled
            if _v3_intraday_features_enabled():
                hourly = _fetch_ohlcv(symbol, asset_type, period="5d", interval="1h")
                if hourly and len(hourly) >= 6:
                    prev_close = None
                    if len(rows) >= 2:
                        prev_close = float(rows[-2].get("close") or 0)
                    from core.intraday_features import compute_intraday_features
                    intra = compute_intraday_features(hourly, prev_close=prev_close)
                    features.update(intra)
        except Exception:
            note_suppressed()
        from core.tp_sl_resolve import simulate_tp_sl_label_detail, simulate_direction_label, entry_predicted_at, forward_bars_after_entry
        label_type = _v3_label_type()
        if label_type == "cross_sectional":
            # Cross-sectional mode: store forward return for later median-split.
            # Labels are computed in train_and_validate after all symbols are collected.
            entry_close = float(rows[i].get("close") or 0)
            if entry_close > 0:
                predicted_at = entry_predicted_at(rows, i)
                fwd = forward_bars_after_entry(rows, predicted_at, V3_LABEL_HOLD_BARS)
                if len(fwd) >= V3_LABEL_HOLD_BARS:
                    final_close = float(fwd[V3_LABEL_HOLD_BARS - 1].get("close") or 0)
                    if final_close > 0:
                        fwd_return = (final_close - entry_close) / entry_close
                        labeled_up.append({
                            "features": dict(features), "label": 0,  # placeholder, set later
                            "outcome": "PENDING", "direction": "UP",
                            "label_resolved_ts": int(time.time()),
                            "_fwd_return": fwd_return,
                            "_date_key": str(rows[i].get("ts", ""))[:10],
                        })
            up_outcome = None; up_resolved_ts = None
            down_outcome = None; down_resolved_ts = None
        elif label_type == "direction":
            up_outcome, up_resolved_ts = simulate_direction_label(
                rows, i, V3_LABEL_HOLD_BARS,
            )
            down_outcome, down_resolved_ts = simulate_direction_label(
                rows, i, V3_LABEL_HOLD_BARS,
            )
        else:
            up_outcome, up_resolved_ts = simulate_tp_sl_label_detail(
                rows, i, V3_LABEL_HOLD_BARS, vol_pct, "UP",
            )
            down_outcome, down_resolved_ts = simulate_tp_sl_label_detail(
                rows, i, V3_LABEL_HOLD_BARS, vol_pct, "DOWN",
            )
        # Only completed horizons are evidence. Store label availability
        # separately from feature availability for point-in-time peer filters.
        if up_outcome and up_resolved_ts:
            labeled_up.append({
                "features": dict(features), "label": 1 if up_outcome == "WIN" else 0,
                "outcome": up_outcome, "direction": "UP",
                "label_resolved_ts": int(up_resolved_ts),
            })
        if down_outcome and down_resolved_ts:
            labeled_down.append({
                "features": dict(features), "label": 1 if down_outcome == "WIN" else 0,
                "outcome": down_outcome, "direction": "DOWN",
                "label_resolved_ts": int(down_resolved_ts),
            })
    up_wins = sum(1 for r in labeled_up if r["label"] == 1)
    down_wins = sum(1 for r in labeled_down if r["label"] == 1)
    LOGGER.info(
        f"Backtest {symbol}: UP={len(labeled_up)} ({round(up_wins/len(labeled_up)*100,1) if labeled_up else 0}% WIN) "
        f"DOWN={len(labeled_down)} ({round(down_wins/len(labeled_down)*100,1) if labeled_down else 0}% WIN) "
        f"({V3_LABEL_HOLD_BARS}d bars)"
    )
    return labeled_up, labeled_down

def build_training_data(symbols_and_types):
    all_rows = []
    for symbol, asset_type in symbols_and_types:
        up_rows, down_rows = backtest_symbol(symbol, asset_type)
        all_rows.extend(up_rows)
    if len(all_rows) < _min_train_rows(): return None, None, []
    # Phase 1: compute cross-sectional ranks per date across all symbols.
    # Group rows by date, compute percentile ranks within each date group,
    # then inject back into the feature dicts.
    try:
        from collections import defaultdict
        by_date: dict = defaultdict(list)
        for r in all_rows:
            ts = r["features"].get("feature_asof_ts")
            if ts:
                date_key = str(ts)[:10]
                by_date[date_key].append(r)
        for date_key, date_rows in by_date.items():
            if len(date_rows) < 2:
                continue
            # Collect peer values for each metric
            for r in date_rows:
                f = r["features"]
                peers = [r2["features"] for r2 in date_rows if r2 is not r]
                if not peers:
                    continue
                # RSI rank
                rsi_vals = [p["rsi"] for p in peers if p.get("rsi") is not None]
                if rsi_vals and f.get("rsi") is not None:
                    f["cs_rsi_rank"] = round(sum(1 for v in rsi_vals if v < f["rsi"]) / max(len(rsi_vals), 1), 4)
                # Volume rank
                vol_vals = [p["volume_ratio"] for p in peers if p.get("volume_ratio") is not None]
                if vol_vals and f.get("volume_ratio") is not None:
                    f["cs_volume_rank"] = round(sum(1 for v in vol_vals if v < f["volume_ratio"]) / max(len(vol_vals), 1), 4)
                # Momentum rank
                mom_vals = [p["mom_4h"] for p in peers if p.get("mom_4h") is not None]
                if mom_vals and f.get("mom_4h") is not None:
                    f["cs_momentum_rank"] = round(sum(1 for v in mom_vals if v < f["mom_4h"]) / max(len(mom_vals), 1), 4)
                # SMA distance rank
                sma_vals = [p["price_in_range"] for p in peers if p.get("price_in_range") is not None]
                if sma_vals and f.get("price_in_range") is not None:
                    f["cs_sma_distance_rank"] = round(sum(1 for v in sma_vals if v < f["price_in_range"]) / max(len(sma_vals), 1), 4)
                # ATR rank
                atr_vals = [p["atr_pct"] for p in peers if p.get("atr_pct") is not None]
                if atr_vals and f.get("atr_pct") is not None:
                    f["cs_atr_rank"] = round(sum(1 for v in atr_vals if v < f["atr_pct"]) / max(len(atr_vals), 1), 4)
                # ADX rank
                adx_vals = [p["adx"] for p in peers if p.get("adx") is not None]
                if adx_vals and f.get("adx") is not None:
                    f["cs_adx_rank"] = round(sum(1 for v in adx_vals if v < f["adx"]) / max(len(adx_vals), 1), 4)
    except Exception as e:
        LOGGER.warning("cross-sectional training features: %s", str(e)[:120])
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
            ensure_ghost_state(cur)
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












def _valid_asof_ts(value: Any) -> int:
    """Return an exact positive Unix-second timestamp, else invalid evidence."""
    if isinstance(value, (bool, np.bool_)):
        return 0
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not np.isfinite(parsed) or parsed <= 0 or not parsed.is_integer():
        return 0
    return int(parsed)


def _assemble_pooled_training(
    X_train, y_train, peer_rows, feature_cols, wolf_weight, target_train_rows=None,
    *, invert_cols=None, max_asof_ts=None,
):
    """Stack the target's training slice with point-in-time peer samples (W1).

    Returns (X_pooled, y_pooled, sample_weight). Target rows keep wolf_weight;
    peer rows get regime-aware weights (Phase 3) so mismatched vol/momentum/%B
    peers contribute less. Optional ``max_asof_ts`` requires both peer features
    and peer labels to have been available by the target cutoff. ``invert_cols``
    applies the same learned sign map without mutating shared rows.
    """
    from core.feature_audit import peer_regime_weight, regime_profile

    wolf_sw = np.full(len(X_train), float(wolf_weight))
    if max_asof_ts is not None:
        cutoff = _valid_asof_ts(max_asof_ts)
        peer_rows = [
            r for r in peer_rows
            if (
                0 < _valid_asof_ts((r.get("features") or {}).get("feature_asof_ts")) <= cutoff
                and 0 < _valid_asof_ts(r.get("label_resolved_ts")) <= cutoff
            )
        ] if cutoff else []
    if not peer_rows:
        return X_train, y_train, wolf_sw
    profile = regime_profile(target_train_rows or []) if target_train_rows else {}
    inverted = set(invert_cols or [])
    Xp = np.array([[
        -float(r["features"].get(c, 0.0)) if c in inverted else r["features"].get(c, 0.0)
        for c in feature_cols
    ] for r in peer_rows])
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
    Peers that fail to fetch or have too few labeled rows are skipped.

    Phase 2: labels are direction-specific (an UP label means TP-before-SL on
    a long; a DOWN label means TP-before-SL on a short), so peer pools must be
    kept per-direction — feeding UP-labeled peer rows into DOWN training would
    contaminate the DOWN model. Returns ({"UP": rows, "DOWN": rows},
    peers_used) where peers_used lists per-peer sample counts per direction.
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
    pooled = {"UP": [], "DOWN": []}
    used = []
    min_rows = _min_train_rows()
    for p in peers:
        try:
            up_rows, down_rows = backtest_symbol(p, "stock")
        except Exception as e:
            LOGGER.info(f"pool: peer {p} skipped ({str(e)[:80]})")
            continue
        entry = {"symbol": p}
        if up_rows and len(up_rows) >= min_rows:
            pooled["UP"].extend(up_rows)
            entry["n"] = len(up_rows)
        if down_rows and len(down_rows) >= min_rows:
            pooled["DOWN"].extend(down_rows)
            entry["n_down"] = len(down_rows)
        if len(entry) > 1:
            used.append(entry)
        else:
            LOGGER.info(
                f"pool: peer {p} skipped (UP={len(up_rows) if up_rows else 0} "
                f"DOWN={len(down_rows) if down_rows else 0} rows, need {min_rows})"
            )
    return pooled, used


def _train_one_direction(rows, symbol, direction, active_cols, peer_rows, peers_used, pool_info):
    """Train a single-direction model (UP or DOWN). Returns (passed, detail, model_bytes, meta_json)."""
    from xgboost import XGBClassifier
    import pickle, base64, hashlib
    from core.db import db_conn

    n_samples = len(rows)
    min_rows = _min_train_rows()
    wins_ct = int(np.sum([r["label"] for r in rows]))
    min_wins = _v3_min_tp_sl_wins()
    train_end, calib_end = _v3_holdout_slices(len(rows))
    train_fit_end, calib_fit_end = _purged_holdout_bounds(
        len(rows), train_end, calib_end, _v3_wf_purge())
    feature_audit: List[Dict[str, Any]] = []
    invert_cols: set = set()
    if _v3_feature_audit_enabled() and train_fit_end >= 10:
        from core.feature_audit import audit_gate_features, select_inverted_features

        # Choosing a sign map is model selection, so it may inspect only the
        # purged training slice. Calibration and gate labels remain untouched.
        X_audit_train = np.array([
            [r["features"].get(c, 0.0) for c in active_cols]
            for r in rows[:train_fit_end]
        ])
        y_audit_train = np.array([r["label"] for r in rows[:train_fit_end]])
        feature_audit = audit_gate_features(
            X_audit_train, y_audit_train, active_cols,
        )
        invert_cols = select_inverted_features(feature_audit)

    def _feature_vector(row):
        features = row["features"]
        return [
            -float(features.get(c, 0.0)) if c in invert_cols else features.get(c, 0.0)
            for c in active_cols
        ]

    # Do not mutate shared row dictionaries: target, peer, and live inference
    # all receive the same persisted transform exactly once.
    X = np.array([_feature_vector(r) for r in rows])
    y = np.array([r["label"] for r in rows])
    # Leakage guard: labels look ahead V3_LABEL_HOLD_BARS bars, so drop the
    # purge-tail of the train and calib slices — otherwise the precision gate's
    # "proven OOS" threshold is chosen/validated on partially-seen futures.
    X_train, y_train = X[:train_fit_end], y[:train_fit_end]
    X_calib, y_calib = X[train_end:calib_fit_end], y[train_end:calib_fit_end]
    X_gate, y_gate = X[calib_end:], y[calib_end:]
    natural_rate = float(np.mean(y_gate)) if len(y_gate) else 0.0
    target_asof = np.asarray([
        _valid_asof_ts((r.get("features") or {}).get("feature_asof_ts"))
        for r in rows
    ])
    train_cutoff = _valid_asof_ts(target_asof[train_fit_end - 1]) if train_fit_end else 0
    X_fit, y_fit, sample_weight = _assemble_pooled_training(
        X_train, y_train, peer_rows, active_cols, _v3_wolf_sample_weight(),
        target_train_rows=rows[:train_fit_end], invert_cols=invert_cols,
        max_asof_ts=train_cutoff,
    )
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
    from core.stacking_ensemble import is_stacking_enabled
    if is_stacking_enabled() or _v3_ensemble_enabled():
        final_model, calib_info = _build_ensemble(
            model, X_fit, y_fit, sample_weight, X_calib, y_calib)
        if is_stacking_enabled():
            calib_info["ensemble_mode"] = "stacking"
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
    # Phase 3: precision-targeted fire threshold. Chosen on the calib slice,
    # validated on the untouched gate slice. A model without a proven >=target
    # operating point is stored (shadow/research still work) but cannot fire
    # live picks — see _evaluate_lane.
    try:
        from core.precision_gate import select_fire_threshold
        calib_probs = (
            final_model.predict_proba(X_calib)[:, 1] if len(X_calib) else []
        )
        gate_probs_pg = (
            final_model.predict_proba(X_gate)[:, 1] if len(X_gate) else []
        )
        precision_info = select_fire_threshold(
            calib_probs, y_calib, gate_probs_pg, y_gate,
        )
    except Exception as _pg_e:
        precision_info = {"ok": False, "fail_reason": "error: " + str(_pg_e)[:120]}
        gate_probs_pg = []
    calib_info["precision_gate"] = precision_info
    # Gate-slice OOS predictions feed the pooled cross-symbol operating point
    # (see core.precision_gate.store_global_thresholds). Popped from detail by
    # train_and_validate before persisting.
    gate_resolved_asof = np.asarray([
        _valid_asof_ts(r.get("label_resolved_ts")) for r in rows[calib_end:]
    ])
    _gate_oos = {
        # Proof membership is defined by the exact model output; rounding here
        # can move records across the certified threshold. Chronology is bound
        # to when the forward label became knowable, never prediction time.
        "probs": [float(p) for p in gate_probs_pg],
        "labels": [int(v) for v in y_gate],
        "timestamps": [int(v) for v in gate_resolved_asof],
    }
    # Walk-forward starts from raw matrices and derives a separate sign map in
    # each fold. Reusing the final model's training-period map would let later
    # labels influence earlier folds.
    X_wf_raw = np.array([
        [r["features"].get(c, 0.0) for c in active_cols] for r in rows
    ])
    if peer_rows:
        peer_asof = np.asarray([
            _valid_asof_ts((r.get("features") or {}).get("feature_asof_ts"))
            for r in peer_rows
        ])
        peer_label_asof = np.asarray([
            _valid_asof_ts(r.get("label_resolved_ts")) for r in peer_rows
        ])
        X_peer_wf = np.array([
            [r["features"].get(c, 0.0) for c in active_cols]
            for r in peer_rows
        ])
        y_peer_wf = np.array([r["label"] for r in peer_rows])
        wf = _walk_forward_scores(
            X_wf_raw, y, X_peer_wf, y_peer_wf, _v3_wolf_sample_weight(),
            target_ts=target_asof, peer_ts=peer_asof,
            peer_label_ts=peer_label_asof, feature_cols=active_cols,
        )
    else:
        wf = _walk_forward_scores(X_wf_raw, y, feature_cols=active_cols)
    min_wf_acc = _v3_min_wf_acc_mean()
    min_wf_folds = _v3_min_wf_folds()
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
    _pg = calib_info.get("precision_gate") or {}
    LOGGER.info(
        f"RETRAIN [{symbol}/{direction}]: acc={accuracy*100:.1f}% edge={edge*100:.1f}% "
        f"brier={calib_info.get('gate_brier')} wf_folds={wf['fold_count']} "
        f"wf_acc_mean={wf['acc_mean']*100:.1f}% "
        f"wf_edge_mean={wf['edge_mean']*100:.1f}% wf_acc_min={wf['acc_min']*100:.1f}% "
        f"precision_gate={'ok thr=' + str(_pg.get('threshold')) if _pg.get('ok') else 'UNPROVEN (' + str(_pg.get('fail_reason')) + ')'} "
        f"| {'PASS' if passes else 'FAIL: ' + fail_reason}"
    )
    detail = {
        "direction": direction,
        "passed": bool(passes),
        "fail_reason": fail_reason,
        "stage": "trained",
        "n_samples": n_samples,
        "wins_ct": wins_ct,
        "natural_rate": round(natural_rate, 4),
        "no_skill_accuracy": round(float(holdout.get("no_skill_accuracy", 0.0)), 4),
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
        "gate_oos": _gate_oos,
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
    }
    if not passes and not _v3_research_tier_enabled():
        return False, detail, None, None
    tier = "proven" if passes else "research"
    raw_model_bytes = pickle.dumps(final_model)
    model_sha256 = hashlib.sha256(raw_model_bytes).hexdigest()
    model_payload_bytes = len(raw_model_bytes)
    model_bytes = base64.b64encode(raw_model_bytes).decode('ascii')
    meta = json.dumps({
        "feature_cols": active_cols, "accuracy": accuracy,
        "natural_rate": natural_rate,
        "no_skill_accuracy": float(holdout.get("no_skill_accuracy", 0.0)),
        "validation_schema": _v3_validation_schema(),
        "edge": edge,
        "tier": tier, "gate_fail_reason": fail_reason,
        "trained_at": time.time(), "n_samples": len(rows),
        "engine_version": "v3.2_tp_sl_daily",
        "direction": direction,
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
        "precision_gate": calib_info.get("precision_gate"),
        "feature_audit": feature_audit,
        "feature_inversions": sorted(invert_cols),
        "reliability_monotonic": reliability_mono,
        "model_sha256": model_sha256,
        "model_payload_bytes": model_payload_bytes,
    })
    # Research-tier bytes may be stored for shadow evidence, but they did not
    # pass validation and must never count as proven or enter global proof.
    return passes, detail, model_bytes, meta


def _research_overwrite_allowed(symbol, direction) -> bool:
    """A research-tier model may only fill an empty slot or replace a model
    that is itself research-tier or no longer serveable. It must NEVER clobber
    a proven, still-serveable model."""
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM ghost_v3_model WHERE key=%s",
                        (f"meta_{symbol}_{direction.lower()}",))
            row = cur.fetchone()
        if not row or not row[0]:
            return True
        m = json.loads(row[0])
        if m.get("tier") == "research":
            return True
        return model_serve_guard(m) is not None  # stored model already unserveable
    except Exception as e:
        LOGGER.warning("research overwrite check %s/%s failed (refusing store): %s",
                       symbol, direction, str(e)[:80])
        return False  # fail closed: never risk clobbering a proven model


def _store_direction_model(symbol, direction, model_bytes, meta_json) -> bool:
    """Atomically authorize and store one directional model generation.

    Every writer takes the same transaction-scoped database lock. Research
    authorization is re-evaluated after that lock, so another process cannot
    install a proven model between the incumbent check and the two-row UPSERT.
    """
    from core.db import db_conn
    from core.precision_gate import (
        invalidate_global_threshold_cache,
        invalidate_global_threshold_persistent,
    )

    try:
        incoming_meta = json.loads(meta_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        LOGGER.warning("model store %s/%s refused: invalid metadata", symbol, direction)
        return False
    incoming_tier = str(incoming_meta.get("tier") or "").lower()
    if incoming_tier not in ("proven", "research"):
        LOGGER.warning("model store %s/%s refused: invalid tier", symbol, direction)
        return False

    symbol_key = str(symbol or "").upper()
    direction_key = str(direction or "").upper()
    d = direction_key.lower()
    stored = False
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS ghost_v3_model "
                    "(key TEXT PRIMARY KEY, value TEXT, updated_at BIGINT)")
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"ghost_v3_model:{symbol_key}:{direction_key}",),
        )
        if incoming_tier == "research":
            cur.execute(
                "SELECT value FROM ghost_v3_model WHERE key=%s",
                (f"meta_{symbol_key}_{d}",),
            )
            incumbent = cur.fetchone()
            if incumbent and incumbent[0]:
                try:
                    incumbent_meta = json.loads(incumbent[0])
                    replaceable = (
                        incumbent_meta.get("tier") == "research"
                        or model_serve_guard(incumbent_meta) is not None
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    replaceable = False
                if not replaceable:
                    LOGGER.info(
                        "research model %s/%s refused: proven generation retained",
                        symbol_key, direction_key,
                    )
                    return False
        updated_at = int(time.time())
        cur.execute("INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s) "
                    "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
                    (f"model_{symbol_key}_{d}", model_bytes, updated_at))
        cur.execute("INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s) "
                    "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
                    (f"meta_{symbol_key}_{d}", meta_json, updated_at))
        invalidate_global_threshold_persistent(cur)
        stored = True
    if stored:
        invalidate_global_threshold_cache()
    return stored


def _train_cross_sectional(symbols_and_types):
    """Cross-sectional training: one pooled model predicting relative rank.
    
    Collects all symbols' forward returns, computes per-date quartile splits
    (top 25% = WIN, bottom 25% = LOSS, middle 50% = skipped), and trains
    one XGBoost model using ONLY cross-sectional percentile-rank features.
    Natural win rate is exactly 50% by construction — any edge is real
    predictive power.
    """
    from xgboost import XGBClassifier
    import pickle, base64, hashlib
    from collections import defaultdict
    
    clear_ohlcv_cache()
    symbol_delay = _v3_train_symbol_delay_sec()
    active_cols = _active_feature_cols()
    
    # Phase 1: collect all rows with forward returns
    all_rows = []
    for idx, (symbol, asset_type) in enumerate(symbols_and_types):
        try:
            up_rows, _ = backtest_symbol(symbol, asset_type)
            for r in up_rows:
                if r.get("_fwd_return") is not None:
                    r["_symbol"] = symbol
                    all_rows.append(r)
        except Exception as e:
            LOGGER.warning("CS backtest %s: %s", symbol, str(e)[:80])
        if symbol_delay > 0 and idx + 1 < len(symbols_and_types):
            time.sleep(symbol_delay)
    
    if len(all_rows) < _min_train_rows():
        LOGGER.warning("CS training: only %d rows, need %d", len(all_rows), _min_train_rows())
        return None, 0.0, False
    
    # Phase 2: group by date, compute quartile labels + full cross-sectional ranks
    by_date = defaultdict(list)
    for r in all_rows:
        dk = r.get("_date_key", "")
        if dk:
            by_date[dk].append(r)
    
    # Build the list of features to rank (all numeric features in the feature dict)
    _RANK_FEATURES = [
        'rsi','macd_hist','pct_b','volume_ratio','mom_4h','mom_8h','mom_24h',
        'price_in_range','ema20_vs_ema50','adx','atr_pct','obv_slope',
        'stoch_k','stoch_d',
    ]
    
    for dk, date_rows in by_date.items():
        rets = [r["_fwd_return"] for r in date_rows]
        n_syms = len(rets)
        if n_syms < 5:
            continue
        
        # Quartile split: top 25% = WIN, bottom 25% = LOSS, middle 50% = skip
        sorted_rets = sorted(rets)
        p25_idx = n_syms // 4
        p75_idx = (3 * n_syms) // 4
        p25 = sorted_rets[p25_idx]
        p75 = sorted_rets[p75_idx]
        
        for r in date_rows:
            ret = r["_fwd_return"]
            if ret > p75:
                r["label"] = 1; r["outcome"] = "WIN"
            elif ret < p25:
                r["label"] = 0; r["outcome"] = "LOSS"
            # middle 50%: skipped (no label)
        
        # Compute percentile ranks for ALL features against peers on this date
        peers = [r["features"] for r in date_rows]
        for r in date_rows:
            f = r["features"]
            for feat in _RANK_FEATURES:
                vals = [p.get(feat) for p in peers if p.get(feat) is not None]
                my_val = f.get(feat)
                if vals and my_val is not None and len(vals) >= 3:
                    f[f"cs_{feat}_rank"] = round(
                        sum(1 for v in vals if v < my_val) / len(vals), 4)
    
    # Filter to only labeled rows (quartile extremes)
    labeled = [r for r in all_rows if r.get("outcome") in ("WIN", "LOSS")]
    if len(labeled) < _min_train_rows():
        LOGGER.warning("CS training: only %d labeled rows", len(labeled))
        return None, 0.0, False
    
    LOGGER.info("CS training: %d rows across %d symbols, %d dates (quartile split)", 
                len(labeled), len(symbols_and_types), len(by_date))
    
    # Phase 3: build feature matrix using ONLY cross-sectional rank features
    # Drop absolute features — the model must learn from relative ranks
    cs_cols = [c for c in active_cols if c.startswith("cs_") or c.startswith("macro_")]
    if not cs_cols:
        cs_cols = active_cols  # fallback if no cs_ features exist
    
    X = np.array([[r["features"].get(c, 0.0) for c in cs_cols] for r in labeled])
    y = np.array([r["label"] for r in labeled])
    
    LOGGER.info("CS features: %d cross-sectional rank features (from %d total)", 
                len(cs_cols), len(active_cols))
    
    n = len(X)
    train_end, calib_end = _v3_holdout_slices(n)
    train_fit_end, calib_fit_end = _purged_holdout_bounds(n, train_end, calib_end, _v3_wf_purge())
    
    X_train, y_train = X[:train_fit_end], y[:train_fit_end]
    X_calib, y_calib = X[train_end:calib_fit_end], y[train_end:calib_fit_end]
    X_gate, y_gate = X[calib_end:], y[calib_end:]
    
    pos_ct = int(np.sum(y_train))
    neg_ct = len(y_train) - pos_ct
    spw = (neg_ct / pos_ct) if pos_ct > 0 else 1.0
    
    model = XGBClassifier(
        n_estimators=300, max_depth=5, learning_rate=0.02,
        subsample=0.8, colsample_bytree=0.7, min_child_weight=3,
        scale_pos_weight=min(25.0, max(1.0, float(spw))),
        eval_metric='logloss', random_state=42,
    )
    model.fit(X_train, y_train)
    
    # Evaluate directly — calibration destroys edge on near-50% problems
    from core.engine_calibration import _evaluate_calibration_holdout
    holdout = _evaluate_calibration_holdout(model, X_gate, y_gate)
    accuracy = float(holdout["holdout_acc"])
    edge = float(holdout["edge"])
    natural_rate = float(holdout["natural_rate"])
    
    LOGGER.info(
        f"CS RETRAIN: n={n} acc={accuracy*100:.1f}% edge={edge*100:.1f}% "
        f"natural={natural_rate*100:.1f}% train={len(X_train)} calib={len(X_calib)} gate={len(X_gate)} "
        f"features={len(cs_cols)}"
    )
    
    # Store as a single pooled model
    model_bytes = base64.b64encode(pickle.dumps(model)).decode("utf-8")
    model_sha = hashlib.sha256(model_bytes.encode()).hexdigest()
    
    meta = {
        "trained_at": int(time.time()),
        "model_sha256": model_sha,
        "feature_schema": _v3_feature_schema(),
        "label_schema": _v3_label_schema(),
        "validation_schema": _v3_validation_schema(),
        "label_hold_bars": V3_LABEL_HOLD_BARS,
        "label_type": "cross_sectional",
        "accuracy": round(accuracy, 4),
        "edge": round(edge, 4),
        "natural_rate": round(natural_rate, 4),
        "n_samples": n,
        "engine_version": "v3.2",
        "tier": "proven" if (accuracy >= _v3_min_holdout_acc() and edge >= _v3_min_edge()) else "research",
        "ensemble": False,
        "conformal_ok": False,
        "precision_gate": {"ok": False, "note": "cross_sectional_pooled"},
    }
    meta_json = json.dumps(meta)
    
    passed = accuracy >= _v3_min_holdout_acc() and edge >= _v3_min_edge()
    
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS ghost_v3_model "
                    "(key TEXT PRIMARY KEY, value TEXT, updated_at BIGINT)")
        cur.execute(
            "INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
            ("model_cs_pooled", model_bytes, int(time.time())),
        )
        cur.execute(
            "INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
            ("meta_cs_pooled", meta_json, int(time.time())),
        )
    
    return model, accuracy, passed


def train_and_validate(symbols_and_types):
    try:
        import pickle, base64
        from core.db import db_conn
    except ImportError as e:
        LOGGER.error("Missing dep: "+str(e)); return None, 0.0, False
    
    label_type = _v3_label_type()
    
    # ── Cross-sectional mode: one pooled model ─────────────────────
    if label_type == "cross_sectional":
        return _train_cross_sectional(symbols_and_types)
    
    # ── Per-symbol mode (tp_sl or direction) ───────────────────────
    total_passed = 0
    research_stored = 0
    details: list = []
    _global_pools = {
        "UP": {"records": []},
        "DOWN": {"records": []},
    }
    clear_ohlcv_cache()
    symbol_delay = _v3_train_symbol_delay_sec()
    for idx, (symbol, asset_type) in enumerate(symbols_and_types):
        symbol_detail = {"symbol": symbol, "asset_type": asset_type}
        try:
            up_rows, down_rows = backtest_symbol(symbol, asset_type)
            min_rows = _min_train_rows()
            # Retry once if both directions are thin
            up_n = len(up_rows) if up_rows else 0
            down_n = len(down_rows) if down_rows else 0
            if up_n < min_rows and down_n < min_rows:
                LOGGER.info(f"RETRAIN [{symbol}]: first pass UP={up_n} DOWN={down_n}, retrying after backoff")
                time.sleep(2.0)
                up_rows, down_rows = backtest_symbol(symbol, asset_type)
                up_n = len(up_rows) if up_rows else 0
                down_n = len(down_rows) if down_rows else 0
            if up_n < min_rows and down_n < min_rows:
                fail_msg = f"n_samples<{min_rows} (UP={up_n} DOWN={down_n})"
                LOGGER.info(
                    f"RETRAIN [{symbol}]: acc=NA edge=NA wf_folds=NA wf_acc_mean=NA wf_edge_mean=NA wf_acc_min=NA "
                    f"| FAIL: {fail_msg}"
                )
                symbol_detail.update({
                    "passed": False, "fail_reason": fail_msg,
                    "n_samples": max(up_n, down_n), "stage": "pre_train",
                })
                details.append(symbol_detail)
                continue
            active_cols = _active_feature_cols()
            # Peer pools are per-direction: UP labels come from long-side
            # triple-barrier outcomes, DOWN labels from short-side. Mixing
            # them would contaminate the DOWN model with UP-labeled rows.
            peer_pools, peers_used = ({"UP": [], "DOWN": []}, [])
            if _v3_pool_training_enabled():
                peer_pools, peers_used = _collect_peer_rows(symbol)
            pool_info = {
                "enabled": _v3_pool_training_enabled(),
                "peer_sample_count": int(len(peer_pools.get("UP") or [])),
                "peer_sample_count_down": int(len(peer_pools.get("DOWN") or [])),
                "peers": peers_used,
                "wolf_sample_weight": _v3_wolf_sample_weight(),
            }
            # Train UP model
            up_passed = False
            up_detail = {}
            if up_n >= min_rows:
                up_passed, up_detail, up_model_bytes, up_meta = _train_one_direction(
                    up_rows, symbol, "UP", active_cols,
                    peer_pools.get("UP") or [], peers_used, pool_info)
                up_stored = bool(
                    up_model_bytes
                    and _store_direction_model(symbol, "UP", up_model_bytes, up_meta)
                )
                if up_stored:
                    invalidate_model_cache(symbol)
                    if up_passed:
                        total_passed += 1
                    else:
                        research_stored += 1
                        if isinstance(up_detail, dict):
                            up_detail["research_stored"] = True
            # Train DOWN model
            down_passed = False
            down_detail = {}
            if down_n >= min_rows:
                down_passed, down_detail, down_model_bytes, down_meta = _train_one_direction(
                    down_rows, symbol, "DOWN", active_cols,
                    peer_pools.get("DOWN") or [], peers_used, pool_info)
                down_stored = bool(
                    down_model_bytes
                    and _store_direction_model(symbol, "DOWN", down_model_bytes, down_meta)
                )
                if down_stored:
                    invalidate_model_cache(symbol)
                    if down_passed:
                        total_passed += 1
                    else:
                        research_stored += 1
                        if isinstance(down_detail, dict):
                            down_detail["research_stored"] = True
            # Pool gate-slice OOS predictions for the global operating point
            # (only from models that passed quality gates and were persisted),
            # then drop the bulky arrays from the persisted detail blob.
            for _dirname, _det, _ok, _meta_json in (
                ("UP", up_detail, up_passed, up_meta if up_n >= min_rows else None),
                ("DOWN", down_detail, down_passed, down_meta if down_n >= min_rows else None),
            ):
                _oos = _det.pop("gate_oos", None) if isinstance(_det, dict) else None
                if _ok and _oos and _oos.get("probs") and _meta_json:
                    _meta = json.loads(_meta_json)
                    _model_sha = str(_meta.get("model_sha256") or "")
                    timestamps = _oos.get("timestamps") or [0] * len(_oos["probs"])
                    _global_pools[_dirname]["records"].extend(
                        (int(ts), float(prob), int(label), _model_sha)
                        for ts, prob, label in zip(
                            timestamps, _oos["probs"], _oos["labels"]
                        )
                    )
            # Build combined detail
            symbol_detail.update({
                "passed": up_passed or down_passed,
                "stage": "trained",
                "directions": {
                    "UP": up_detail,
                    "DOWN": down_detail,
                },
            })
            details.append(symbol_detail)
        except Exception as e:
            LOGGER.warning(f"Training failed {symbol}: {e}")
            symbol_detail.update({
                "passed": False, "fail_reason": "exception: " + str(e)[:200],
                "stage": "exception",
            })
            details.append(symbol_detail)
        if symbol_delay > 0 and idx + 1 < len(symbols_and_types):
            time.sleep(symbol_delay)
    LOGGER.info(f"v3.2 training: {total_passed}/{len(symbols_and_types)*2} direction-models passed"
                + (f" (+{research_stored} research-tier stored for shadow evidence)" if research_stored else ""))
    # Cross-symbol precision operating point: pooled gate-slice OOS predictions
    # give the statistical power per-symbol slices lack. Only meaningful on
    # multi-symbol sweeps; single-symbol retrains keep the existing thresholds.
    if len(symbols_and_types) >= 5:
        try:
            from core.precision_gate import store_global_thresholds
            store_global_thresholds(
                _global_pools,
                validation_schema=_v3_validation_schema(),
                label_schema=_v3_label_schema(),
                feature_schema=_v3_feature_schema(),
            )
        except Exception as _gt_e:
            LOGGER.warning("global precision threshold update failed: %s", str(_gt_e)[:120])
    _persist_train_details(details)
    return None, total_passed / max(len(symbols_and_types) * 2, 1), total_passed > 0


def _exact_nonnegative_int(value: Any) -> Optional[int]:
    """Parse metadata counters without accepting booleans or truncation."""
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric) or numeric < 0 or not numeric.is_integer():
        return None
    return int(numeric)


def _model_metric_values(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return bounded finite serving metrics, or None for malformed metadata."""
    values: Dict[str, Any] = {}
    bounds = {
        "accuracy": (0.0, 1.0),
        "edge": (-1.0, 1.0),
        "wf_acc_mean": (0.0, 1.0),
        "wf_edge_mean": (-1.0, 1.0),
    }
    for key, (lower, upper) in bounds.items():
        if isinstance(meta.get(key), bool):
            return None
        try:
            value = float(meta[key])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(value) or not lower <= value <= upper:
            return None
        values[key] = value
    folds = _exact_nonnegative_int(meta.get("wf_fold_count"))
    if folds is None:
        return None
    values["wf_fold_count"] = folds
    for key in ("natural_rate", "no_skill_accuracy"):
        if key not in meta:
            continue
        if isinstance(meta.get(key), bool):
            return None
        try:
            value = float(meta[key])
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            return None
        values[key] = value
    return values


def model_serve_guard(
    meta: Optional[Dict[str, Any]], *, expected_direction: Optional[str] = None,
    allow_research_scoring: bool = False,
) -> Optional[str]:
    """Return a reject code if model metadata is incomplete or stale.

    Research artifacts may be loaded only to emit shadow probabilities; the
    independent lane gate still blocks them from ever producing a live signal.
    """
    if not meta or not isinstance(meta, dict):
        return "missing_meta"
    tier = str(meta.get("tier") or "")
    if tier != "proven" and not (allow_research_scoring and tier == "research"):
        return "tier_unproven"
    direction = str(meta.get("direction") or "").upper()
    if direction not in ("UP", "DOWN"):
        return "direction_missing"
    if expected_direction and direction != str(expected_direction).upper():
        return "direction_mismatch"
    model_sha = str(meta.get("model_sha256") or "").strip().lower()
    if len(model_sha) != 64 or any(ch not in "0123456789abcdef" for ch in model_sha):
        return "model_sha256_invalid"
    if meta.get("label_type") != LABEL_TYPE:
        return "label_type_mismatch"
    if meta.get("label_schema") != _v3_label_schema():
        return "label_schema_stale"
    if meta.get("feature_schema") != _v3_feature_schema():
        return "feature_schema_stale"
    if meta.get("validation_schema") != _v3_validation_schema():
        return "validation_schema_stale"
    try:
        hold_bars = _exact_nonnegative_int(meta.get("label_hold_bars"))
        if hold_bars is None or hold_bars != int(V3_LABEL_HOLD_BARS):
            return "label_hold_bars_stale"
        trained_at = float(meta.get("trained_at"))
    except (TypeError, ValueError, OverflowError):
        return "metadata_numeric_invalid"
    now = time.time()
    if not math.isfinite(trained_at) or trained_at <= 0:
        return "metadata_numeric_invalid"
    if trained_at > now + 300:
        return "trained_at_future"
    if now - trained_at > 14 * 86400:
        return "model_expired"
    if _model_metric_values(meta) is None:
        return "model_metrics_invalid"
    return None


def load_model(symbol=None, direction="UP"):
    """Load a trained model for symbol+direction. direction is 'UP' or 'DOWN' (Phase 2).

    Results (including negative lookups) are cached per (symbol, direction)
    for V3_MODEL_CACHE_TTL_S; retrain/delete paths call
    invalidate_model_cache(). The serve guard (model_expired etc.) is
    re-evaluated on every call, so a cached model can still age out.
    """
    if not symbol: return None, None, None
    ttl = _model_cache_ttl_s()
    cache_key = (str(symbol).upper(), str(direction).upper())
    if ttl > 0:
        with _MODEL_CACHE_LOCK:
            hit = _MODEL_CACHE.get(cache_key)
        if hit is not None:
            model, feature_cols, meta, cached_at = hit
            if time.time() - cached_at < ttl:
                # Serve guard re-check: a model cached fresh can expire mid-TTL.
                if meta is not None and model_serve_guard(
                    meta, expected_direction=direction, allow_research_scoring=True,
                ):
                    return None, None, None
                return model, feature_cols, meta
    model, feature_cols, meta = _load_model_uncached(symbol, direction)
    if ttl > 0:
        with _MODEL_CACHE_LOCK:
            if len(_MODEL_CACHE) > 256:  # safety bound; universe is ~90 keys
                _MODEL_CACHE.clear()
            _MODEL_CACHE[cache_key] = (model, feature_cols, meta, time.time())
    return model, feature_cols, meta


def _load_model_uncached(symbol, direction="UP"):
    try:
        import pickle, base64, binascii, hashlib
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            model_key = f"model_{symbol}_{direction.lower()}"
            meta_key = f"meta_{symbol}_{direction.lower()}"
            cur.execute("SELECT value FROM ghost_v3_model WHERE key=%s", (meta_key,))
            mrow = cur.fetchone()
            if not mrow:
                # Fallback: legacy keys without direction suffix (pre-Phase 2)
                if direction == "UP":
                    cur.execute("SELECT value FROM ghost_v3_model WHERE key=%s", (f"meta_{symbol}",))
                    mrow = cur.fetchone()
                    if mrow:
                        model_key = f"model_{symbol}"
                        meta_key = f"meta_{symbol}"
                if not mrow:
                    return None, None, None
            meta = json.loads(mrow[0])
            reject = model_serve_guard(
                meta, expected_direction=direction, allow_research_scoring=True,
            )
            if reject:
                LOGGER.info("load_model %s/%s: rejected (%s)", symbol, direction, reject)
                return None, None, None
            cur.execute("SELECT value FROM ghost_v3_model WHERE key=%s", (model_key,))
            row = cur.fetchone()
            if not row: return None, None, None
            encoded = row[0] or ""
            if len(encoded) > _model_payload_max_bytes() * 2:
                LOGGER.warning("load_model %s/%s: rejected model payload too large (encoded=%s)", symbol, direction, len(encoded))
                return None, None, None
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as dec_exc:
                LOGGER.warning("load_model %s/%s: invalid base64 payload: %s", symbol, direction, str(dec_exc)[:80])
                return None, None, None
            if len(raw) > _model_payload_max_bytes():
                LOGGER.warning("load_model %s/%s: rejected model payload too large (bytes=%s)", symbol, direction, len(raw))
                return None, None, None
            expected_sha = str(meta.get("model_sha256") or "").strip().lower()
            if expected_sha:
                actual_sha = hashlib.sha256(raw).hexdigest()
                if actual_sha != expected_sha:
                    LOGGER.warning("load_model %s/%s: model sha256 mismatch", symbol, direction)
                    return None, None, None
            else:
                LOGGER.info("load_model %s/%s: legacy model without sha256 metadata", symbol, direction)
            model = pickle.loads(raw)
            return model, meta.get('feature_cols', FEATURE_COLS), meta
    except Exception as e:
        LOGGER.warning(f"load_model {symbol}/{direction}: {e}"); return None, None, None

def predict_live_ex(symbol, asset_type, scores=None, research_mode=False):
    """
    Like predict_live but returns (signal_tuple_or_None, reason_code_or_None).
    reason_code is for diagnostics/metrics only.

    If a mutable `scores` dict is passed, it is populated on the success path
    with the specialist score vector + regime-at-issuance (blueprint §4: the
    pick journal must capture the full score vector, not just the outcome).
    Callers that omit it are unaffected.

    research_mode=True lowers the min_win_proba threshold to 0.40 so the
    engine can fire low-confidence picks when the system has too few resolved
    outcomes to prove edge (research pick mode, PR #114).
    """
    # Phase 2: load both UP and DOWN models. UP is the production lane; DOWN
    # is scored for the journal (shadow) and can only fire when explicitly
    # enabled via V3_DOWN_SIGNALS_ENABLED (load_model handles legacy keys).
    up_model, up_feature_cols, up_meta = load_model(symbol, "UP")
    down_model, down_feature_cols, down_meta = load_model(symbol, "DOWN")
    if up_model is None and down_model is None:
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
    attach_feature_asof(
        features, rows[-1].get("ts") if rows else None, default_now=True,
    )

    # P2 (audit): cross-sectional rank features — percentile within watchlist
    try:
        from core.cross_sectional import compute_cross_sectional, CS_FEATURE_NAMES
        _all_sym_feats = getattr(predict_live_ex, '_last_scan_features', None)
        if _all_sym_feats and symbol in _all_sym_feats:
            cs = compute_cross_sectional(symbol, features, _all_sym_feats)
            for k, v in cs.items():
                features[k] = v
    except Exception:
        note_suppressed()  # P3 (audit): macro regime features — VIX, yield curve, Fed rate, etc.
    try:
        from core.macro_regime import get_macro_features, MACRO_FEATURE_NAMES
        macro = get_macro_features()
        for k, v in macro.items():
            features[k] = v
    except Exception:
        note_suppressed()  # W3: same point-in-time sector relative strength as training, for the
    # current (last) bar. Only when enabled, matching the persisted feature set.
    if _v3_sector_feature_enabled():
        aligned_sector = _align_sector_closes(rows, _fetch_sector_series())
        features["sector_rel_strength"] = _sector_rel_at(
            rows, aligned_sector, len(rows) - 1, _v3_sector_lookback()
        )
    # PR #165: same point-in-time fundamentals as training, as of the last bar.
    try:
        from core.fundamental_features import enabled as _fund_on
        if _fund_on():
            from core.fundamental_features import get_fundamental_features_for_date
            _fund_date = str(rows[-1].get("ts", ""))[:10] if rows else ""
            if _fund_date:
                features.update(get_fundamental_features_for_date(symbol, _fund_date))
    except Exception:
        note_suppressed()

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
    adx_thresh = _v3_adx_trending_threshold()
    if above_ema200 == 0 and adx_trending == 0:
        LOGGER.info(f"REGIME GATE [{symbol}]: below EMA200 + ADX={adx_val:.1f}<{adx_thresh:.0f} — skip BUY")
        regime_block = True

    # Gate 2: full bearish alignment, not oversold
    if not regime_block and ema_trend_bullish == 0 and rsi > 40 and stoch_k > 30:
        LOGGER.info(f"REGIME GATE [{symbol}]: bearish EMA stack, RSI={rsi:.1f} not oversold — skip")
        regime_block = True

    # Gate 3: price below 5-day SMA. Only checked when gates 1-2 passed (the
    # SMA fetch costs a daily-bars call; preserves the original early-out cost).
    # Can be disabled via V3_BLOCK_UP_BELOW_SMA5=0 for choppy markets where
    # SMA5 gate blocks too many candidates.
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
                note_suppressed()
            if not bypass:
                LOGGER.info(
                    f"REGIME GATE [{symbol}]: price {cur_px:.2f} below 5d SMA {sma_5d:.2f} — skip BUY"
                )
                regime_block = True

    # UP and DOWN are independently trained and may persist different sign
    # maps. Apply each lane's own map without mutating the raw journal vector.
    from core.feature_audit import apply_inversions_to_features
    up_features = apply_inversions_to_features(
        dict(features), (up_meta or {}).get("feature_inversions") or [],
    )
    down_features = apply_inversions_to_features(
        dict(features), (down_meta or {}).get("feature_inversions") or [],
    )
    X_up = np.array([[
        up_features.get(c, 0.0) for c in (up_feature_cols or FEATURE_COLS)
    ]])
    X_down = np.array([[
        down_features.get(c, 0.0) for c in (down_feature_cols or FEATURE_COLS)
    ]])

    # Phase 2: score both UP and DOWN models, pick the stronger signal
    up_prob = None
    down_prob = None
    if up_model is not None:
        try:
            up_proba = up_model.predict_proba(X_up)[0]
            up_prob = float(up_proba[1])
        except Exception:
            note_suppressed()
    if down_model is not None:
        try:
            down_proba = down_model.predict_proba(X_down)[0]
            down_prob = float(down_proba[1])
        except Exception:
            note_suppressed()
    if up_prob is None and down_prob is None:
        return None, "no_model"

    # Journal record: the raw stronger signal, independent of firing rules —
    # shadow analysis needs to see when DOWN outscored UP even though only the
    # UP lane can fire in production.
    if up_prob is not None and (down_prob is None or up_prob >= down_prob):
        journal_direction = "UP"
    else:
        journal_direction = "DOWN"

    # Firing lanes: UP is production (identical to pre-Phase 2 behavior).
    # DOWN fires only when V3_DOWN_SIGNALS_ENABLED=1 and the UP lane did not
    # fire. Meta gates are evaluated per-lane against that lane's own model.
    primary_meta = (up_meta or {}) if up_prob is not None else (down_meta or {})
    primary_prob = up_prob if up_prob is not None else down_prob
    primary_direction = "UP" if up_prob is not None else "DOWN"

    min_edge = _v3_min_edge()
    min_acc = _v3_min_holdout_acc()
    min_p = _v3_min_win_proba()
    if research_mode:
        min_p = 0.40  # research: lower bar to let low-confidence signals through
    if scores is not None and isinstance(scores.get("regime"), dict):
        try:
            from core.regime_calibration import effective_min_win_proba, regime_calibration_meta
            rl = scores["regime"].get("label")
            min_p = effective_min_win_proba(rl, base=min_p)
            scores["regime_calibration"] = regime_calibration_meta(rl, base=_v3_min_win_proba())
        except Exception:
            note_suppressed()
    min_wf_acc = _v3_min_wf_acc_mean()
    edge = primary_meta.get('edge', 0)
    wf_acc_mean = float(primary_meta.get("wf_acc_mean", primary_meta.get("accuracy", 0)))
    wf_edge_mean = float(primary_meta.get("wf_edge_mean", primary_meta.get("edge", 0)))
    wf_fold_count = int(primary_meta.get("wf_fold_count", 0))
    accuracy = primary_meta.get('accuracy', min_acc)

    # Capture the model score vector NOW — before the meta gates and the prob_low
    # return — so /api/wolf/gate-status can show where probabilities landed.
    if scores is not None:
        fires = primary_prob > min_p
        # Persist exact classifier probabilities because they define membership
        # in certified threshold populations. Round only presentation fields.
        scores["up_prob"] = float(up_prob) if up_prob is not None else None
        scores["down_prob"] = float(down_prob) if down_prob is not None else None
        scores["winning_direction"] = journal_direction
        scores["win_prob"] = float(primary_prob)
        scores["down_signals_enabled"] = _v3_down_signals_enabled()
        scores["model_identity_by_direction"] = {
            direction: {
                "model_sha256": meta.get("model_sha256"),
                "feature_schema": meta.get("feature_schema"),
                "label_schema": meta.get("label_schema"),
                "validation_schema": meta.get("validation_schema"),
                "hold_bars": meta.get("label_hold_bars"),
            }
            for direction, meta in (("UP", up_meta or {}), ("DOWN", down_meta or {}))
            if meta
        }
        scores["specialists"] = {
            "daily_swing": {
                "model": "xgboost_v3",
                "up_prob": round(up_prob, 4) if up_prob is not None else None,
                "down_prob": round(down_prob, 4) if down_prob is not None else None,
                "vote": primary_direction if fires else "none",
            },
        }
        scores["specialist_count"] = 1
        scores["specialist_agree_up"] = 1 if (fires and primary_direction == "UP") else 0
        scores["model_meta"] = {
            "direction": primary_direction,
            "accuracy": round(float(accuracy), 4),
            "edge": round(float(edge), 4),
            "wf_acc_mean": round(wf_acc_mean, 4),
            "wf_edge_mean": round(wf_edge_mean, 4),
            "wf_fold_count": wf_fold_count,
            "min_win_proba": round(float(min_p), 4),
            "calibrated": bool(primary_meta.get("calibrated", False)),
            "calibration_method": primary_meta.get("calibration_method"),
            "gate_brier": primary_meta.get("gate_brier"),
            "conformal_ok": bool(primary_meta.get("conformal_ok", False)),
            "conformal_q_hat": primary_meta.get("conformal_q_hat"),
            "ensemble": bool(primary_meta.get("ensemble", False)),
        }
        scores["direction_source"] = "classifier_dual"
        scores["trade_signal_source"] = "classifier_dual"

    def _evaluate_lane(direction, prob, meta):
        """Meta + probability gates for one direction lane. Regime gates are
        BUY-only, so only the UP lane can be regime-blocked."""
        # Research-tier hard block (belt-and-braces): these models exist ONLY
        # to give shadow evals an up_prob source; they can never emit a real
        # signal, regardless of what any floor arithmetic below would say.
        if str(meta.get("tier") or "") == "research":
            return None, "research_tier"
        if direction == "UP" and regime_block:
            return None, "regime_gate"
        metric_values = _model_metric_values(meta)
        if metric_values is None:
            return None, "meta_invalid"
        try:
            lane_prob = float(prob)
        except (TypeError, ValueError, OverflowError):
            return None, "prob_invalid"
        if not math.isfinite(lane_prob) or not 0.0 <= lane_prob <= 1.0:
            return None, "prob_invalid"
        lane_edge = metric_values["edge"]
        lane_wf_acc = metric_values["wf_acc_mean"]
        lane_wf_edge = metric_values["wf_edge_mean"]
        lane_folds = metric_values["wf_fold_count"]
        lane_accuracy = metric_values["accuracy"]
        if lane_edge < min_edge - 0.001:
            return None, "meta_gate"
        if lane_accuracy < min_acc - 0.001:
            return None, "meta_gate"
        if lane_folds > 0 and (lane_wf_acc < min_wf_acc - 0.001 or lane_wf_edge < min_edge - 0.001):
            return None, "meta_gate"
        # Phase 3: precision-targeted fire threshold — the 70% contract.
        eff_min_p = min_p
        from core.accuracy_contract import research_bypasses_precision_gate
        from core.precision_gate import (
            global_fallback_enabled,
            load_global_threshold,
            precision_gate_enabled,
            validate_fire_proof,
        )
        enforce_precision = precision_gate_enabled() and (
            not research_mode or not research_bypasses_precision_gate()
        )
        if enforce_precision:
            pg = meta.get("precision_gate") or {}
            source = "symbol"
            if not validate_fire_proof(pg) and global_fallback_enabled():
                g = load_global_threshold(
                    direction, model_sha256=meta.get("model_sha256"),
                )
                if g and g.get("ok"):
                    pg = g
                    source = "global_pool"
            proof_ok = source == "global_pool" or validate_fire_proof(pg)
            if scores is not None:
                scores["precision_gate_" + direction.lower()] = {
                    "ok": proof_ok,
                    "threshold": pg.get("threshold"),
                    "target": pg.get("target"),
                    "source": source if proof_ok else None,
                    "fail_reason": pg.get("fail_reason"),
                }
            if not proof_ok:
                return None, "precision_unproven"
            try:
                proof_threshold = float(pg["threshold"])
            except (KeyError, TypeError, ValueError, OverflowError):
                return None, "precision_unproven"
            if not math.isfinite(proof_threshold):
                return None, "precision_unproven"
            eff_min_p = max(min_p, proof_threshold)
        if lane_prob >= eff_min_p:
            # Use the same inclusive boundary as certification (prob >= t), so
            # the served picks are exactly members of the proven population.
            # PR #155: proven-skill blocker. Even a model with a calibrated
            # threshold can be a base-rate rider or symbol-specific
            # overconfidence (GME/NOK/XPO class). Only once the model would
            # otherwise fire do we require the symbol's forward shadow track
            # record to clear a basic TP-rate + expectancy bar. This preserves
            # precise diagnostics: below-threshold signals still return
            # prob_low, while otherwise-valid fires can be blocked as
            # skill_unproven. This gate only tightens; it never touches shadow
            # scoring, wallet research, or research-mode probes.
            if not research_mode:
                try:
                    from core.proven_skill_gate import global_calibration_review, symbol_review
                    identity = {
                        "direction": direction,
                        "model_sha256": meta.get("model_sha256"),
                        "feature_schema": meta.get("feature_schema"),
                        "label_schema": meta.get("label_schema"),
                        "validation_schema": meta.get("validation_schema"),
                        "hold_bars": meta.get("label_hold_bars"),
                    }
                    skill = symbol_review(symbol, **identity)
                except Exception as _skill_e:
                    skill = {"ok": False, "symbol": symbol, "fail_reason": "skill_exception",
                             "error": str(_skill_e)[:120]}
                if scores is not None:
                    scores["proven_skill_gate_" + direction.lower()] = skill
                if not skill.get("ok"):
                    return None, "skill_unproven"
                try:
                    cal_gate = global_calibration_review(lane_prob, **identity)
                except Exception as _cal_e:
                    cal_gate = {"ok": False, "fail_reason": "calibration_exception",
                                "error": str(_cal_e)[:120]}
                if scores is not None:
                    scores["overconfidence_gate_" + direction.lower()] = cal_gate
                if not cal_gate.get("ok"):
                    return None, "calibration_unproven"
                # PR #162: live per-bin recalibration — shrink the model prob
                # toward its Watcher bin's realized live win rate before
                # computing confidence, so a persistently inverted bin lowers
                # the probability itself instead of relying solely on the
                # PR #156 block. LIVE fires only: research/shadow probes keep
                # RAW probs — the evidence stream this layer feeds on must
                # stay unadjusted or the loop would eat its own output.
                try:
                    from core.live_recalibration import live_recalibrated_prob
                    recal = live_recalibrated_prob(lane_prob, **identity)
                except Exception as _recal_e:
                    recal = {"applied": False, "prob_raw": lane_prob,
                             "prob_adjusted": lane_prob,
                             "error": str(_recal_e)[:120]}
                if scores is not None:
                    scores["live_recalibration_" + direction.lower()] = recal
                adjusted = recal.get("prob_adjusted")
                try:
                    prob_adj = float(lane_prob if adjusted is None else adjusted)
                except (TypeError, ValueError, OverflowError):
                    return None, "live_recal_prob_invalid"
                if not math.isfinite(prob_adj) or not 0.0 <= prob_adj <= 1.0:
                    return None, "live_recal_prob_invalid"
                if prob_adj < eff_min_p:
                    return None, "live_recal_prob_low"
                lane_prob = prob_adj
            q_hat = meta.get("conformal_q_hat")
            if q_hat is not None and float(q_hat) > 0:
                from core.conformal_calibration import conformal_confidence
                conf = conformal_confidence(
                    lane_prob, float(q_hat), lane_accuracy, float(min_p))
            else:
                conf = round(min(0.98, max(0.0, lane_prob)), 3)
            if scores is not None:
                scores["confidence"] = conf
                if q_hat is not None:
                    scores["conformal_q_hat"] = float(q_hat)
            return (direction, conf), None
        return None, "prob_low"

    # UP lane first — a blocked/weaker DOWN score never suppresses UP.
    reason = "no_model"
    if up_prob is not None:
        sig, reason = _evaluate_lane("UP", up_prob, up_meta or {})
        if sig:
            return sig, None

    # DOWN lane: shadow-only unless explicitly enabled.
    if down_prob is not None and _v3_down_signals_enabled():
        d_sig, d_reason = _evaluate_lane("DOWN", down_prob, down_meta or {})
        if d_sig:
            return d_sig, None
        if up_prob is None:
            reason = d_reason
    elif up_prob is None:
        # Only a DOWN model exists and the lane is disabled — nothing can fire.
        reason = "down_shadow_only"
    return None, reason


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
            skill_cache = {}

            def _status_float(value: Any, default: float = 0.0) -> float:
                try:
                    parsed = float(value)
                except (TypeError, ValueError, OverflowError):
                    return default
                return parsed if math.isfinite(parsed) else default

            def _status_precision_review(lane: str, meta: dict) -> dict:
                """Use the same exact proof and global fallback as runtime."""
                from core.precision_gate import (
                    global_fallback_enabled, load_global_threshold,
                    precision_gate_enabled, validate_fire_proof,
                )
                if not precision_gate_enabled():
                    return {"ok": True, "disabled": True, "source": "disabled"}
                proof = meta.get("precision_gate") or {}
                if validate_fire_proof(proof):
                    return {**proof, "ok": True, "source": "symbol"}
                if global_fallback_enabled():
                    pooled = load_global_threshold(
                        lane, model_sha256=meta.get("model_sha256"),
                    )
                    if pooled and pooled.get("ok") is True:
                        return {**pooled, "ok": True, "source": "global_pool"}
                return {
                    "ok": False,
                    "source": None,
                    "fail_reason": proof.get("fail_reason") or "proof_invalid",
                }

            def _status_skill_review(symbol_name: str, lane: str, meta: dict) -> dict:
                """Mirror the identity-bound runtime proven-skill gate."""
                sym_u = (symbol_name or "").upper()
                identity = (
                    sym_u, lane, meta.get("model_sha256"),
                    meta.get("feature_schema"), meta.get("label_schema"),
                    meta.get("validation_schema"), meta.get("label_hold_bars"),
                )
                if identity in skill_cache:
                    return skill_cache[identity]
                try:
                    from core.proven_skill_gate import enabled as _skill_enabled, review as _skill_review
                    if not _skill_enabled():
                        out = {"ok": True, "disabled": True, "symbol": sym_u}
                    else:
                        cur.execute(
                            """
                            SELECT
                              SUM(CASE WHEN outcome IN ('WIN','LOSS','EXPIRED') THEN 1 ELSE 0 END) AS resolved,
                              SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins,
                              AVG(CASE WHEN outcome IN ('WIN','LOSS','EXPIRED') THEN pnl_pct ELSE NULL END) AS avg_pnl
                            FROM ghost_shadow_outcomes
                            WHERE symbol=%s AND direction=%s AND model_sha256=%s
                              AND feature_schema=%s AND label_schema=%s
                              AND validation_schema=%s AND hold_bars=%s
                              AND outcome IN ('WIN','LOSS','EXPIRED')
                            """,
                            identity,
                        )
                        row = cur.fetchone()
                        out = _skill_review(
                            sym_u,
                            resolved=int((row and row[0]) or 0),
                            wins=int((row and row[1]) or 0),
                            avg_pnl_pct=(row[2] if row else None),
                        )
                except Exception as _skill_e:
                    out = {"ok": False, "symbol": sym_u, "fail_reason": "skill_unavailable",
                           "error": str(_skill_e)[:120]}
                skill_cache[identity] = out
                return out
            for key, val in rows:
                raw_key = key.replace('meta_', '')
                # Phase 2: directional keys like WOLF_up, WOLF_down
                # Legacy keys: just WOLF
                if raw_key.endswith('_up'):
                    sym = raw_key[:-3]
                    direction = "UP"
                elif raw_key.endswith('_down'):
                    sym = raw_key[:-5]
                    direction = "DOWN"
                else:
                    sym = raw_key
                    direction = "UP"  # legacy models are UP-only
                m = json.loads(val)
                reject = model_serve_guard(m, expected_direction=direction)
                if raw_key not in model_keys:
                    reject = reject or "missing_pickle"
                precision_review = _status_precision_review(direction, m)
                nat_display = _status_float(m.get("natural_rate"))
                no_skill_display = _status_float(
                    m.get("no_skill_accuracy"), max(nat_display, 1.0 - nat_display),
                )
                summary = {
                    "direction": direction,
                    "accuracy": round(_status_float(m.get("accuracy"))*100,1),
                    "natural_rate": round(nat_display*100,1),
                    "no_skill_accuracy": round(no_skill_display*100, 1),
                    "edge": round(_status_float(m.get("edge"))*100,1),
                    "wf_acc_mean": round(_status_float(m.get("wf_acc_mean"))*100,1),
                    "wf_acc_min": round(_status_float(m.get("wf_acc_min"))*100,1),
                    "wf_edge_mean": round(_status_float(m.get("wf_edge_mean"))*100,1),
                    "wf_edge_min": round(_status_float(m.get("wf_edge_min"))*100,1),
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
                    "precision_ok": precision_review.get("ok") is True,
                    "precision_source": precision_review.get("source"),
                    "fire_threshold": precision_review.get("threshold"),
                    "serveable": reject is None,
                    "tier": m.get("tier", "proven"),
                }
                if reject:
                    summary["serve_reject"] = reject
                # ── Honesty fields (PR #135 audit) ──────────────────────────
                # serveable only means "pickle loads and schema matches".
                # fireable_now mirrors the STATIC checks of the runtime fire
                # chain (_evaluate_lane): the model could fire if the tape
                # cooperates. The audit found 11 precision_ok models displayed
                # while 0 could actually fire — that gap must be visible.
                metric_values = _model_metric_values(m)
                acc_f = (metric_values or {}).get("accuracy", 0.0)
                nat_f = _status_float(m.get("natural_rate"))
                no_skill_f = _status_float(
                    m.get("no_skill_accuracy"), max(nat_f, 1.0 - nat_f),
                )
                edge_f = (metric_values or {}).get("edge", 0.0)
                wf_edge_f = (metric_values or {}).get("wf_edge_mean", 0.0)
                wf_acc_f = (metric_values or {}).get("wf_acc_mean", 0.0)
                folds = (metric_values or {}).get("wf_fold_count", 0)
                block = None
                if reject is not None:
                    block = f"not_serveable:{reject}"
                elif m.get("tier") == "research":
                    block = "research_tier"
                elif direction == "DOWN" and not _v3_down_signals_enabled():
                    block = "down_lane_disabled"
                elif edge_f < _v3_min_edge() - 0.001:
                    block = f"meta_gate:edge {edge_f*100:.1f}% < {_v3_min_edge()*100:.1f}%"
                elif acc_f < _v3_min_holdout_acc() - 0.001:
                    block = f"meta_gate:holdout_acc {acc_f*100:.1f}% < {_v3_min_holdout_acc()*100:.1f}%"
                elif folds > 0 and wf_acc_f < _v3_min_wf_acc_mean() - 0.001:
                    block = f"meta_gate:wf_acc {wf_acc_f*100:.1f}% < {_v3_min_wf_acc_mean()*100:.1f}%"
                elif folds > 0 and wf_edge_f < _v3_min_edge() - 0.001:
                    block = f"meta_gate:wf_edge {wf_edge_f*100:.1f}% < {_v3_min_edge()*100:.1f}%"
                elif not summary["precision_ok"]:
                    block = "precision_unproven"
                else:
                    # PR #155: keep /api/v3/status honest with runtime firing.
                    # If runtime would block an otherwise-valid UP model because
                    # the symbol lacks forward shadow skill, status must not call
                    # it fireable_now. DOWN is shadow-disabled by default above.
                    skill = _status_skill_review(sym, direction, m)
                    summary["proven_skill_gate"] = skill
                    if not skill.get("ok"):
                        block = "skill_unproven"
                summary["fireable_now"] = block is None
                if block:
                    summary["fire_block_reason"] = block
                # Accuracy near majority-class no-skill performance means a
                # constant guess would score the same (base-rate rider).
                summary["no_skill_accuracy"] = round(no_skill_f * 100, 1)
                summary["base_rate_rider"] = bool(acc_f <= no_skill_f + 0.02)
                # skill = beats base rate in-sample AND out-of-time.
                summary["proven_skill"] = bool(edge_f > 0 and wf_edge_f > 0)
                stored[raw_key] = summary
                if reject is None:
                    if sym not in symbols:
                        symbols[sym] = {}
                    symbols[sym][direction] = summary
            # ── Fleet honesty summary (PR #136, live-market audit P4+P5) ──
            fireable = [k for k, v in stored.items() if v.get("fireable_now")]
            riders = [k for k, v in stored.items() if v.get("base_rate_rider")]
            # Watchlist symbols with no serveable model in ANY direction, with
            # the concrete reason (audit P5: "missing" must never be a mystery).
            missing_v3: Dict[str, str] = {}
            try:
                from config.symbols import OFFICIAL_WATCHLIST
                for wsym in OFFICIAL_WATCHLIST:
                    if wsym in symbols:
                        continue
                    rejects = {v.get("serve_reject") for k, v in stored.items()
                               if k == wsym or k.startswith(f"{wsym}_")}
                    rejects.discard(None)
                    missing_v3[wsym] = "; ".join(sorted(rejects)) if rejects else "no_model_stored"
            except Exception:
                note_suppressed()
            out = {
                "trained": bool(symbols),
                "models": sum(len(v) for v in symbols.values()),
                "models_stored": len(stored),
                "fleet_summary": {
                    "serveable": sum(1 for v in stored.values() if v.get("serveable")),
                    "serveable_research": sum(1 for v in stored.values()
                                              if v.get("serveable") and v.get("tier") == "research"),
                    "fireable_now": len(fireable),
                    "fireable_models": fireable,
                    "precision_ok": sum(1 for v in stored.values() if v.get("precision_ok")),
                    "base_rate_riders": len(riders),
                    "proven_skill": sum(1 for v in stored.values() if v.get("proven_skill")),
                    "note": ("fireable_now is the only 'ready' number — serveable means "
                             "the pickle loads, precision_ok alone can ride base rates; "
                             "serveable_research models feed shadow evals and can never fire"),
                },
                "missing_v3": missing_v3,
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
