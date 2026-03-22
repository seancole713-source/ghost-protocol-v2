"""
core/prediction.py - Ghost prediction engine.
XGBoost model + regime gate + confidence floor.
Only fires picks above 0.70 confidence.
Blocks crypto BUYs when BTC down 5%+.
"""
import os, time, logging, json
from typing import Optional, List, Dict
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

CRYPTO_SYMBOLS = os.getenv("CRYPTO_SYMBOLS",
    "BTC,ETH,SOL,XRP,CHZ,LINK,ADA,AVAX,DOT,MATIC,TRX,LTC,ATOM,UNI").split(",")
STOCK_SYMBOLS = os.getenv("STOCK_SYMBOLS",
    "AAPL,NVDA,TSLA,MSFT,META,AMZN,GOOGL,HOOD,COIN,PLTR,AMD,WOLF").split(",")

EXCLUDE = set(os.getenv("GHOST_EXCLUDE_SYMBOLS", "").split(","))

def _load_model():
    """Load XGBoost model from disk. Returns None if not found."""
    try:
        import xgboost as xgb
        model_path = os.getenv("MODEL_PATH", "models/ghost_v2.json")
        if os.path.exists(model_path):
            model = xgb.XGBClassifier()
            model.load_model(model_path)
            LOGGER.info(f"Model loaded from {model_path}")
            return model
    except Exception as e:
        LOGGER.warning(f"Model load failed: {e}")
    return None

_model = None

def get_model():
    global _model
    if _model is None:
        _model = _load_model()
    return _model

def _check_regime() -> dict:
    """Check market regime. Returns gates that should block trades."""
    gates = {"block_crypto_buys": False, "reduce_size": False, "reason": ""}
    try:
        # BTC trend check
        btc_now = get_crypto_price("BTC")
        if btc_now:
            # Compare to 24h ago via DB
            with db_conn() as conn:
                cur = conn.cursor()
                cutoff = int(time.time()) - 86400
                cur.execute(
                    "SELECT entry_price FROM predictions WHERE symbol='BTC' AND predicted_at > %s ORDER BY predicted_at ASC LIMIT 1",
                    (cutoff,)
                )
                row = cur.fetchone()
                if row:
                    btc_24h_ago = row[0]
                    btc_pct = (btc_now - btc_24h_ago) / btc_24h_ago * 100
                    if btc_pct <= BTC_THRESHOLD:
                        gates["block_crypto_buys"] = True
                        gates["reason"] = f"BTC down {btc_pct:.1f}% in 24h"
                        LOGGER.info(f"REGIME: blocking crypto BUYs - {gates['reason']}")
        # VIX check
        vix = get_vix()
        if vix and vix >= VIX_FEAR:
            gates["reduce_size"] = True
            gates["reason"] += f" | VIX {vix:.1f} >= {VIX_FEAR}"
    except Exception as e:
        LOGGER.error(f"Regime check error: {e}")
    return gates

def _build_features(symbol: str, asset_type: str) -> Optional[List[float]]:
    """Build feature vector for a symbol. Returns None if data unavailable."""
    try:
        price = get_price(symbol, asset_type)
        if not price: return None
        # Core features - expand as model improves
        # For now: price momentum proxies from DB history
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT entry_price, outcome FROM predictions WHERE symbol=%s ORDER BY predicted_at DESC LIMIT 20",
                (symbol,)
            )
            rows = cur.fetchall()
        prices = [r[0] for r in rows if r[0]]
        outcomes = [r[1] for r in rows if r[1]]
        win_rate = outcomes.count("WIN") / len(outcomes) if outcomes else 0.5
        price_momentum = (price - prices[0]) / prices[0] if prices else 0.0
        volatility = max(prices)/min(prices) - 1 if len(prices) > 1 else 0.1
        # 10 base features
        return [
            price,
            price_momentum,
            volatility,
            win_rate,
            len(rows),
            1.0 if asset_type == "crypto" else 0.0,
            float(symbol in ["BTC","ETH","XRP","AAPL","NVDA","TSLA"]),
            float(get_vix() or 15.0) / 100,
            float(len(outcomes)),
            float(outcomes.count("WIN")),
        ]
    except Exception as e:
        LOGGER.error(f"Feature build failed for {symbol}: {e}")
        return None

def predict_symbol(symbol: str, asset_type: str, regime: dict) -> Optional[dict]:
    """Generate a prediction for one symbol. Returns None if below floor."""
    if symbol in EXCLUDE:
        return None
    features = _build_features(symbol, asset_type)
    if not features:
        return None
    model = get_model()
    price = features[0]
    # Use model if available, else simple momentum signal
    if model:
        import numpy as np
        proba = model.predict_proba(np.array([features]))[0]
        up_conf = float(proba[1])
        down_conf = float(proba[0])
    else:
        # Fallback: momentum-based signal
        momentum = features[1]
        up_conf = 0.5 + min(abs(momentum) * 10, 0.25)
        down_conf = 1.0 - up_conf
        if momentum < 0:
            up_conf, down_conf = down_conf, up_conf
    direction = "UP" if up_conf > down_conf else "DOWN"
    confidence = max(up_conf, down_conf)
    # Apply confidence floor
    if confidence < CONFIDENCE_FLOOR:
        return None
    # Apply regime gate
    if regime["block_crypto_buys"] and asset_type == "crypto" and direction == "UP":
        LOGGER.info(f"REGIME GATE: blocked {symbol} UP - {regime['reason']}")
        return None
    now = int(time.time())
    hold_seconds = CRYPTO_HOLD_H * 3600 if asset_type == "crypto" else 48 * 3600
    target = price * (1 + TARGET_PCT) if direction == "UP" else price * (1 - TARGET_PCT)
    stop = price * (1 - STOP_PCT) if direction == "UP" else price * (1 + STOP_PCT)
    return {
        "symbol": symbol,
        "direction": direction,
        "confidence": round(confidence, 3),
        "entry_price": price,
        "target_price": round(target, 6),
        "stop_price": round(stop, 6),
        "predicted_at": now,
        "expires_at": now + hold_seconds,
        "asset_type": asset_type,
    }

def run_prediction_cycle() -> List[dict]:
    """
    Main prediction cycle. Called by scheduler at 8AM CT + hourly.
    Returns list of picks saved to DB.
    """
    LOGGER.info("Starting prediction cycle...")
    regime = _check_regime()
    all_picks = []
    symbols = (
        [(s.strip(), "crypto") for s in CRYPTO_SYMBOLS if s.strip()] +
        [(s.strip(), "stock") for s in STOCK_SYMBOLS if s.strip()]
    )
    for symbol, asset_type in symbols:
        pick = predict_symbol(symbol, asset_type, regime)
        if pick:
            all_picks.append(pick)
    # Sort by confidence, take top DAILY_CAP
    all_picks.sort(key=lambda x: x["confidence"], reverse=True)
    top_picks = all_picks[:DAILY_CAP]
    # Save to DB
    saved = []
    with db_conn() as conn:
        cur = conn.cursor()
        for pick in top_picks:
            cur.execute(
                """INSERT INTO predictions
                   (symbol, direction, confidence, entry_price, target_price,
                    stop_price, predicted_at, expires_at, asset_type)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (pick["symbol"], pick["direction"], pick["confidence"],
                 pick["entry_price"], pick["target_price"], pick["stop_price"],
                 pick["predicted_at"], pick["expires_at"], pick["asset_type"])
            )
            pred_id = cur.fetchone()[0]
            pick["id"] = pred_id
            cur.execute(
                """INSERT INTO paper_trades
                   (prediction_id, symbol, direction, entry_price,
                    target_price, stop_price, entry_time, usd_in)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (pred_id, pick["symbol"], pick["direction"], pick["entry_price"],
                 pick["target_price"], pick["stop_price"], pick["predicted_at"], 100.0)
            )
            saved.append(pick)
    LOGGER.info(f"Cycle complete: {len(saved)}/{len(all_picks)} picks saved (regime: {regime['reason'] or 'OK'})")
    return saved

def reconcile_outcomes() -> int:
    """
    Check all open predictions against current prices.
    Mark WIN/LOSS for any that hit target or stop.
    Returns count of resolved predictions.
    """
    resolved = 0
    now = int(time.time())
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, symbol, direction, target_price, stop_price, expires_at, asset_type FROM predictions WHERE outcome IS NULL"
        )
        open_preds = cur.fetchall()
    for pred_id, symbol, direction, target, stop, expires_at, asset_type in open_preds:
        price = get_price(symbol, asset_type or "crypto")
        if not price: continue
        outcome = None
        if direction == "UP":
            if price >= target: outcome = "WIN"
            elif price <= stop: outcome = "LOSS"
        else:
            if price <= target: outcome = "WIN"
            elif price >= stop: outcome = "LOSS"
        if not outcome and now > expires_at:
            outcome = "EXPIRED"
        if outcome:
            entry_price = None
            with db_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT entry_price FROM predictions WHERE id=%s", (pred_id,))
                row = cur.fetchone()
                if row: entry_price = row[0]
            if entry_price:
                if direction == "UP":
                    pnl = (price - entry_price) / entry_price * 100
                else:
                    pnl = (entry_price - price) / entry_price * 100
                usd_out = 100.0 * (1 + pnl/100)
                with db_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE predictions SET outcome=%s, exit_price=%s, pnl_pct=%s, resolved_at=%s WHERE id=%s",
                        (outcome, price, round(pnl,3), now, pred_id)
                    )
                    cur.execute(
                        "UPDATE paper_trades SET result=%s, exit_price=%s, pnl_pct=%s, exit_time=%s, usd_out=%s WHERE prediction_id=%s",
                        (outcome, price, round(pnl,3), now, round(usd_out,2), pred_id)
                    )
                resolved += 1
                LOGGER.info(f"Resolved {symbol} {direction}: {outcome} {pnl:+.2f}%")
    return resolved