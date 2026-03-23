"""
scripts/retrain.py - Train XGBoost on ghost_prediction_outcomes.
LESSONS APPLIED:
  - Balanced 50/50 WIN/LOSS sampling (prevents all-DOWN predictions)
  - Last 90 days only (recent market conditions matter more)
  - FEATURE_ORDER exported so prediction.py uses identical features
  - Confidence calibration check after training
"""
import os, sys, time, json, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
LOGGER = logging.getLogger("ghost.retrain")

CRYPTO_SET = {"BTC","ETH","SOL","XRP","ADA","DOT","LINK","AVAX","MATIC","LTC","ATOM","UNI","TRX","BCH","CHZ","TURBO","ZEC","RNDR"}

# Single source of truth for feature order - imported by prediction.py
FEATURE_ORDER = [
    "sym_win_rate",    # Historical win rate for this symbol
    "sym_count_norm",  # Normalized sample count (0-1)
    "is_crypto",       # 1.0 for crypto, 0.0 for stock
    "is_major",        # 1.0 for BTC/ETH/XRP/AAPL/NVDA
    "hour_norm",       # Hour of day normalized (0-1)
    "dow_norm",        # Day of week normalized (0-1)
    "price_norm",      # Price normalized to 0-1 range
    "conf_input",      # Confidence from upstream signal (0-1)
    "direction_up",    # 1.0 if UP, 0.0 if DOWN
    "target_pct",      # Target gain percentage
    "stop_pct",        # Stop loss percentage
]  # 11 features - DO NOT REORDER without retraining

def load_balanced_data(min_samples=200, days=90):
    """Load 90-day balanced 50/50 WIN/LOSS sample from ghost_prediction_outcomes."""
    from core.db import init_db, db_conn
    init_db()
    with db_conn() as conn:
        cur = conn.cursor()
        # Check class balance first
        cur.execute("SELECT hit_direction, COUNT(*) FROM ghost_prediction_outcomes WHERE hit_direction IN (0,1) AND created_at > NOW() - INTERVAL '90 days' GROUP BY hit_direction")
        balance = {r[0]: r[1] for r in cur.fetchall()}
        LOGGER.info("Class balance (90d): " + str(balance))
        cap = min(balance.get(0, 0), balance.get(1, 0), 2500)
        if cap < min_samples:
            LOGGER.error("Not enough balanced data: " + str(cap) + " per class (need " + str(min_samples) + ")")
            return None, None, None
        # Balanced sample: equal wins and losses
        cur.execute("""
            (SELECT gpo.symbol, gpo.predicted_direction, gpo.predicted_confidence,
                    gpo.price_at_prediction, gpo.realized_move_pct,
                    EXTRACT(EPOCH FROM gpo.created_at)::BIGINT as ts,
                    1 as label
             FROM ghost_prediction_outcomes gpo
             WHERE gpo.hit_direction = 1
             AND gpo.created_at > NOW() - INTERVAL '90 days'
             AND gpo.price_at_prediction > 0
             ORDER BY RANDOM() LIMIT %s)
            UNION ALL
            (SELECT gpo.symbol, gpo.predicted_direction, gpo.predicted_confidence,
                    gpo.price_at_prediction, gpo.realized_move_pct,
                    EXTRACT(EPOCH FROM gpo.created_at)::BIGINT as ts,
                    0 as label
             FROM ghost_prediction_outcomes gpo
             WHERE gpo.hit_direction = 0
             AND gpo.created_at > NOW() - INTERVAL '90 days'
             AND gpo.price_at_prediction > 0
             ORDER BY RANDOM() LIMIT %s)
        """, (cap, cap))
        rows = cur.fetchall()
    LOGGER.info("Loaded " + str(len(rows)) + " balanced rows (" + str(cap) + " wins + " + str(cap) + " losses)")
    # Build per-symbol stats from this sample
    from collections import defaultdict
    sym_stats = defaultdict(lambda: {"wins": 0, "total": 0})
    for row in rows:
        sym = row[0]; label = row[6]
        sym_stats[sym]["total"] += 1
        if label == 1: sym_stats[sym]["wins"] += 1
    X, y = [], []
    for sym, direction, conf, price, pnl, ts, label in rows:
        if not price or price <= 0: continue
        wr = sym_stats[sym]["wins"] / sym_stats[sym]["total"] if sym_stats[sym]["total"] else 0.5
        cnt = sym_stats[sym]["total"]
        import datetime
        dt = datetime.datetime.fromtimestamp(float(ts)) if ts else datetime.datetime.now()
        features = [
            float(wr),                                        # sym_win_rate
            float(min(cnt, 200)) / 200,                       # sym_count_norm
            1.0 if sym in CRYPTO_SET else 0.0,                # is_crypto
            1.0 if sym in {"BTC","ETH","XRP","AAPL","NVDA","TSLA"} else 0.0,  # is_major
            float(dt.hour) / 24,                              # hour_norm
            float(dt.weekday()) / 7,                          # dow_norm
            min(float(price), 100000) / 100000,               # price_norm
            float(conf) if conf else 0.5,                     # conf_input
            1.0 if (direction or "UP") == "UP" else 0.0,      # direction_up
            0.06,                                             # target_pct (fixed 6%)
            0.03,                                             # stop_pct (fixed 3%)
        ]
        X.append(features)
        y.append(label)
    wins = sum(y); total = len(y)
    LOGGER.info("Final sample: " + str(wins) + "W/" + str(total-wins) + "L = " + str(round(wins/total*100,1)) + "% win rate")
    return X, y, sym_stats

def train_model(X, y, model_path="/tmp/ghost_v2.json"):
    import xgboost as xgb, numpy as np
    X_np, y_np = np.array(X), np.array(y)
    # Shuffle to mix wins and losses
    idx = np.random.permutation(len(X_np))
    X_np, y_np = X_np[idx], y_np[idx]
    split = int(len(X_np) * 0.8)
    X_train, X_val = X_np[:split], X_np[split:]
    y_train, y_val = y_np[:split], y_np[split:]
    model = xgb.XGBClassifier(
        n_estimators=100, max_depth=2, learning_rate=0.05,
        subsample=0.6, colsample_bytree=0.6, min_child_weight=10,
        gamma=1.0, reg_alpha=0.1, reg_lambda=1.0,
        use_label_encoder=False, eval_metric="logloss",
        scale_pos_weight=1.0,
        random_state=42
    )
    model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)
    val_preds = model.predict(X_val)
    val_probas = model.predict_proba(X_val)[:,1]
    val_acc = float(np.mean(val_preds == y_val))
    train_acc = float(np.mean(model.predict(X_train) == y_train))
    # Confidence calibration check - does high confidence = high accuracy?
    bins = [(0.5,0.6),(0.6,0.7),(0.7,0.8),(0.8,0.9),(0.9,1.0)]
    calibration = {}
    for lo, hi in bins:
        mask = (val_probas >= lo) & (val_probas < hi)
        if mask.sum() > 5:
            calibration[str(lo)+"-"+str(hi)] = {
                "n": int(mask.sum()),
                "accuracy": round(float(np.mean(val_preds[mask] == y_val[mask]))*100, 1)
            }
    LOGGER.info("Train: " + str(round(train_acc*100,1)) + "% | Val: " + str(round(val_acc*100,1)) + "%")
    LOGGER.info("Calibration: " + json.dumps(calibration))
    os.makedirs(os.path.dirname(model_path) if os.path.dirname(model_path) else ".", exist_ok=True)
    model.save_model(model_path)
    meta = {
        "trained_at": int(time.time()), "samples": len(X),
        "train_acc": round(train_acc*100,1), "val_acc": round(val_acc*100,1),
        "calibration": calibration, "model_path": model_path,
        "feature_order": FEATURE_ORDER
    }
    with open(model_path.replace(".json","_meta.json"), "w") as f: json.dump(meta, f)
    return meta

def run_retrain(min_samples=200, model_path="/tmp/ghost_v2.json"):
    LOGGER.info("Starting balanced retrain...")
    result = load_balanced_data(min_samples)
    if result[0] is None:
        return {"error": "Not enough balanced data"}
    X, y, sym_stats = result
    meta = train_model(X, y, model_path)
    try:
        from core import prediction
        prediction._model = None  # force reload on next prediction
    except: pass
    return meta

if __name__ == "__main__":
    result = run_retrain()
    print(json.dumps(result, indent=2))