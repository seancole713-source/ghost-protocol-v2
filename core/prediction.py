"""
core/prediction.py - Ghost v2 prediction engine.
Signal source: per-symbol historical accuracy from real resolved picks.
Rules:
  - 30+ resolved picks AND win_rate > 55%: predict dominant direction
  - 30+ resolved picks AND win_rate < 45%: predict inverse
  - Less than 30 picks: momentum-based (price vs 7-day average)
  - Regime gate: BTC down 5%+ blocks crypto BUYs
  - SELL signals blocked: 1.9% win rate across 211 trades
  - Confidence floor 0.75 (raised from 0.60)
  - Features logged on every prediction for future ML training
"""
import os, time, logging, json
from datetime import datetime, timezone
from typing import Optional, List
from core.db import db_conn
try:
    from core.prices import get_price, get_crypto_price
except ImportError:
    def get_price(s, t): return None
    def get_crypto_price(s): return None

LOGGER = logging.getLogger("ghost.prediction")

CONFIDENCE_FLOOR = float(os.getenv("MIN_ALERT_CONFIDENCE", "0.75"))
DAILY_CAP        = int(os.getenv("DAILY_ALERT_CAP", "10"))
CRYPTO_HOLD_H    = int(os.getenv("CRYPTO_HOLD_HOURS", "48"))
TARGET_PCT       = float(os.getenv("TARGET_PCT", "0.06"))
STOP_PCT         = float(os.getenv("STOP_PCT", "0.03"))
MIN_SAMPLES      = int(os.getenv("MIN_SAMPLES", "10"))
EDGE_THRESHOLD   = 0.55
INVERSE_THRESHOLD = 0.40
BTC_THRESHOLD    = float(os.getenv("BTC_TREND_THRESHOLD", "-5.0"))

CRYPTO_SYMBOLS = os.getenv(
    "CRYPTO_SYMBOLS",
    "BTC,ETH,SOL,XRP,LINK,DOT,MATIC,TRX,LTC,ATOM,UNI,BCH,NEAR,SUI,ARB,AAVE").split(",")
STOCK_SYMBOLS = os.getenv(
    "STOCK_SYMBOLS",
    "AAPL,NVDA,TSLA,MSFT,META,AMZN,PLTR,AMD,T,XPO,NET").split(",")
EXCLUDE = set(os.getenv("EXCLUDE_SYMBOLS","HOOD,COIN,CHZ,ADA,AVAX,SAND,FLOW,HBAR,ALGO").split(","))


def _check_regime():
    """Block crypto BUYs when BTC down significantly. Returns regime dict including btc_24h_pct."""
    gates = {"block_crypto_buys": False, "reduce_size": False, "reason": "", "btc_24h_pct": 0.0}
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
                    gates["btc_24h_pct"] = round(pct, 2)
                    if pct <= BTC_THRESHOLD or pct <= -2.0:
                        gates["block_crypto_buys"] = True
                        gates["reason"] = "BTC " + str(round(pct,1)) + "% (24h)"
                        LOGGER.info("REGIME: blocking crypto BUYs, BTC " + str(round(pct,1)) + "%")
    except Exception as e:
        LOGGER.warning("regime check failed: " + str(e))
    return gates


def _get_sentiment(symbol):
    """Get cached news sentiment for symbol. Returns float -1..+1."""
    try:
        from core.news import get_sentiment_for_symbol
        return get_sentiment_for_symbol(symbol)
    except Exception:
        return 0.0


def _get_symbol_signal(symbol, current_price):
    # T22: Skip symbols with < 15% WR after 20+ v2 picks
    try:
        with db_conn() as _ac:
            _c = _ac.cursor()
            _c.execute(
                "SELECT COUNT(*), SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) FROM predictions WHERE symbol=%s AND outcome IN ('WIN','LOSS') AND direction='UP' AND predicted_at > 1742000000",
                (symbol,))
            _r = _c.fetchone()
            if _r and _r[0] and _r[0] >= 20:
                _wr = (_r[1] or 0) / _r[0]
                if _wr < 0.15:
                    LOGGER.info("T22 SKIP " + symbol + " poor WR: " + str(round(_wr*100,1)) + "% on " + str(_r[0]) + " picks")
                    return None
    except Exception: pass
    """
    Core signal logic. Returns (direction, confidence) or None.
    Uses v2 resolved picks + legacy ghost_prediction_outcomes as fallback.
    """
    rows = []
    v2_rows = []
    try:
        with db_conn() as conn:
            cur = conn.cursor()
            # v2 resolved picks
            cur.execute(
                "SELECT direction, CASE WHEN outcome='WIN' THEN 1 ELSE 0 END FROM predictions WHERE symbol=%s AND outcome IN ('WIN','LOSS') ORDER BY id DESC LIMIT 50",
                (symbol,))
            v2_rows = cur.fetchall()
            # Legacy ghost_prediction_outcomes
            cur.execute("""
                SELECT predicted_direction, hit_direction
                FROM ghost_prediction_outcomes
                WHERE symbol = %s AND hit_direction IN (0, 1)
                ORDER BY id DESC LIMIT 100
            """, (symbol,))
            rows = cur.fetchall()
    except Exception as e:
        LOGGER.warning("signal query failed for " + symbol + ": " + str(e))
        return None

    if len(v2_rows) >= 8:
        # Circuit breaker: 8 consecutive v2 losses
        last8 = [o for _, o in v2_rows[:8]]
        if sum(last8) == 0:
            try:
                with db_conn() as conn2:
                    cur2 = conn2.cursor()
                    cur2.execute(
                        "SELECT COUNT(*), SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) FROM ghost_prediction_outcomes WHERE symbol=%s AND hit_direction IN (0,1)",
                        (symbol,))
                    r = cur2.fetchone()
                    gpo_total = r[0] or 0
                    gpo_wins  = r[1] or 0
                    gpo_wr = gpo_wins / gpo_total if gpo_total > 0 else 0
            except Exception:
                gpo_wr = 0
            if gpo_wr <= 0.60:
                LOGGER.info("CIRCUIT BREAKER: " + symbol + " benched (8 v2 losses, gpo_wr=" + str(round(gpo_wr,2)) + ")")
                return None
            else:
                LOGGER.info("CB SKIPPED: " + symbol + " has gpo_wr=" + str(round(gpo_wr,2)) + " overrides 8 v2 losses")

    rows = list(v2_rows) + list(rows)

    if len(rows) >= MIN_SAMPLES:
        total = len(rows)
        wins = sum(1 for _, o in rows if o == 1 or o == "WIN")
        win_rate = wins / total
        up_picks = sum(1 for d, _ in rows if d == "UP")
        dominant_dir = "UP" if up_picks >= total / 2 else "DOWN"
        up_win_rate = sum(1 for d, o in rows if d == "UP" and (o == 1 or o == "WIN")) / max(up_picks, 1)
        down_picks = total - up_picks
        down_win_rate = sum(1 for d, o in rows if d == "DOWN" and (o == 1 or o == "WIN")) / max(down_picks, 1)

        if up_win_rate > down_win_rate and up_win_rate > EDGE_THRESHOLD:
            return ("UP", round(up_win_rate, 3))
        elif down_win_rate > up_win_rate and down_win_rate > EDGE_THRESHOLD:
            return ("DOWN", round(down_win_rate, 3))
        elif win_rate < INVERSE_THRESHOLD:
            inv_dir = "DOWN" if dominant_dir == "UP" else "UP"
            inv_conf = min(round(1.0 - win_rate, 3), 0.65)
            return (inv_dir, inv_conf)
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
                    conf = min(round(0.55 + abs(pct_change) * 2, 3), 0.70)
                    return (direction, conf)
        return None


def predict_symbol(symbol, asset_type, regime):
    if symbol.strip() in EXCLUDE or not symbol.strip():
        return None
    price = get_price(symbol, asset_type)
    if (not price or price <= 0) and asset_type == "stock":
        try:
            import yfinance as _yf
            _hist = _yf.Ticker(symbol).history(period="2d")
            if not _hist.empty:
                price = float(_hist["Close"].iloc[-1])
                LOGGER.info("Stock prev-close for " + symbol + ": $" + str(round(price,2)))
        except Exception as _pe:
            LOGGER.warning("Prev-close fallback failed " + symbol + ": " + str(_pe))
    if not price or price <= 0:
        return None
    signal = _get_symbol_signal(symbol, price)
    if not signal:
        return None
    direction, confidence = signal

    _floor = regime.get('confidence_floor_override', CONFIDENCE_FLOOR) if isinstance(regime, dict) else CONFIDENCE_FLOOR
    if confidence < _floor:
        return None

    # SELL signals blocked: 1.9% win rate across 211 trades (data as of 2026-03-25)
    if direction == "DOWN":
        LOGGER.info("SELL blocked: " + symbol + " — DOWN signals 1.9% wr historically")
        return None

    if regime["block_crypto_buys"] and asset_type == "crypto" and direction == "UP":
        LOGGER.info("REGIME blocked " + symbol + " UP")
        return None

    now = int(time.time())
    hold = CRYPTO_HOLD_H * 3600 if asset_type == "crypto" else 48 * 3600
    target = price * (1 + TARGET_PCT) if direction == "UP" else price * (1 - TARGET_PCT)
    stop   = price * (1 - STOP_PCT)   if direction == "UP" else price * (1 + STOP_PCT)

    # Capture raw confidence before sentiment nudge (for ML features)
    confidence_raw = confidence

    # Price change vs ~4h ago (for ML features)
    price_4h_pct = 0.0
    try:
        with db_conn() as fc:
            fc_cur = fc.cursor()
            fc_cur.execute(
                "SELECT entry_price FROM predictions WHERE symbol=%s AND predicted_at > %s AND entry_price > 0 ORDER BY id ASC LIMIT 1",
                (symbol, int(time.time()) - 14400))
            old_row = fc_cur.fetchone()
            if old_row and old_row[0] and float(old_row[0]) > 0:
                price_4h_pct = round((price - float(old_row[0])) / float(old_row[0]) * 100, 3)
    except Exception:
        pass

    # Claude news sentiment: nudges confidence +-10% based on news alignment
    sentiment_score = 0.0
    try:
        sent = _get_sentiment(symbol)
        sentiment_score = float(sent)
        if abs(sent) > 0.1:
            dir_mult = 1.0 if direction in ("UP", "BUY") else -1.0
            adj = round(sent * dir_mult * 0.10, 3)
            confidence = round(max(CONFIDENCE_FLOOR, min(0.98, confidence + adj)), 3)
            LOGGER.info("[SENTIMENT] " + symbol + " news=" + str(round(sent,2)) + " adj=" + str(adj) + " conf=" + str(confidence))
    except Exception:
        pass

    # Build feature vector — stored in DB for future ML training
    now_dt = datetime.now(timezone.utc)
    features = {
        "btc_24h_pct":      regime.get("btc_24h_pct", 0.0),
        "hour_of_day":      now_dt.hour,
        "day_of_week":      now_dt.weekday(),
        "symbol_win_rate":  round(confidence_raw, 3),
        "confidence_raw":   round(confidence_raw, 3),
        "sentiment_score":  round(sentiment_score, 3),
        "price_4h_pct":     price_4h_pct,
    }

    if confidence >= 0.90:   pos_pct = 5.0
    elif confidence >= 0.85: pos_pct = 4.0
    elif confidence >= 0.80: pos_pct = 3.0
    elif confidence >= 0.75: pos_pct = 2.0
    else:                    pos_pct = 1.0
    return {
        "symbol":       symbol,
        "direction":    direction,
        "confidence":   confidence,
        "entry_price":  price,
        "target_price": round(target, 6),
        "stop_price":   round(stop, 6),
        "predicted_at": now,
        "expires_at":   now + hold,
        "asset_type":   asset_type,
        "features":     features,
        "pos_size_pct": pos_pct,
    }


def run_prediction_cycle():
    """Run predictions. Returns list of saved picks. Does NOT send Telegram."""
    # T23: Circuit breaker — if last 5 resolved picks are all losses, raise confidence floor
    _cb_floor = CONFIDENCE_FLOOR
    try:
        with db_conn() as _cb:
            _cc = _cb.cursor()
            _cc.execute(
                "SELECT outcome FROM predictions WHERE outcome IN ('WIN','LOSS') AND direction='UP' AND predicted_at > 1742000000 ORDER BY resolved_at DESC LIMIT 5"
            )
            _last5 = [r[0] for r in _cc.fetchall()]
            if len(_last5) == 5 and all(o == 'LOSS' for o in _last5):
                _cb_floor = min(0.92, CONFIDENCE_FLOOR + 0.10)
                LOGGER.warning("T23 CIRCUIT BREAKER: 5 consecutive losses — raising floor to " + str(_cb_floor))
    except Exception: pass
    regime = _check_regime()
    regime['confidence_floor_override'] = _cb_floor
    symbols = ([(s.strip(),"crypto") for s in CRYPTO_SYMBOLS if s.strip()] +
               [(s.strip(),"stock")  for s in STOCK_SYMBOLS  if s.strip()])
    # AUTO-INCLUDE portfolio holdings — if you own it, Ghost watches it
    try:
        with db_conn() as _pc:
            _cur = _pc.cursor()
            _cur.execute("SELECT DISTINCT symbol, asset_type FROM user_portfolio")
            for _sym, _at in _cur.fetchall():
                _sym = _sym.strip().upper()
                _at = (_at or "stock").strip()
                if not any(s == _sym for s, _ in symbols):
                    symbols.append((_sym, _at))
                    LOGGER.info("Portfolio symbol added to scan: " + _sym)
    except Exception as _pe:
        LOGGER.warning("Could not load portfolio symbols: " + str(_pe))
    all_picks = []
    for symbol, asset_type in symbols:
        pick = predict_symbol(symbol, asset_type, regime)
        if pick:
            all_picks.append(pick)

    all_picks.sort(key=lambda x: x["confidence"], reverse=True)
    top = all_picks[:DAILY_CAP]
    saved = []
    # Pre-fetch open symbols once (separate conn avoids cursor state corruption mid-loop)
    _open = set()
    try:
        with db_conn() as _c:
            _x = _c.cursor()
            _x.execute("SELECT DISTINCT symbol FROM predictions WHERE outcome IS NULL AND expires_at > %s", (int(time.time()),))
            _open = {r[0] for r in _x.fetchall()}
    except Exception as _e:
        LOGGER.warning("dedup prefetch failed: " + str(_e))
    with db_conn() as conn:
        cur = conn.cursor()
        for pick in top:
            try:
                if pick["symbol"] in _open:
                    LOGGER.info("DEDUP: skipping " + pick["symbol"])
                    continue
                cur.execute(
                    "INSERT INTO predictions (symbol,direction,confidence,entry_price,target_price,stop_price,run_at,predicted_at,expires_at,asset_type,features) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (pick["symbol"], pick["direction"], pick["confidence"], pick["entry_price"],
                     pick["target_price"], pick["stop_price"], pick["predicted_at"],
                     pick["predicted_at"], pick["expires_at"], pick["asset_type"],
                     json.dumps(pick.get("features", {})))
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
            # Watchdog: fire Telegram alert immediately when pick resolves
            if outcome in ("WIN", "LOSS"):
                try:
                    from core.telegram import send_position_alert
                    usd_out = round(100 * (1 + pnl/100), 2)
                    send_position_alert(symbol, direction, outcome, entry, price, pnl, usd_out)
                except Exception as te:
                    LOGGER.error("Watchdog alert failed: " + str(te))
    return resolved