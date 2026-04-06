"""
core/model.py
Ghost Protocol — XGBoost signal model.

Training: called by weekly scheduler once >=MIN_TRAIN_ROWS resolved
          predictions with features exist in DB.
Inference: loaded by prediction.py at startup; falls back to win-rate
           logic if model file is absent.
"""

import os
import json
import logging
import pickle
import time
import numpy as np

LOGGER = logging.getLogger("ghost.model")

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models", "ghost_xgb.pkl")
MIN_TRAIN_ROWS = 300   # minimum labeled rows with features before training
_model_cache = None
_model_loaded_at = 0
MODEL_RELOAD_S = 3600  # reload from disk every hour


# ─── Feature schema ────────────────────────────────────────────────────────────
# Must match exactly what prediction.py stores in the features JSONB column.
FEATURE_COLS = [
    "btc_24h_pct",      # BTC price change over last 24h (regime context)
    "hour_of_day",      # UTC hour 0-23
    "day_of_week",      # 0=Mon .. 6=Sun
    "symbol_win_rate",  # historical win rate for this symbol at signal time
    "confidence_raw",   # model confidence before sentiment adjustment
    "sentiment_score",  # news sentiment -1..+1 (0 if unavailable)
    "price_4h_pct",     # symbol price % change vs ~4h ago (from stored entries)
]


def _row_to_features(features_json):
    """Convert a stored features dict to a numpy feature vector."""
    f = features_json if isinstance(features_json, dict) else {}
    return [
        float(f.get("btc_24h_pct", 0.0)),
        float(f.get("hour_of_day", 12)),
        float(f.get("day_of_week", 2)),
        float(f.get("symbol_win_rate", 0.3)),
        float(f.get("confidence_raw", 0.75)),
        float(f.get("sentiment_score", 0.0)),
        float(f.get("price_4h_pct", 0.0)),
    ]


# ─── Inference ─────────────────────────────────────────────────────────────────

def load_model():
    """Load model from disk, cached. Returns model or None."""
    global _model_cache, _model_loaded_at
    now = time.time()
    if _model_cache is not None and (now - _model_loaded_at) < MODEL_RELOAD_S:
        return _model_cache
    path = MODEL_PATH
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            _model_cache = pickle.load(f)
        _model_loaded_at = now
        LOGGER.info("[MODEL] Loaded from " + path)
        return _model_cache
    except Exception as e:
        LOGGER.warning("[MODEL] Load failed: " + str(e))
        return None


def predict_with_model(features_dict):
    """
    Run model inference. Returns (direction, confidence) or None if no model.
    Only fires BUY signals — SELL is disabled until model proves SELL edge.
    """
    model = load_model()
    if model is None:
        return None
    try:
        X = np.array([_row_to_features(features_dict)])
        proba = model.predict_proba(X)[0]
        # Class 1 = WIN, class 0 = LOSS (set during training)
        win_prob = float(proba[1])
        if win_prob >= 0.55:
            return ("UP", round(win_prob, 3))
        return None  # not confident enough
    except Exception as e:
        LOGGER.warning("[MODEL] Inference error: " + str(e))
        return None


# ─── Training ──────────────────────────────────────────────────────────────────

def _load_training_data():
    """Pull labeled rows with features from DB. Returns (X, y, timestamps)."""
    from core.db import db_conn
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT features, outcome, predicted_at
            FROM predictions
            WHERE outcome IN ('WIN','LOSS','STOP')
              AND features IS NOT NULL
              AND direction IN ('UP','BUY')
            ORDER BY predicted_at ASC
        """)
        rows = cur.fetchall()

    X, y, ts = [], [], []
    for features_json, outcome, predicted_at in rows:
        if not features_json:
            continue
        try:
            f = features_json if isinstance(features_json, dict) else json.loads(features_json)
            X.append(_row_to_features(f))
            y.append(1 if outcome == "WIN" else 0)
            ts.append(predicted_at or 0)
        except Exception:
            continue
    return np.array(X), np.array(y), ts


def train_model(force=False):
    """
    Train XGBoost on accumulated labeled data.
    Returns (model, metrics_dict) or (None, reason_str).
    """
    try:
        import xgboost as xgb
        from sklearn.model_selection import TimeSeriesSplit
        from sklearn.metrics import roc_auc_score
    except ImportError as e:
        return None, "missing dependency: " + str(e)

    X, y, ts = _load_training_data()
    n = len(X)

    if n < MIN_TRAIN_ROWS and not force:
        return None, "only " + str(n) + " rows, need " + str(MIN_TRAIN_ROWS)
    if n == 0:
        return None, "no training data"

    wins = int(y.sum())
    losses = n - wins
    LOGGER.info("[MODEL] Training on " + str(n) + " rows (" + str(wins) + "W / " + str(losses) + "L)")
    scale = losses / max(wins, 1)

    model = xgb.XGBClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        scale_pos_weight=scale, subsample=0.8, colsample_bytree=0.8,
        eval_metric="logloss",
        random_state=42, verbosity=0,
    )

    cv_scores = []
    if n >= 100:
        tscv = TimeSeriesSplit(n_splits=min(5, n // 20))
        for train_idx, val_idx in tscv.split(X):
            X_tr, X_val = X[train_idx], X[val_idx]
            y_tr, y_val = y[train_idx], y[val_idx]
            if len(np.unique(y_val)) < 2:
                continue
            m = xgb.XGBClassifier(
                n_estimators=200, max_depth=4, learning_rate=0.05,
                scale_pos_weight=scale, subsample=0.8, colsample_bytree=0.8,
                eval_metric="logloss",
                random_state=42, verbosity=0,
            )
            m.fit(X_tr, y_tr)
            proba = m.predict_proba(X_val)[:, 1]
            cv_scores.append(roc_auc_score(y_val, proba))

    model.fit(X, y)

    importances = dict(zip(FEATURE_COLS, model.feature_importances_.tolist()))
    top = sorted(importances.items(), key=lambda x: x[1], reverse=True)
    LOGGER.info("[MODEL] Feature importances: " + str(top[:4]))

    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(model, f)
    LOGGER.info("[MODEL] Saved to " + MODEL_PATH)

    global _model_cache, _model_loaded_at
    _model_cache = None
    _model_loaded_at = 0

    metrics = {
        "rows": n, "wins": wins, "losses": losses,
        "cv_auc": round(float(np.mean(cv_scores)), 3) if cv_scores else None,
        "feature_importances": dict(top),
    }
    return model, metrics


def retrain_if_ready():
    """Called by weekly scheduler. Trains if enough labeled data."""
    from core.db import db_conn
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) FROM predictions
                WHERE outcome IN ('WIN','LOSS','STOP')
                  AND features IS NOT NULL
                  AND direction IN ('UP','BUY')
            """)
            count = cur.fetchone()[0]
    except Exception as e:
        LOGGER.error("[MODEL] DB count failed: " + str(e))
        return

    LOGGER.info("[MODEL] Labeled rows with features: " + str(count) + " / " + str(MIN_TRAIN_ROWS) + " needed")
    if count < MIN_TRAIN_ROWS:
        return

    model, result = train_model()
    if model is None:
        LOGGER.warning("[MODEL] Training skipped: " + str(result))
    else:
        LOGGER.info("[MODEL] Training complete: " + str(result))
