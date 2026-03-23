"""
scripts/retrain.py - Train XGBoost on v1 ghost_prediction_outcomes table.
Queries directly from the source table, bypassing v1 predictions schema constraints.
"""
import os, sys, time, json, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
LOGGER = logging.getLogger("ghost.retrain")

CRYPTO_SYMBOLS = {"BTC","ETH","SOL","XRP","ADA","DOT","LINK","AVAX","MATIC","LTC","ATOM","UNI","TRX","BCH","CHZ","TURBO","ZEC","RNDR"}

def load_training_data(min_samples=100):
    from core.db import init_db, db_conn
    init_db()
    LOGGER.info("Loading from ghost_prediction_outcomes...")
    with db_conn() as conn:
        cur = conn.cursor()
        # Primary source: ghost_prediction_outcomes (13k rows)
        cur.execute("""
            SELECT
                gpo.symbol,
                COALESCE(gpo.predicted_direction, 'UP') as direction,
                COALESCE(gpo.predicted_confidence, 0.5) as confidence,
                gpo.price_at_prediction as entry_price,
                gpo.price_at_resolution as exit_price,
                gpo.realized_move_pct as pnl_pct,
                EXTRACT(EPOCH FROM gpo.created_at)::BIGINT as ts,
                CASE WHEN gpo.hit_direction = 1 THEN 'WIN' ELSE 'LOSS' END as outcome
            FROM ghost_prediction_outcomes gpo
            WHERE gpo.hit_direction IN (0, 1)
            AND gpo.price_at_prediction IS NOT NULL
            AND gpo.price_at_prediction > 0
            ORDER BY gpo.created_at DESC
            LIMIT 5000
        """)
        rows = cur.fetchall()
    LOGGER.info("Loaded " + str(len(rows)) + " rows from ghost_prediction_outcomes")
    if len(rows) < min_samples:
        LOGGER.error("Not enough data: " + str(len(rows)) + " (need " + str(min_samples) + ")")
        return None, None
    from collections import defaultdict
    sym_stats = defaultdict(lambda: {"wins": 0, "total": 0})
    for row in rows:
        sym, direction, conf, entry, exit_p, pnl, ts, outcome = row
        sym_stats[sym]["total"] += 1
        if outcome == "WIN": sym_stats[sym]["wins"] += 1
    X, y = [], []
    for row in rows:
        sym, direction, conf, entry, exit_p, pnl, ts, outcome = row
        if not entry or entry <= 0: continue
        target_pct = abs(pnl) / 100 if pnl else 0.05
        stop_pct = 0.03
        rr = target_pct / stop_pct if stop_pct > 0 else 1.0
        wr = sym_stats[sym]["wins"] / sym_stats[sym]["total"] if sym_stats[sym]["total"] else 0.5
        sym_count = sym_stats[sym]["total"]
        hour = 0
        dow = 0
        if ts:
            import datetime
            dt = datetime.datetime.fromtimestamp(float(ts))
            hour = dt.hour
            dow = dt.weekday()
        is_crypto = 1.0 if sym in CRYPTO_SYMBOLS else 0.0
        is_major = 1.0 if sym in {"BTC","ETH","XRP","AAPL","NVDA","TSLA"} else 0.0
        features = [
            float(conf),
            1.0 if direction == "UP" else 0.0,
            is_crypto,
            float(target_pct),
            float(stop_pct),
            float(rr),
            float(wr),
            float(min(sym_count, 100)) / 100,
            float(min(entry, 10000)) / 10000,
            float(hour) / 24,
            float(dow) / 7,
        ]
        X.append(features)
        y.append(1 if outcome == "WIN" else 0)
    wins = sum(y)
    LOGGER.info("Features: " + str(len(X)) + " samples | " + str(wins) + "W " + str(len(y)-wins) + "L (" + str(round(wins/len(y)*100,1)) + "%)")
    return X, y

def train_model(X, y, model_path="models/ghost_v2.json"):
    try:
        import xgboost as xgb
        import numpy as np
    except ImportError as e:
        LOGGER.error("Missing dep: " + str(e))
        return {}
    X_np = np.array(X)
    y_np = np.array(y)
    split = int(len(X_np) * 0.8)
    X_train, X_val = X_np[:split], X_np[split:]
    y_train, y_val = y_np[:split], y_np[split:]
    model = xgb.XGBClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        use_label_encoder=False, eval_metric="logloss", random_state=42
    )
    model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)
    val_preds = model.predict(X_val)
    val_acc = float(np.mean(val_preds == y_val))
    train_acc = float(np.mean(model.predict(X_train) == y_train))
    LOGGER.info("Train: " + str(round(train_acc*100,1)) + "% | Val: " + str(round(val_acc*100,1)) + "%")
    os.makedirs(os.path.dirname(model_path) if os.path.dirname(model_path) else ".", exist_ok=True)
    model.save_model(model_path)
    meta = {"trained_at": int(time.time()), "samples": len(X), "train_acc": round(train_acc*100,1), "val_acc": round(val_acc*100,1)}
    with open(model_path.replace(".json","_meta.json"), "w") as f: json.dump(meta, f)
    LOGGER.info("Model saved: " + model_path)
    return meta

def run_retrain(min_samples=100, model_path="models/ghost_v2.json"):
    X, y = load_training_data(min_samples)
    if X is None: return {"error": "Not enough data"}
    meta = train_model(X, y, model_path)
    try:
        from core import prediction
        prediction._model = None
    except: pass
    return meta

if __name__ == "__main__":
    result = run_retrain()
    print(json.dumps(result, indent=2))