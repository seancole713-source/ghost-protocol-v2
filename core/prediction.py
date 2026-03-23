"""
core/prediction.py - Ghost v2 prediction engine.
NUCLEAR OPTION: pure win-rate signal, no XGBoost.
Signal source: per-symbol historical accuracy from real resolved picks.
Rules:
  - 30+ resolved picks AND win_rate > 55%: predict dominant direction
  - 30+ resolved picks AND win_rate < 45%: predict inverse
  - Less than 30 picks: momentum-based (price vs 7-day average)
  - Regime gate: BTC down 5%+ blocks crypto BUYs
  - Confidence floor 0.55 (lower than before since signal is calibrated)
"""
import os, time, logging
from typing import Optional, List
from core.db import db_conn
from core.prices import get_price, get_vix, get_crypto_price

LOGGER = logging.getLogger("ghost.prediction")
CONFIDENCE_FLOOR = float(os.getenv("MIN_ALERT_CONFIDENCE", "0.52"))
DAILY_CAP = int(os.getenv("DAILY_ALERT_CAP", "10"))
BTC_THRESHOLD = float(os.getenv("BTC_TREND_THRESHOLD", "-5.0"))
VIX_FEAR = float(os.getenv("VIX_FEAR", "25"))
CRYPTO_HOLD_H = int(os.getenv("CRYPTO_FORECAST_H", "48"))
STOP_PCT = float(os.getenv("RISK_SL_PCT", "3.0")) / 100
TARGET_PCT = float(os.getenv("RISK_TP_PCT", "6.0")) / 100
EXCLUDE = set(os.getenv("GHOST_EXCLUDE_SYMBOLS", "").split(","))
MIN_SAMPLES = 5  # Low until v2 accumulates history
EDGE_THRESHOLD = 0.55  # Win rate above this = real edge
INVERSE_THRESHOLD = 0.45  # Win rate below this = inverse signal

CRYPTO_SYMBOLS = os.getenv("CRYPTO_SYMBOLS",
    "BTC,ETH,SOL,XRP,CHZ,LINK,ADA,AVAX,DOT,MATIC,TRX,LTC,ATOM,UNI").split(",")
STOCK_SYMBOLS = os.getenv("STOCK_SYMBOLS",
    "AAPL,NVDA,TSLA,MSFT,META,AMZN,HOOD,COIN,PLTR,AMD").split(",")

def _check_regime():
    """Block crypto BUYs when BTC down 5%+."""
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
        LOGGER.error("Regime: " + str(e))
    try:
        vix = get_vix()
        if vix and vix >= VIX_FEAR:
            gates["reduce_size"] = True
            gates["reason"] += " VIX=" + str(round(vix,1))
    except: pass
    return gates

def _get_symbol_signal(symbol, current_price):
    """
    Core signal logic. Returns (direction, confidence) or None.
    Uses historical win rate from real resolved picks.
    Falls back to price momentum for symbols with < MIN_SAMPLES.
    """
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            # Get resolved picks for this symbol, most recent 100
            cur.execute(
                "SELECT direction, outcome FROM predictions WHERE symbol=%s AND outcome IN ('WIN','LOSS') ORDER BY id DESC LIMIT 100",
                (symbol,))
            rows = cur.fetchall()
        if len(rows) >= MIN_SAMPLES:
            total = len(rows)
            wins = sum(1 for _, o in rows if o == "WIN")
            win_rate = wins / total
            # Dominant direction from recent history
            up_picks = sum(1 for d, _ in rows if d == "UP")
            dominant_dir = "UP" if up_picks >= total / 2 else "DOWN"
            up_win_rate = sum(1 for d, o in rows if d == "UP" and o == "WIN") / max(up_picks, 1)
            down_picks = total - up_picks
            down_win_rate = sum(1 for d, o in rows if d == "DOWN" and o == "WIN") / max(down_picks, 1)
            # Pick the direction with better win rate
            if up_win_rate > down_win_rate and up_win_rate > EDGE_THRESHOLD:
                return ("UP", round(up_win_rate, 3))
            elif down_win_rate > up_win_rate and down_win_rate > EDGE_THRESHOLD:
                return ("DOWN", round(down_win_rate, 3))
            elif win_rate < INVERSE_THRESHOLD:
                # Ghost has an inverse edge - flip it
                inv_dir = "DOWN" if dominant_dir == "UP" else "UP"
                return (inv_dir, round(1.0 - win_rate, 3))
            else:
                return None  # No edge (45-55% zone)
        else:
            # Not enough history - use price momentum vs last recorded price
            with db_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT entry_price, predicted_at FROM predictions WHERE symbol=%s AND entry_price > 0 ORDER BY id DESC LIMIT 5",
                    (symbol,))
                price_rows = cur.fetchall()
            if len(price_rows) >= 2:
                oldest_price = price_rows[-1][0]
                age_hours = (time.time() - (price_rows[-1][1] or 0)) / 3600
                if oldest_price > 0 and age_hours < 72:
                    pct_change = (current_price - oldest_price) / oldest_price
                    if abs(pct_change) > 0.01:  # >1% move
                        direction = "UP" if pct_change > 0 else "DOWN"
                        confidence = min(0.5 + abs(pct_change) * 5, 0.65)
                        return (direction, round(confidence, 3))
            return None  # Insufficient data
    except Exception as e:
        LOGGER.error("Signal " + symbol + ": " + str(e))
        return None

def predict_symbol(symbol, asset_type, regime):
    if symbol.strip() in EXCLUDE or not symbol.strip(): return None
    price = get_price(symbol, asset_type)
    if not price or price <= 0: return None
    signal = _get_symbol_signal(symbol, price)
    if not signal: return None
    direction, confidence = signal
    if confidence < CONFIDENCE_FLOOR: return None
    if regime["block_crypto_buys"] and asset_type == "crypto" and direction == "UP":
        LOGGER.info("REGIME blocked " + symbol + " UP")
        return None
    now = int(time.time())
    hold = CRYPTO_HOLD_H * 3600 if asset_type == "crypto" else 48 * 3600
    target = price * (1 + TARGET_PCT) if direction == "UP" else price * (1 - TARGET_PCT)
    stop = price * (1 - STOP_PCT) if direction == "UP" else price * (1 + STOP_PCT)
    return {
        "symbol": symbol, "direction": direction, "confidence": confidence,
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
    LOGGER.info("Cycle: " + str(len(saved)) + "/" + str(len(all_picks)) + " picks | regime: " + (regime["reason"] or "OK"))
    return saved

def reconcile_outcomes():
    """Check open v2 predictions against live prices. Mark WIN/LOSS/EXPIRED."""
    resolved = 0
    now = int(time.time())
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id,symbol,direction,entry_price,target_price,stop_price,expires_at,asset_type FROM predictions WHERE outcome IS NULL AND predicted_at IS NOT NULL AND entry_price IS NOT NULL AND entry_price > 0 AND target_price IS NOT NULL AND stop_price IS NOT NULL"
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