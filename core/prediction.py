"""
core/prediction.py - Ghost v2 prediction engine.
XGBoost model + regime gate + confidence floor 0.70.
"""
import os, time, logging
from typing import Optional, List
from core.db import db_conn
from core.prices import get_price, get_spy_price, get_vix, get_crypto_price

LOGGER = logging.getLogger("ghost.prediction")
CONFIDENCE_FLOOR = float(os.getenv("MIN_ALERT_CONFIDENCE", "0.70"))
DAILY_CAP = int(os.getenv("DAILY_ALERT_CAP", "10"))
BTC_THRESHOLD = float(os.getenv("BTC_TREND_THRESHOLD", "-5.0"))
VIX_FEAR = float(os.getenv("VIX_FEAR", "25"))
CRYPTO_HOLD_H = int(os.getenv("CRYPTO_FORECAST_H", "48"))
STOP_PCT = float(os.getenv("RISK_SL_PCT", "3.0")) / 100
TARGET_PCT = float(os.getenv("RISK_TP_PCT", "6.0")) / 100
EXCLUDE = set(os.getenv("GHOST_EXCLUDE_SYMBOLS", "").split(","))

CRYPTO_SYMBOLS = os.getenv("CRYPTO_SYMBOLS",
    "BTC,ETH,SOL,XRP,CHZ,LINK,ADA,AVAX,DOT,MATIC,TRX,LTC,ATOM,UNI").split(",")
STOCK_SYMBOLS = os.getenv("STOCK_SYMBOLS",
    "AAPL,NVDA,TSLA,MSFT,META,AMZN,HOOD,COIN,PLTR,AMD,WOLF").split(",")

_model = None

def get_model():
    global _model
    if _model is None:
        try:
            import xgboost as xgb
            path = os.getenv("MODEL_PATH", "/tmp/ghost_v2.json")
            if os.path.exists(path):
                _model = xgb.XGBClassifier()
                _model.load_model(path)
                LOGGER.info("Model loaded from " + path)
        except Exception as e:
            LOGGER.warning("Model load failed: " + str(e))
    return _model

def _check_regime() -> dict:
    gates = {"block_crypto_buys": False, "reduce_size": False, "reason": ""}
    try:
        btc = get_crypto_price("BTC")
        if btc:
            with db_conn() as conn:
                cur = conn.cursor()
                cutoff = int(time.time()) - 86400
                cur.execute(
                    "SELECT entry_price FROM predictions WHERE symbol='BTC' AND (predicted_at > %s OR run_at > %s) ORDER BY id ASC LIMIT 1",
                    (cutoff, cutoff)
                )
                row = cur.fetchone()
                if row and row[0]:
                    pct = (btc - row[0]) / row[0] * 100
                    if pct <= BTC_THRESHOLD:
                        gates["block_crypto_buys"] = True
                        gates["reason"] = "BTC " + str(round(pct,1)) + "% in 24h"
    except Exception as e:
        LOGGER.error("Regime check: " + str(e))
    try:
        vix = get_vix()
        if vix and vix >= VIX_FEAR:
            gates["reduce_size"] = True
            gates["reason"] += " VIX=" + str(round(vix,1))
    except: pass
    return gates

def _build_features(symbol: str, asset_type: str):
    try:
        price = get_price(symbol, asset_type)
        if not price: return None
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT entry_price, outcome FROM predictions WHERE symbol=%s AND outcome IS NOT NULL ORDER BY id DESC LIMIT 20",
                (symbol,)
            )
            rows = cur.fetchall()
        prices = [r[0] for r in rows if r[0]]
        outcomes = [r[1] for r in rows if r[1]]
        sym_wr = outcomes.count("WIN") / len(outcomes) if outcomes else 0.5
        sym_count = len(rows)
        # Same 11 features as retrain.py for model compatibility
        confidence_val = 0.5  # placeholder - will be set by predict_symbol
        direction_val = 1.0   # placeholder - will be set by predict_symbol
        target_pct = TARGET_PCT
        stop_pct = STOP_PCT
        rr = target_pct / stop_pct if stop_pct > 0 else 2.0
        import datetime
        now_dt = datetime.datetime.now()
        hour = float(now_dt.hour) / 24
        dow = float(now_dt.weekday()) / 7
        # Return (real_price, feature_vector) - price is separate from features
        feat = [
            confidence_val,           # [0] placeholder, updated in predict_symbol
            direction_val,            # [1] placeholder, updated in predict_symbol
            1.0 if asset_type == "crypto" else 0.0,
            float(target_pct),
            float(stop_pct),
            float(rr),
            float(sym_wr),
            float(min(sym_count, 100)) / 100,
            float(min(price, 10000)) / 10000,
            hour,
            dow,
        ]
        return (price, feat)  # real price separate from feature vector
    except Exception as e:
        LOGGER.error("Features " + symbol + ": " + str(e))
        return None

def predict_symbol(symbol: str, asset_type: str, regime: dict):
    if symbol in EXCLUDE: return None
    result = _build_features(symbol, asset_type)
    if not result: return None
    price, features = result  # unpack real price from feature vector
    model = get_model()
    if model:
        import numpy as np
        proba = model.predict_proba(np.array([features]))[0]
        up_conf, down_conf = float(proba[1]), float(proba[0])
    else:
        momentum = features[1]
        up_conf = 0.5 + min(abs(momentum) * 10, 0.25)
        down_conf = 1.0 - up_conf
        if momentum < 0: up_conf, down_conf = down_conf, up_conf
    direction = "UP" if up_conf > down_conf else "DOWN"
    confidence = max(up_conf, down_conf)
    if confidence < CONFIDENCE_FLOOR: return None
    if regime["block_crypto_buys"] and asset_type == "crypto" and direction == "UP":
        LOGGER.info("REGIME GATE blocked " + symbol + " UP: " + regime["reason"])
        return None
    now = int(time.time())
    hold = CRYPTO_HOLD_H * 3600 if asset_type == "crypto" else 48 * 3600
    target = price * (1 + TARGET_PCT) if direction == "UP" else price * (1 - TARGET_PCT)
    stop = price * (1 - STOP_PCT) if direction == "UP" else price * (1 + STOP_PCT)
    return {"symbol": symbol, "direction": direction, "confidence": round(confidence,3),
            "entry_price": price, "target_price": round(target,6), "stop_price": round(stop,6),
            "predicted_at": now, "expires_at": now + hold, "asset_type": asset_type}

def run_prediction_cycle() -> List[dict]:
    LOGGER.info("Prediction cycle starting...")
    regime = _check_regime()
    all_picks = []
    symbols = ([(s.strip(),"crypto") for s in CRYPTO_SYMBOLS if s.strip()] +
               [(s.strip(),"stock") for s in STOCK_SYMBOLS if s.strip()])
    for symbol, asset_type in symbols:
        pick = predict_symbol(symbol, asset_type, regime)
        if pick: all_picks.append(pick)
    all_picks.sort(key=lambda x: x["confidence"], reverse=True)
    top = all_picks[:DAILY_CAP]
    saved = []
    with db_conn() as conn:
        cur = conn.cursor()
        for pick in top:
            cur.execute(
                "INSERT INTO predictions (symbol,direction,confidence,entry_price,target_price,stop_price,predicted_at,expires_at,asset_type) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (pick["symbol"],pick["direction"],pick["confidence"],pick["entry_price"],
                 pick["target_price"],pick["stop_price"],pick["predicted_at"],pick["expires_at"],pick["asset_type"])
            )
            pred_id = cur.fetchone()[0]
            pick["id"] = pred_id
            saved.append(pick)
    LOGGER.info("Cycle done: " + str(len(saved)) + " picks saved")
    return saved

def reconcile_outcomes() -> int:
    """Check open predictions against live prices. Mark WIN/LOSS/EXPIRED."""
    resolved = 0
    now = int(time.time())
    with db_conn() as conn:
        cur = conn.cursor()
        # Only reconcile v2 predictions that have valid prices set
        cur.execute(
            "SELECT id, symbol, direction, entry_price, target_price, stop_price, expires_at, asset_type FROM predictions WHERE outcome IS NULL AND entry_price IS NOT NULL AND target_price IS NOT NULL AND stop_price IS NOT NULL AND predicted_at IS NOT NULL"
        )
        open_preds = cur.fetchall()
    for pred_id, symbol, direction, entry, target, stop, expires_at, asset_type in open_preds:
        # Skip if any price is None
        if entry is None or target is None or stop is None: continue
        price = get_price(symbol, asset_type or "crypto")
        if not price: continue
        outcome = None
        if direction == "UP":
            if price >= target: outcome = "WIN"
            elif price <= stop: outcome = "LOSS"
        else:
            if price <= target: outcome = "WIN"
            elif price >= stop: outcome = "LOSS"
        if not outcome and expires_at and now > expires_at: outcome = "EXPIRED"
        if outcome:
            if direction == "UP":
                pnl = (price - entry) / entry * 100
            else:
                pnl = (entry - price) / entry * 100
            usd_out = round(100.0 * (1 + pnl/100), 2)
            with db_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE predictions SET outcome=%s, exit_price=%s, pnl_pct=%s, resolved_at=%s WHERE id=%s",
                    (outcome, price, round(pnl,3), now, pred_id)
                )
                cur.execute(
                    "UPDATE paper_trades SET result=%s, exit_price=%s, pnl_pct=%s, exit_time=%s, usd_out=%s WHERE prediction_id=%s",
                    (outcome, price, round(pnl,3), now, usd_out, pred_id)
                )
            resolved += 1
            LOGGER.info("Resolved " + symbol + " " + direction + ": " + outcome + " " + str(round(pnl,2)) + "%")
    return resolved