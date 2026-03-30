""" core/signal_engine.py - Ghost Protocol v3.1 Signal Engine
UPGRADES (2026-03-30) from GitHub research:
  - EMA(20/50/200): no BUY below EMA200 (FarisZnf repo - #1 fix)
  - ADX(14): no BUY in choppy/sideways markets ADX<20 (FarisZnf repo)
  - ATR: volatility-aware feature for XGBoost
  - OBV slope: accumulation/distribution signal
  - Stochastic %K/%D: momentum confirmation
  - Regime gate in predict_live blocking BUYs in downtrend+choppy
  - Better XGBoost hyperparams: 200 estimators, depth 4, min_child 3
"""
import os, time, logging, json
import numpy as np
LOGGER = logging.getLogger("ghost.signal_v3")

HOLD_HOURS_LABEL = 24
HOLD_HOURS = 48
MIN_ACCURACY = 0.50
MIN_TRAIN_ROWS = 80
MODEL_DB_KEY = "ghost_v3_model_pkl"
FEATURES_DB_KEY = "ghost_v3_features_json"

def _rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0: return 100.0
    return float(100 - (100 / (1 + avg_gain / avg_loss)))

def _macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal: return 0.0, 0.0, 0.0
    def ema(data, n):
        k = 2/(n+1); r = [data[0]]
        for v in data[1:]: r.append(v*k + r[-1]*(1-k))
        return np.array(r)
    ml = ema(closes, fast) - ema(closes, slow)
    if len(ml) < signal: return 0.0, 0.0, 0.0
    sl = ema(ml, signal)
    return float(ml[-1]), float(sl[-1]), float(ml[-1] - sl[-1])

def _bollinger(closes, period=20):
    if len(closes) < period: return 0.5, 0.0
    w = closes[-period:]; mid = np.mean(w); std = np.std(w)
    if std == 0: return 0.5, 0.0
    upper = mid + 2*std; lower = mid - 2*std
    pct_b = float((closes[-1] - lower) / (upper - lower)) if (upper - lower) > 0 else 0.5
    return pct_b, float((upper - lower) / mid)

def _volume_ratio(volumes, period=20):
    if len(volumes) < period + 1: return 1.0
    avg = np.mean(volumes[-period-1:-1])
    return float(volumes[-1] / avg) if avg > 0 else 1.0

def _price_momentum(closes, periods=[1, 3, 5]):
    result = {}
    for p in periods:
        if len(closes) > p and closes[-p-1] > 0:
            result[f'mom_{p}h'] = float((closes[-1] - closes[-p-1]) / closes[-p-1])
        else:
            result[f'mom_{p}h'] = 0.0
    return result

def _ema(closes, period):
    if len(closes) < 2: return float(closes[-1])
    k = 2.0 / (period + 1); v = float(closes[0])
    for c in closes[1:]: v = c * k + v * (1 - k)
    return v

def _adx(highs, lows, closes, period=14):
    if len(closes) < period * 2: return 25.0
    trs, pdms, ndms = [], [], []
    for i in range(1, len(closes)):
        h, l, pc = highs[i], lows[i], closes[i-1]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
        ph = highs[i] - highs[i-1]; nl = lows[i-1] - lows[i]
        pdms.append(max(ph, 0) if ph > nl else 0)
        ndms.append(max(nl, 0) if nl > ph else 0)
    def wilder(data, p):
        s = sum(data[:p]); r = [s]
        for v in data[p:]: s = s - s/p + v; r.append(s)
        return r
    dxs = []
    for a, p, n in zip(wilder(trs, period), wilder(pdms, period), wilder(ndms, period)):
        if a == 0: continue
        pdi, ndi = 100*p/a, 100*n/a
        if pdi + ndi == 0: continue
        dxs.append(100 * abs(pdi - ndi) / (pdi + ndi))
    return float(np.mean(dxs[-period:])) if dxs else 25.0

def _atr(highs, lows, closes, period=14):
    if len(closes) < period + 1: return float(closes[-1] * 0.02)
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, len(closes))]
    return float(np.mean(trs[-period:]))

def _obv_slope(closes, volumes, period=10):
    if len(closes) < period + 1: return 0.0
    obv = 0.0; obvs = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]: obv += volumes[i]
        elif closes[i] < closes[i-1]: obv -= volumes[i]
        obvs.append(obv)
    r = obvs[-period:]
    if len(r) < 2: return 0.0
    slope = (r[-1] - r[0]) / (len(r) * max(abs(r[0]), 1e-9))
    return float(np.clip(slope, -1.0, 1.0))

def _stochastic(highs, lows, closes, k_period=14, d_period=3):
    if len(closes) < k_period: return 50.0, 50.0
    ks = []
    for i in range(k_period-1, len(closes)):
        hh = max(highs[i-k_period+1:i+1]); ll = min(lows[i-k_period+1:i+1])
        ks.append(100*(closes[i]-ll)/(hh-ll) if hh != ll else 50.0)
    k = ks[-1]
    d = float(np.mean(ks[-d_period:])) if len(ks) >= d_period else k
    return float(k), float(d)

def _calculate_features(df):
    closes = np.array([c['close'] for c in df], dtype=float)
    volumes = np.array([c['volume'] for c in df], dtype=float)
    highs = np.array([c['high'] for c in df], dtype=float)
    lows = np.array([c['low'] for c in df], dtype=float)

    rsi = _rsi(closes)
    macd_line, macd_sig, macd_hist = _macd(closes)
    pct_b, band_width = _bollinger(closes)
    vol_ratio = _volume_ratio(volumes)
    momentum = _price_momentum(closes)
    rh = np.max(highs[-24:]) if len(highs) >= 24 else highs[-1]
    rl = np.min(lows[-24:]) if len(lows) >= 24 else lows[-1]
    price_in_range = float((closes[-1] - rl) / (rh - rl + 1e-9))

    import datetime as _dt
    ts = df[-1].get('ts','') if df else ''
    try:
        _d = _dt.datetime.fromisoformat(str(ts).replace('Z','+00:00'))
        hod, dow = _d.hour, _d.weekday()
    except:
        hod, dow = 12, 0

    cur = float(closes[-1])
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50) if len(closes) >= 50 else cur
    ema200 = _ema(closes, 200) if len(closes) >= 200 else cur
    adx = _adx(highs, lows, closes)
    atr = _atr(highs, lows, closes)
    obv_slope = _obv_slope(closes, volumes)
    stoch_k, stoch_d = _stochastic(highs, lows, closes)

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
        'mom_4h': momentum['mom_1h'],
        'mom_8h': momentum['mom_3h'],
        'mom_24h': momentum['mom_5h'],
        'price_in_range': price_in_range,
        'near_low': 1 if price_in_range < 0.25 else 0,
        'near_high': 1 if price_in_range > 0.75 else 0,
        'hour_of_day': hod,
        'day_of_week': dow,
        'is_weekend': 1 if dow >= 5 else 0,
        'above_ema20': 1 if cur > ema20 else 0,
        'above_ema50': 1 if cur > ema50 else 0,
        'above_ema200': 1 if cur > ema200 else 0,
        'ema_trend_bullish': 1 if (ema20 > ema50 and ema50 > ema200) else 0,
        'ema20_vs_ema50': float((ema20 - ema50) / ema50) if ema50 > 0 else 0.0,
        'adx': adx,
        'adx_trending': 1 if adx > 20 else 0,
        'adx_strong': 1 if adx > 30 else 0,
        'atr_pct': float(atr / cur) if cur > 0 else 0.02,
        'obv_slope': obv_slope,
        'obv_accumulating': 1 if obv_slope > 0 else 0,
        'stoch_k': stoch_k,
        'stoch_d': stoch_d,
        'stoch_oversold': 1 if stoch_k < 20 else 0,
        'stoch_overbought': 1 if stoch_k > 80 else 0,
    }

FEATURE_COLS = [
    'rsi','rsi_oversold','rsi_overbought','macd_hist','macd_bullish',
    'pct_b','bb_squeeze','volume_ratio','volume_spike',
    'mom_4h','mom_8h','mom_24h','price_in_range','near_low','near_high',
    'hour_of_day','day_of_week','is_weekend',
    'above_ema20','above_ema50','above_ema200','ema_trend_bullish','ema20_vs_ema50',
    'adx','adx_trending','adx_strong',
    'atr_pct',
    'obv_slope','obv_accumulating',
    'stoch_k','stoch_d','stoch_oversold','stoch_overbought',
]

def _fetch_ohlcv(symbol, asset_type, period='2y', interval='1d'):
    import os, requests as _req
    from datetime import datetime, timedelta, timezone
    key = os.getenv("ALPACA_KEY_ID",""); secret = os.getenv("ALPACA_SECRET_KEY","")
    if not key or not secret: return None
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    end_dt = datetime.now(timezone.utc); start_dt = end_dt - timedelta(days=730)
    start_str = start_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    end_str = end_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        if asset_type == 'crypto':
            import urllib.parse
            ticker = symbol.upper() + '/USD'
            ticker_enc = urllib.parse.quote(ticker, safe='')
            url = (f"https://data.alpaca.markets/v1beta3/crypto/us/bars"
                   f"?symbols={ticker_enc}&timeframe=1Day&limit=1000"
                   f"&start={start_str}&end={end_str}")
            r = _req.get(url, headers=headers, timeout=30)
            if r.status_code != 200: return None
            bars = r.json().get('bars', {}).get(ticker, [])
        else:
            url = (f"https://data.alpaca.markets/v2/stocks/{symbol.upper()}/bars"
                   f"?timeframe=1Day&limit=1000&feed=iex"
                   f"&start={start_str}&end={end_str}")
            r = _req.get(url, headers=headers, timeout=30)
            if r.status_code != 200: return None
            bars = r.json().get('bars', [])
        rows = [{'ts': b.get('t',''), 'open': float(b.get('o',0)),
                 'high': float(b.get('h',0)), 'low': float(b.get('l',0)),
                 'close': float(b.get('c',0)), 'volume': float(b.get('v',0))}
                for b in bars if b.get('c',0) > 0]
        LOGGER.info(f"Alpaca daily {symbol}: {len(rows)} bars")
        return rows if rows else None
    except Exception as e:
        LOGGER.warning(f"Alpaca fetch failed {symbol}: {e}"); return None

def backtest_symbol(symbol, asset_type):
    rows = _fetch_ohlcv(symbol, asset_type)
    if not rows or len(rows) < 100: return []
    labeled = []; window = 220
    for i in range(window, len(rows) - HOLD_HOURS):
        hist = rows[max(0, i-window):i+1]
        features = _calculate_features(hist)
        features['symbol'] = symbol; features['asset_type'] = asset_type
        entry = rows[i]['close']
        future_close = rows[i+1]['close'] if i+1 < len(rows) else entry
        labeled.append({'features': features, 'label': 1 if future_close > entry else 0})
    wins = sum(1 for r in labeled if r['label'] == 1)
    LOGGER.info(f"Backtest {symbol}: {len(labeled)} samples, "
                f"{round(wins/len(labeled)*100,1) if labeled else 0}% natural rate")
    return labeled

def build_training_data(symbols_and_types):
    all_rows = []
    for symbol, asset_type in symbols_and_types:
        all_rows.extend(backtest_symbol(symbol, asset_type))
    if len(all_rows) < MIN_TRAIN_ROWS: return None, None, []
    X = np.array([[r['features'].get(c, 0.0) for c in FEATURE_COLS] for r in all_rows])
    y = np.array([r['label'] for r in all_rows])
    return X, y, FEATURE_COLS

def train_and_validate(symbols_and_types):
    try:
        from xgboost import XGBClassifier
        from sklearn.metrics import accuracy_score
        import pickle, base64
        from core.db import db_conn
    except ImportError as e:
        LOGGER.error("Missing dep: "+str(e)); return None, 0.0, False
    total_passed = 0
    for symbol, asset_type in symbols_and_types:
        try:
            rows = backtest_symbol(symbol, asset_type)
            if not rows or len(rows) < 80: continue
            X = np.array([[r['features'].get(c, 0.0) for c in FEATURE_COLS] for r in rows])
            y = np.array([r['label'] for r in rows])
            split = int(len(X) * 0.8)
            X_train, X_test = X[:split], X[split:]
            y_train, y_test = y[:split], y[split:]
            natural_rate = float(np.mean(y_test))
            model = XGBClassifier(
                n_estimators=200, max_depth=4, learning_rate=0.03,
                subsample=0.8, colsample_bytree=0.7, min_child_weight=3,
                use_label_encoder=False, eval_metric='logloss', random_state=42
            )
            model.fit(X_train, y_train)
            accuracy = float(accuracy_score(y_test, model.predict(X_test)))
            edge = accuracy - natural_rate
            passes = edge >= 0.02
            LOGGER.info(f"{symbol}: acc={round(accuracy*100,1)}% nat={round(natural_rate*100,1)}% "
                       f"edge={round(edge*100,1)}% {'SAVED' if passes else 'skipped'}")
            if passes:
                model_bytes = base64.b64encode(pickle.dumps(model)).decode('ascii')
                meta = json.dumps({'feature_cols': FEATURE_COLS, 'accuracy': accuracy,
                                   'natural_rate': natural_rate, 'edge': edge,
                                   'trained_at': time.time(), 'n_samples': len(rows),
                                   'engine_version': 'v3.1_ema_adx_atr_obv_stoch'})
                with db_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("CREATE TABLE IF NOT EXISTS ghost_v3_model "
                                "(key TEXT PRIMARY KEY, value TEXT, updated_at BIGINT)")
                    cur.execute("INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s) "
                                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
                                (f"model_{symbol}", model_bytes, int(time.time())))
                    cur.execute("INSERT INTO ghost_v3_model(key,value,updated_at) VALUES(%s,%s,%s) "
                                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
                                (f"meta_{symbol}", meta, int(time.time())))
                total_passed += 1
        except Exception as e:
            LOGGER.warning(f"Training failed {symbol}: {e}")
    LOGGER.info(f"v3.1 training: {total_passed}/{len(symbols_and_types)} passed")
    return None, total_passed / max(len(symbols_and_types), 1), total_passed > 0

def load_model(symbol=None):
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
            if time.time() - meta.get('trained_at', 0) > 14 * 86400: return None, None, None
            return model, meta.get('feature_cols', FEATURE_COLS), meta
    except Exception as e:
        LOGGER.warning(f"load_model {symbol}: {e}"); return None, None, None

def predict_live(symbol, asset_type):
    """
    Regime gate (jakejk1285 + FarisZnf research):
    - Skip BUY if price below EMA200 AND ADX<20 (downtrend + choppy)
    - Skip BUY if full bearish EMA alignment (20<50<200) unless deep oversold
    """
    model, feature_cols, meta = load_model(symbol)
    if model is None: return None

    rows = _fetch_ohlcv(symbol, asset_type, period='5d', interval='1h')
    if not rows or len(rows) < 30: return None
    features = _calculate_features(rows)

    above_ema200 = features.get('above_ema200', 1)
    adx_trending = features.get('adx_trending', 1)
    adx_val = features.get('adx', 25)
    ema_trend_bullish = features.get('ema_trend_bullish', 1)
    rsi = features.get('rsi', 50)
    stoch_k = features.get('stoch_k', 50)

    # Gate 1: below EMA200 + choppy = high-probability loss setup
    if above_ema200 == 0 and adx_trending == 0:
        LOGGER.info(f"REGIME GATE [{symbol}]: below EMA200 + ADX={adx_val:.1f}<20 — skip BUY")
        return None

    # Gate 2: full bearish alignment, not oversold
    if ema_trend_bullish == 0 and rsi > 40 and stoch_k > 30:
        LOGGER.info(f"REGIME GATE [{symbol}]: bearish EMA stack, RSI={rsi:.1f} not oversold — skip")
        return None

    X = np.array([[features.get(c, 0.0) for c in feature_cols]])
    proba = model.predict_proba(X)[0]
    up_prob = float(proba[1])
    edge = meta.get('edge', 0)
    if edge < 0.02: return None

    if up_prob > 0.50:
        conf = round(min(0.95, max(0.75, 0.75 + (up_prob-0.50)*2.0 + edge*0.5)), 3)
        return ("UP", conf)
    elif up_prob < 0.50:
        conf = round(min(0.95, max(0.75, 0.75 + (0.50-up_prob)*2.0 + edge*0.5)), 3)
        return ("DOWN", conf)
    return None

def get_model_status():
    try:
        from core.db import db_conn
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM ghost_v3_model WHERE key LIKE 'meta_%' ORDER BY key")
            rows = cur.fetchall()
            if not rows: return {"trained": False, "reason": "No models — run /api/v3/train"}
            symbols = {}
            for key, val in rows:
                sym = key.replace('meta_',''); m = json.loads(val)
                symbols[sym] = {
                    "accuracy": round(m.get("accuracy",0)*100,1),
                    "natural_rate": round(m.get("natural_rate",0)*100,1),
                    "edge": round(m.get("edge",0)*100,1),
                    "n_samples": m.get("n_samples",0),
                    "engine": m.get("engine_version","v3.0"),
                }
            return {"trained": True, "models": len(symbols), "symbols": symbols}
    except Exception as e:
        return {"trained": False, "reason": str(e)}
