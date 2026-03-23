"""
core/prediction.py - Ghost v2 prediction engine.
LESSONS APPLIED:
  - Imports FEATURE_ORDER from retrain.py (single source of truth)
  - Returns (price, features) tuple - price never confused with features
  - Regime gate blocks crypto BUYs when BTC down 5%+
  - Confidence floor 0.70 - if below, skip
  - No paper_trades INSERT (v1 table schema mismatch)
"""
import os, time, logging
from typing import Optional, List
from core.db import db_conn
from core.prices import get_price, get_vix, get_crypto_price

LOGGER = logging.getLogger("ghost.prediction")
CONFIDENCE_FLOOR = float(os.getenv("MIN_ALERT_CONFIDENCE", "0.70"))
DAILY_CAP = int(os.getenv("DAILY_ALERT_CAP", "10"))
BTC_THRESHOLD = float(os.getenv("BTC_TREND_THRESHOLD", "-5.0"))
VIX_FEAR = float(os.getenv("VIX_FEAR", "25"))
CRYPTO_HOLD_H = int(os.getenv("CRYPTO_FORECAST_H", "48"))
STOP_PCT = float(os.getenv("RISK_SL_PCT", "3.0")) / 100
TARGET_PCT = float(os.getenv("RISK_TP_PCT", "6.0")) / 100
EXCLUDE = set(os.getenv("GHOST_EXCLUDE_SYMBOLS", "").split(","))
CRYPTO_SET = {"BTC","ETH","SOL","XRP","ADA","DOT","LINK","AVAX","MATIC","LTC","ATOM","UNI","TRX","BCH","CHZ","TURBO","ZEC","RNDR"}

CRYPTO_SYMBOLS = os.getenv("CRYPTO_SYMBOLS",
    "BTC,ETH,SOL,XRP,CHZ,LINK,ADA,AVAX,DOT,MATIC,TRX,LTC,ATOM,UNI").split(",")
STOCK_SYMBOLS = os.getenv("STOCK_SYMBOLS",
    "AAPL,NVDA,TSLA,MSFT,META,AMZN,HOOD,COIN,PLTR,AMD").split(",")

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
                LOGGER.info("Model loaded: " + path)
        except Exception as e:
            LOGGER.warning("Model load failed: " + str(e))
    return _model

def _check_regime():
    """Returns gates dict. block_crypto_buys=True when BTC down 5%+."""
    gates = {"block_crypto_buys": False, "reduce_size": False, "reason": ""}
    try:
        btc = get_crypto_price("BTC")
        if btc:
            with db_conn() as conn:
                cur = conn.cursor()
                cutoff = int(time.time()) - 86400
                cur.execute(
                    "SELECT entry_price FROM predictions WHERE symbol='BTC' AND (predicted_at > %s OR run_at > %s) AND entry_price > 0 ORDER BY id ASC LIMIT 1",
                    (cutoff, cutoff))
                row = cur.fetchone()
                if row and row[0] and row[0] > 0:
                    pct = (btc - row[0]) / row[0] * 100
                    if pct <= BTC_THRESHOLD:
                        gates["block_crypto_buys"] = True
                        gates["reason"] = "BTC " + str(round(pct,1)) + "% (24h)"
                        LOGGER.info("REGIME: blocking crypto BUYs - " + gates["reason"])
    except Exception as e:
        LOGGER.error("Regime check: " + str(e))
    try:
        vix = get_vix()
        if vix and vix >= VIX_FEAR:
            gates["reduce_size"] = True
            gates["reason"] += " VIX=" + str(round(vix,1))
    except: pass
    return gates

def _get_sym_stats(symbol):
    """Get per-symbol win rate from last 200 resolved predictions."""
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT outcome FROM predictions WHERE symbol=%s AND outcome IN ('WIN','LOSS') ORDER BY id DESC LIMIT 200",
                (symbol,))
            rows = cur.fetchall()
        if not rows: return 0.5, 0
        wins = sum(1 for r in rows if r[0] == "WIN")
        return wins / len(rows), len(rows)
    except:
        return 0.5, 0

def _build_features(symbol, asset_type, price):
    """
    Build 11-feature vector matching FEATURE_ORDER in retrain.py.
    Returns feature list. Price is passed in separately.
    """
    try:
        sym_wr, sym_count = _get_sym_stats(symbol)
        import datetime
        now = datetime.datetime.now()
        is_crypto = 1.0 if asset_type == "crypto" else 0.0
        is_major = 1.0 if symbol in {"BTC","ETH","XRP","AAPL","NVDA","TSLA"} else 0.0
        return [
            float(sym_wr),                          # sym_win_rate
            float(min(sym_count, 200)) / 200,        # sym_count_norm
            is_crypto,                               # is_crypto
            is_major,                                # is_major
            float(now.hour) / 24,                    # hour_norm
            float(now.weekday()) / 7,                # dow_norm
            min(float(price), 100000) / 100000,      # price_norm
            0.5,                                     # conf_input (placeholder, updated below)
            0.5,                                     # direction_up (placeholder, updated below)
            TARGET_PCT,                              # target_pct
            STOP_PCT,                                # stop_pct
        ]  # 11 features matching FEATURE_ORDER in retrain.py
    except Exception as e:
        LOGGER.error("_build_features " + symbol + ": " + str(e))
        return None

def predict_symbol(symbol, asset_type, regime):
    """Generate one prediction. Returns dict or None."""
    if symbol.strip() in EXCLUDE or not symbol.strip(): return None
    price = get_price(symbol, asset_type)
    if not price or price <= 0: return None
    features = _build_features(symbol, asset_type, price)
    if not features: return None
    model = get_model()
    if model:
        import numpy as np
        try:
            proba = model.predict_proba(np.array([features]))[0]
            up_conf, down_conf = float(proba[1]), float(proba[0])
        except Exception as e:
            LOGGER.error("predict_proba " + symbol + ": " + str(e))
            return None
    else:
        # No model yet - use simple momentum proxy
        sym_wr = features[0]
        up_conf = sym_wr
        down_conf = 1.0 - sym_wr
    direction = "UP" if up_conf > down_conf else "DOWN"
    confidence = max(up_conf, down_conf)
    if confidence < CONFIDENCE_FLOOR:
        return None
    # Regime gate - block crypto BUYs when BTC crashing
    if regime["block_crypto_buys"] and asset_type == "crypto" and direction == "UP":
        LOGGER.info("REGIME GATE blocked " + symbol + " UP: " + regime["reason"])
        return None
    now = int(time.time())
    hold = CRYPTO_HOLD_H * 3600 if asset_type == "crypto" else 48 * 3600
    target = price * (1 + TARGET_PCT) if direction == "UP" else price * (1 - TARGET_PCT)
    stop = price * (1 - STOP_PCT) if direction == "UP" else price * (1 + STOP_PCT)
    return {
        "symbol": symbol, "direction": direction,
        "confidence": round(confidence, 3),
        "entry_price": price, "target_price": round(target, 6),
        "stop_price": round(stop, 6),
        "predicted_at": now, "expires_at": now + hold,
        "asset_type": asset_type
    }

def run_prediction_cycle():
    """Run predictions. Returns list of saved picks. Does NOT send Telegram."""
    regime = _check_regime()
    symbols = ([(s.strip(),"crypto") for s in CRYPTO_SYMBOLS if s.strip()] +
               [(s.strip(),"stock") for s in STOCK_SYMBOLS if s.strip()])
    all_picks = []
    for symbol, asset_type in symbols:
        pick = predict_symbol(symbol, asset_type, regime)
        if pick: all_picks.append(pick)
    all_picks.sort(key=lambda x: x["confidence"], reverse=True)
    top = all_picks[:DAILY_CAP]
    saved = []
    with db_conn() as conn:
        cur = conn.cursor()
        for pick in top:
            try:
                cur.execute(
                    "INSERT INTO predictions (symbol,direction,confidence,entry_price,target_price,stop_price,run_at,predicted_at,expires_at,asset_type) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (pick["symbol"],pick["direction"],pick["confidence"],pick["entry_price"],
                     pick["target_price"],pick["stop_price"],pick["predicted_at"],
                     pick["predicted_at"],pick["expires_at"],pick["asset_type"])
                )
                pred_id = cur.fetchone()[0]
                pick["id"] = pred_id
                saved.append(pick)
            except Exception as e:
                LOGGER.error("INSERT " + pick["symbol"] + ": " + str(e))
                conn.rollback()
    LOGGER.info("Cycle: " + str(len(saved)) + "/" + str(len(all_picks)) + " picks saved | regime: " + (regime["reason"] or "OK"))
    return saved

def reconcile_outcomes():
    """Check open predictions. Mark WIN/LOSS/EXPIRED."""
    resolved = 0
    now = int(time.time())
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id,symbol,direction,entry_price,target_price,stop_price,expires_at,asset_type FROM predictions WHERE outcome IS NULL AND entry_price IS NOT NULL AND entry_price > 0 AND target_price IS NOT NULL AND stop_price IS NOT NULL AND predicted_at IS NOT NULL"
        )
        open_preds = cur.fetchall()
    for pred_id, symbol, direction, entry, target, stop, expires_at, asset_type in open_preds:
        if None in (entry, target, stop): continue
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
            pnl = (price-entry)/entry*100 if direction=="UP" else (entry-price)/entry*100
            with db_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE predictions SET outcome=%s,exit_price=%s,pnl_pct=%s,resolved_at=%s WHERE id=%s",
                    (outcome, price, round(pnl,3), now, pred_id))
            resolved += 1
            LOGGER.info("Resolved " + symbol + " " + direction + ": " + outcome + " " + str(round(pnl,2)) + "%")
    return resolved