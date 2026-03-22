"""
scripts/retrain.py - Train XGBoost model on resolved predictions from Postgres.
Run this once to generate models/ghost_v2.json
Then runs automatically every 14 days via scheduler.

Usage:
  python scripts/retrain.py
  python scripts/retrain.py --min-samples 50
"""
import os, sys, time, json, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
LOGGER = logging.getLogger("ghost.retrain")

def load_training_data(min_samples: int = 30):
    """Load resolved predictions from Postgres and build feature matrix."""
    from core.db import init_db, db_conn
    init_db()
    LOGGER.info("Loading training data from Postgres...")
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                p1.symbol,
                p1.direction,
                p1.confidence,
                p1.entry_price,
                p1.target_price,
                p1.stop_price,
                COALESCE(p1.predicted_at, p1.run_at) as ts,
                p1.asset_type,
                p1.outcome,
                p1.pnl_pct
            FROM predictions p1
            WHERE p1.outcome IN ('WIN', 'LOSS')
            AND p1.entry_price IS NOT NULL
            AND p1.entry_price > 0
            ORDER BY ts DESC
            LIMIT 2000
        """)
        rows = cur.fetchall()
    LOGGER.info("Loaded " + str(len(rows)) + " resolved predictions")
    if len(rows) < min_samples:
        LOGGER.error("Not enough data: " + str(len(rows)) + " rows (need " + str(min_samples) + ")")
        return None, None
    # Build per-symbol accuracy stats
    from collections import defaultdict
    sym_stats = defaultdict(lambda: {"wins": 0, "total": 0})
    for row in rows:
        sym = row[0]
        outcome = row[8]
        sym_stats[sym]["total"] += 1
        if outcome == "WIN": sym_stats[sym]["wins"] += 1
    X, y = [], []
    for row in rows:
        symbol, direction, confidence, entry, target, stop, ts, asset_type, outcome, pnl = row
        if not entry or entry <= 0: continue
        if not target or not stop: continue
        # Calculate features
        target_pct = abs(target - entry) / entry if entry else 0.05
        stop_pct = abs(stop - entry) / entry if entry else 0.03
        risk_reward = target_pct / stop_pct if stop_pct > 0 else 1.0
        sym_wr = sym_stats[symbol]["wins"] / sym_stats[symbol]["total"] if sym_stats[symbol]["total"] > 0 else 0.5
        sym_count = sym_stats[symbol]["total"]
        # Time features (if timestamp available)
        hour_of_day = 0
        day_of_week = 0
        if ts:
            import datetime
            dt = datetime.datetime.fromtimestamp(float(ts))
            hour_of_day = dt.hour
            day_of_week = dt.weekday()
        features = [
            float(confidence) if confidence else 0.5,
            1.0 if direction == "UP" else 0.0,
            1.0 if (asset_type or "crypto") == "crypto" else 0.0,
            float(target_pct),
            float(stop_pct),
            float(risk_reward),
            float(sym_wr),
            float(min(sym_count, 100)) / 100,
            float(entry) if entry < 10000 else 10000.0,
            float(hour_of_day) / 24,
            float(day_of_week) / 7,
        ]
        label = 1 if outcome == "WIN" else 0
        X.append(features)
        y.append(label)
    LOGGER.info("Feature matrix: " + str(len(X)) + " samples, " + str(len(X[0]) if X else 0) + " features")
    wins = sum(y)
    LOGGER.info("Class balance: " + str(wins) + " wins, " + str(len(y)-wins) + " losses (" + str(round(wins/len(y)*100,1)) + "% win rate)")
    return X, y

def train_model(X, y, model_path: str = "models/ghost_v2.json") -> dict:
    """Train XGBoost model and save to disk."""
    try:
        import xgboost as xgb
        import numpy as np
    except ImportError:
        LOGGER.error("xgboost or numpy not installed")
        return {}
    X_np = np.array(X)
    y_np = np.array(y)
    # Split: last 20% for validation
    split = int(len(X_np) * 0.8)
    X_train, X_val = X_np[:split], X_np[split:]
    y_train, y_val = y_np[:split], y_np[split:]
    LOGGER.info("Training on " + str(len(X_train)) + " samples, validating on " + str(len(X_val)))
    model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        use_label_encoder=False,
        eval_metric="logloss",
        random_state=42,
    )
    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        verbose=False,
    )
    # Evaluate
    val_preds = model.predict(X_val)
    val_acc = float(np.mean(val_preds == y_val))
    train_preds = model.predict(X_train)
    train_acc = float(np.mean(train_preds == y_train))
    LOGGER.info("Train accuracy: " + str(round(train_acc*100,1)) + "%")
    LOGGER.info("Val accuracy:   " + str(round(val_acc*100,1)) + "%")
    # Save model
    os.makedirs(os.path.dirname(model_path) if os.path.dirname(model_path) else ".", exist_ok=True)
    model.save_model(model_path)
    LOGGER.info("Model saved to " + model_path)
    # Save metadata
    meta = {
        "trained_at": int(time.time()),
        "samples": len(X),
        "train_acc": round(train_acc*100,1),
        "val_acc": round(val_acc*100,1),
        "model_path": model_path,
    }
    meta_path = model_path.replace(".json", "_meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    LOGGER.info("Metadata: train=" + str(meta["train_acc"]) + "% val=" + str(meta["val_acc"]) + "%")
    return meta

def run_retrain(min_samples: int = 30, model_path: str = "models/ghost_v2.json") -> dict:
    """Full retrain pipeline. Called by scheduler every 14 days."""
    LOGGER.info("Starting model retrain...")
    X, y = load_training_data(min_samples)
    if X is None:
        return {"error": "Not enough training data"}
    meta = train_model(X, y, model_path)
    # Invalidate cached model in prediction.py
    try:
        from core import prediction
        prediction._model = None
        LOGGER.info("Prediction model cache cleared - will reload on next prediction")
    except: pass
    return meta

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-samples", type=int, default=30)
    parser.add_argument("--model-path", default="models/ghost_v2.json")
    args = parser.parse_args()
    result = run_retrain(args.min_samples, args.model_path)
    if "error" in result:
        LOGGER.error("Retrain failed: " + result["error"])
        sys.exit(1)
    else:
        LOGGER.info("Retrain complete: " + json.dumps(result))