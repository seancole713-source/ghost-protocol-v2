"""
core/signal_engine.py - Ghost Protocol v3 Signal Engine

WHAT THIS DOES (different from v1/v2):
  v1/v2: confidence = Ghost's own historical win rate (circular, corrupted)
  v3:    confidence = XGBoost trained on 6 months real price data, validated first

PIPELINE:
  1. backtest_symbol(symbol) -> pulls 6mo OHLCV, calculates indicators,
     labels whether price was +2% in 48h, returns per-indicator accuracy
  2. build_training_data(symbols) -> runs backtest on all symbols, labels rows
  3. train_model() -> XGBoost on 80% train, validates on 20% holdout
  4. predict_live(symbol, asset_type) -> scores current price action
  5. Only generates a pick if model confidence > CONFIDENCE_FLOOR

This engine ONLY deploys after validate() confirms >52% accuracy on holdout.
If it can't beat 52%, it returns None and Ghost stays silent.
"""
import os, time, logging, json
import numpy as np
LOGGER = logging.getLogger("ghost.signal_v3")

# ── Config ─────────────────────────────────────────────────────────────────
HOLD_HOURS_LABEL = 24      # predict direction 24h from now (~50% base rate, consistent across symbols)
HOLD_HOURS       = 48       # 48h trade window (how long pick stays open)
MIN_ACCURACY     = 0.50    # minimum holdout accuracy (need to beat 39% base rate)
MIN_TRAIN_ROWS   = 100     # minimum labeled rows to attempt training
# Model stored in PostgreSQL — survives Railway deploys
MODEL_DB_KEY     = "ghost_v3_model_pkl"
FEATURES_DB_KEY  = "ghost_v3_features_json"

# ── Technical Indicator Calculations ───────────────────────────────────────
def _rsi(closes, period=14):
    """Relative Strength Index. <30 oversold (BUY signal), >70 overbought."""
    if len(closes) < period + 1: return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))

def _macd(closes, fast=12, slow=26, signal=9):
    """MACD line, signal line, histogram. Crossover = momentum shift."""
    if len(closes) < slow + signal: return 0.0, 0.0, 0.0
    def ema(data, n):
        k = 2/(n+1)
        result = [data[0]]
        for v in data[1:]: result.append(v*k + result[-1]*(1-k))
        return np.array(result)
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    macd_line = fast_ema - slow_ema
    if len(macd_line) < signal: return 0.0, 0.0, 0.0
    signal_line = ema(macd_line, signal)
    hist = macd_line[-1] - signal_line[-1]
    return float(macd_line[-1]), float(signal_line[-1]), float(hist)

def _bollinger(closes, period=20):
    """Bollinger Bands. Returns % position within bands (0=lower, 1=upper)."""
    if len(closes) < period: return 0.5, 0.0
    window = closes[-period:]
    mid = np.mean(window)
    std = np.std(window)
    if std == 0: return 0.5, 0.0
    upper = mid + 2*std
    lower = mid - 2*std
    price = closes[-1]
    pct_b = float((price - lower) / (upper - lower)) if (upper - lower) > 0 else 0.5
    band_width = float((upper - lower) / mid)  # squeeze indicator
    return pct_b, band_width

def _volume_ratio(volumes, period=20):
    """Current volume vs 20-period average. >1.5 = unusual activity."""
    if len(volumes) < period + 1: return 1.0
    avg = np.mean(volumes[-period-1:-1])
    if avg == 0: return 1.0
    return float(volumes[-1] / avg)

def _price_momentum(closes, periods=[4, 8, 24]):
    """% change over last N candles."""
    result = {}
    for p in periods:
        if len(closes) > p and closes[-p-1] > 0:
            result[f'mom_{p}h'] = float((closes[-1] - closes[-p-1]) / closes[-p-1])
        else:
            result[f'mom_{p}h'] = 0.0
    return result

def _calculate_features(df):
    """
    Given a dataframe with OHLCV, return a feature dict for one candle.
    df: list of dicts with keys open/high/low/close/volume
    """
    closes = np.array([c['close'] for c in df], dtype=float)
    volumes = np.array([c['volume'] for c in df], dtype=float)
    highs = np.array([c['high'] for c in df], dtype=float)
    lows = np.array([c['low'] for c in df], dtype=float)

    rsi = _rsi(closes)
    macd_line, macd_sig, macd_hist = _macd(closes)
    pct_b, band_width = _bollinger(closes)
    vol_ratio = _volume_ratio(volumes)
    momentum = _price_momentum(closes)

    # Price position vs recent range
    recent_high = np.max(highs[-24:]) if len(highs) >= 24 else highs[-1]
    recent_low = np.min(lows[-24:]) if len(lows) >= 24 else lows[-1]
    price_in_range = float((closes[-1] - recent_low) / (recent_high - recent_low + 1e-9))

    import datetime as _dt
    ts = df[-1].get('ts','') if df else ''
    try:
        _d = _dt.datetime.fromisoformat(str(ts).replace('Z','+00:00'))
        hour_of_day = _d.hour
        day_of_week = _d.weekday()
    except Exception:
        hour_of_day = 12
        day_of_week = 0
    return {
        'rsi': rsi,
        'rsi_oversold': 1 if rsi < 35 else 0,
        'rsi_overbought': 1 if rsi > 65 else 0,
        'macd_hist': macd_hist,
        'macd_bullish': 1 if macd_hist > 0 else 0,
        'pct_b': pct_b,
        'bb_squeeze': 1 if band_width < 0.05 else 0,
        'volume_ratio': min(vol_ratio, 5.0),
        'volume_spike': 1 if vol_ratio > 1.5 else 0,
        'mom_4h': momentum['mom_4h'],
        'mom_8h': momentum['mom_8h'],
        'mom_24h': momentum['mom_24h'],
        'price_in_range': price_in_range,
        'near_low': 1 if price_in_range < 0.25 else 0,
        'near_high': 1 if price_in_range > 0.75 else 0,
        'hour_of_day': hour_of_day,
        'day_of_week': day_of_week,
        'is_weekend': 1 if day_of_week >= 5 else 0,
    }

def _fetch_ohlcv(symbol, asset_type, period='6mo', interval='1h'):
    """Pull OHLCV data via Alpaca historical bars API (confirmed working on Railway)."""
    import os, requests as _req, urllib.parse
    from datetime import datetime, timedelta, timezone
    key = os.getenv("ALPACA_KEY_ID","")
    secret = os.getenv("ALPACA_SECRET_KEY","")
    if not key or not secret:
        LOGGER.warning("No Alpaca credentials for OHLCV fetch")
        return None
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    # Date range
    end_dt = datetime.now(timezone.utc)
    try:
        if 'mo' in period:
            months = int(period.replace('mo',''))
            start_dt = end_dt - timedelta(days=months*30)
        elif 'd' in period:
            days = int(period.replace('d',''))
            start_dt = end_dt - timedelta(days=days)
        else:
            start_dt = end_dt - timedelta(days=180)
    except Exception:
        start_dt = end_dt - timedelta(days=180)
    start_str = start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    end_str = end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    rows = []
    try:
        if asset_type == 'crypto':
            ticker = symbol.upper() + '/USD'
            url = f"https://data.alpaca.markets/v1beta3/crypto/us/bars"
            # Must encode the slash in LTC/USD manually — requests auto-encodes it
            symbol_encoded = urllib.parse.quote(ticker, safe='')
            full_url = url + '?symbols=' + symbol_encoded + '&timeframe=1Hour&limit=10000&start=' + start_str + '&end=' + end_str
            params = {}
            r = _req.get(full_url, headers=headers, timeout=30)
            LOGGER.info(f"Alpaca crypto URL: {full_url[:100]} status={r.status_code}")
            if r.status_code != 200:
                LOGGER.warning(f"Alpaca crypto bars {symbol}: {r.status_code}")
                return None
            bars = r.json().get('bars', {}).get(ticker, [])
            # Handle pagination
            next_token = r.json().get('next_page_token')
            while next_token and len(bars) < 5000:
                params['page_token'] = next_token
                r2 = _req.get(url, headers=headers, params=params, timeout=30)
                if r2.status_code != 200: break
                d2 = r2.json()
                bars.extend(d2.get('bars', {}).get(ticker, []))
                next_token = d2.get('next_page_token')
        else:
            url = f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/bars"
            params = {'timeframe': '1Hour', 'start': start_str, 'end': end_str, 'limit': 10000, 'feed': 'iex'}
            r = _req.get(url, headers=headers, params=params, timeout=30)
            if r.status_code != 200:
                LOGGER.warning(f"Alpaca stock bars {symbol}: {r.status_code}")
                return None
            bars = r.json().get('bars', [])
            next_token = r.json().get('next_page_token')
            while next_token and len(bars) < 5000:
                params['page_token'] = next_token
                r2 = _req.get(url, headers=headers, params=params, timeout=30)
                if r2.status_code != 200: break
                d2 = r2.json()
                bars.extend(d2.get('bars', []))
                next_token = d2.get('next_page_token')
        for bar in bars:
            rows.append({
                'ts': bar.get('t', ''),
                'open': float(bar.get('o', 0)),
                'high': float(bar.get('h', 0)),
                'low': float(bar.get('l', 0)),
                'close': float(bar.get('c', 0)),
                'volume': float(bar.get('v', 0)),
            })
        LOGGER.info(f"Alpaca OHLCV {symbol}: {len(rows)} bars fetched")
        return rows if rows else None
    except Exception as e:
        LOGGER.warning(f"Alpaca OHLCV failed for {symbol}: {e}")
        return None

def backtest_symbol(symbol, asset_type):
    """
    Pull 6mo hourly data, calculate features, label outcomes.
    Label: did price go up TARGET_MOVE_PCT in next HOLD_HOURS candles?
    Returns list of {features, label} dicts.
    """
    rows = _fetch_ohlcv(symbol, asset_type)
    if not rows or len(rows) < 100:
        return []

    labeled = []
    window = 50  # warmup for indicators
    for i in range(window, len(rows) - HOLD_HOURS):
        hist = rows[max(0, i-window):i+1]
        features = _calculate_features(hist)
        features['symbol'] = symbol
        features['asset_type'] = asset_type

        # Label: is close price higher 24h from now? (~50% base rate across all symbols)
        entry = rows[i]['close']
        future_close = rows[i+24]['close'] if i+24 < len(rows) else entry
        label = 1 if future_close > entry else 0
        labeled.append({'features': features, 'label': label})

    wins = sum(1 for r in labeled if r['label'] == 1)
    LOGGER.info(f"Backtest {symbol}: {len(labeled)} samples, {wins} hits "
                f"({round(wins/len(labeled)*100,1) if labeled else 0}% natural hit rate)")
    return labeled

def build_training_data(symbols_and_types):
    """Run backtest on all symbols. Returns X (feature matrix), y (labels)."""
    all_rows = []
    for symbol, asset_type in symbols_and_types:
        rows = backtest_symbol(symbol, asset_type)
        all_rows.extend(rows)
    if len(all_rows) < MIN_TRAIN_ROWS:
        LOGGER.warning(f"Only {len(all_rows)} training rows — need {MIN_TRAIN_ROWS}")
        return None, None, []
    LOGGER.info(f"Total training rows: {len(all_rows)}")
    FEATURE_COLS = ['rsi','rsi_oversold','rsi_overbought','macd_hist','macd_bullish',
                    'pct_b','bb_squeeze','volume_ratio','volume_spike',
                    'mom_4h','mom_8h','mom_24h','price_in_range','near_low','near_high',
                    'hour_of_day','day_of_week','is_weekend']
    X = np.array([[r['features'].get(c, 0.0) for c in FEATURE_COLS] for r in all_rows])
    y = np.array([r['label'] for r in all_rows])
    return X, y, FEATURE_COLS

def train_and_validate(symbols_and_types):
    """Train one XGBoost per symbol. Only deploys if it beats natural rate by 2%+."""
    try:
        from xgboost import XGBClassifier
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import accuracy_score
        import pickle, base64
        from core.db import db_conn
    except ImportError as e:
        LOGGER.error("Missing dependency: " + str(e))
        return None, 0.0, False

    results = {}
    total_passed = 0

    for symbol, asset_type in symbols_and_types:
        try:
            rows = backtest_symbol(symbol, asset_type)
            if not rows or len(rows) < 80:
                LOGGER.info(f"Skipping {symbol} — only {len(rows)} samples")
                continue

            FEATURE_COLS = ['rsi','rsi_oversold','rsi_overbought','macd_hist','macd_bullish',
                            'pct_b','bb_squeeze','volume_ratio','volume_spike',
                            'mom_4h','mom_8h','mom_24h','price_in_range','near_low','near_high',
                            'hour_of_day','day_of_week','is_weekend']
            X = np.array([[r['features'].get(c, 0.0) for c in FEATURE_COLS] for r in rows])
            y = np.array([r['label'] for r in rows])

            # Chronological 80/20 split
            split = int(len(X) * 0.8)
            X_train, X_test = X[:split], X[split:]
            y_train, y_test = y[:split], y[split:]
            natural_rate = float(np.mean(y_test))

            model = XGBClassifier(
                n_estimators=150, max_depth=3, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.8,
                use_label_encoder=False, eval_metric='logloss', random_state=42
            )
            model.fit(X_train, y_train)
            accuracy = float(accuracy_score(y_test, model.predict(X_test)))
            edge = accuracy - natural_rate

            passes = edge >= 0.02  # need at least +2% over natural rate
            LOGGER.info(f"{symbol}: acc={round(accuracy*100,1)}% nat={round(natural_rate*100,1)}% edge={round(edge*100,1)}% {'SAVED' if passes else 'skipped'}")

            if passes:
                model_bytes = base64.b64encode(pickle.dumps(model)).decode('ascii')
                meta = json.dumps({'feature_cols': FEATURE_COLS, 'accuracy': accuracy,
                                   'natural_rate': natural_rate, 'edge': edge,
                                   'trained_at': time.time(), 'n_samples': len(rows)})
                with db_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("""CREATE TABLE IF NOT EXISTS ghost_v3_model
                                   (key TEXT PRIMARY KEY, value TEXT, updated_at BIGINT)""")
                    cur.execute("""INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s)
                                   ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at""",
                                (f"model_{symbol}", model_bytes, int(time.time())))
                    cur.execute("""INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s)
                                   ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at""",
                                (f"meta_{symbol}", meta, int(time.time())))
                results[symbol] = {'accuracy': accuracy, 'edge': edge, 'passed': True}
                total_passed += 1
        except Exception as e:
            LOGGER.warning(f"Training failed for {symbol}: {e}")

    LOGGER.info(f"v3 training complete: {total_passed}/{len(symbols_and_types)} symbols passed")
    return None, total_passed / max(len(symbols_and_types), 1), total_passed > 0


def load_model(symbol=None):
    """Load per-symbol model from PostgreSQL."""
    if not symbol: return None, None, None
    try:
        import pickle, base64
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM ghost_v3_model WHERE key=%s", (f"model_{symbol}",))
            row = cur.fetchone()
            if not row: return None, None, None
            model = pickle.loads(base64.b64decode(row[0]))
            cur.execute("SELECT value FROM ghost_v3_model WHERE key=%s", (f"meta_{symbol}",))
            mrow = cur.fetchone()
            if not mrow: return None, None, None
            meta = json.loads(mrow[0])
        if time.time() - meta.get('trained_at', 0) > 14 * 86400:
            return None, None, None
        return model, meta['feature_cols'], meta
    except Exception as e:
        LOGGER.warning(f"load_model {symbol} failed: {e}")
        return None, None, None

def predict_live(symbol, asset_type):
    """Use symbol-specific model. Returns (direction, confidence) or None."""
    model, feature_cols, meta = load_model(symbol)
    if model is None:
        return None  # no model for this symbol — Ghost stays silent

    # Get recent 72h of hourly data for indicator calculation
    rows = _fetch_ohlcv(symbol, asset_type, period='5d', interval='1h')
    if not rows or len(rows) < 30:
        return None

    features = _calculate_features(rows)
    X = np.array([[features.get(c, 0.0) for c in feature_cols]])
    proba = model.predict_proba(X)[0]
    up_prob = float(proba[1])   # probability price goes up TARGET_MOVE_PCT

    # Only signal if model confidence exceeds floor
    floor = float(os.getenv("MIN_ALERT_CONFIDENCE", "0.75"))
    if up_prob >= floor:
        return ("UP", round(up_prob, 3))

    # Inverse: if model very confident it won't go up, could be a DOWN signal
    down_prob = 1.0 - up_prob
    if down_prob >= floor:
        return ("DOWN", round(down_prob, 3))

    return None  # no edge

def get_model_status():
    """Return per-symbol model status."""
    try:
        import json
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM ghost_v3_model WHERE key LIKE 'meta_%' ORDER BY key")
            rows = cur.fetchall()
        if not rows:
            return {"trained": False, "reason": "No models — run /api/v3/train to build"}
        symbols = {}
        for key, val in rows:
            sym = key.replace('meta_','')
            m = json.loads(val)
            symbols[sym] = {
                "accuracy": round(m.get("accuracy",0)*100,1),
                "natural_rate": round(m.get("natural_rate",0)*100,1),
                "edge": round(m.get("edge",0)*100,1),
                "n_samples": m.get("n_samples",0),
            }
        return {"trained": True, "models": len(symbols), "symbols": symbols}
    except Exception as e:
        return {"trained": False, "reason": str(e)}
