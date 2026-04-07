""" core/signal_engine.py - Ghost Protocol v3.2 Signal Engine
UPGRADES (2026-03-30) from GitHub research:
  - EMA(20/50/200): no BUY below EMA200 (FarisZnf repo - #1 fix)
  - ADX(14): no BUY in choppy/sideways markets ADX<20 (FarisZnf repo)
  - ATR: volatility-aware feature for XGBoost
  - OBV slope: accumulation/distribution signal
  - Stochastic %K/%D: momentum confirmation
  - Regime gate in predict_live blocking BUYs in downtrend+choppy
  - Better XGBoost hyperparams: 200 estimators, depth 4, min_child 3

v3.2: Training labels match live paper trades — WIN = hit vol-based target before stop
within N daily bars (see V3_LABEL_HOLD_BARS), same TP/SL math as core.vol_targets.
"""
import os, time, logging, json
import numpy as np
from core.vol_targets import base_vol_pct, stop_pct_from_vol

LOGGER = logging.getLogger("ghost.signal_v3")

LABEL_TYPE = "tp_sl_daily"
# Daily bars only: approximate 48h crypto hold with this many forward bars (24h each).
V3_LABEL_HOLD_BARS = max(1, int(os.getenv("V3_LABEL_HOLD_BARS", "3")))
MIN_TRAIN_ROWS = 80
MODEL_DB_KEY = "ghost_v3_model_pkl"
FEATURES_DB_KEY = "ghost_v3_features_json"


def _v3_min_holdout_acc() -> float:
    return float(os.getenv("V3_MIN_HOLDOUT_ACC", "0.55"))


def _v3_min_edge() -> float:
    return float(os.getenv("V3_MIN_EDGE", "0.05"))


def _v3_min_win_proba() -> float:
    return float(os.getenv("V3_MIN_WIN_PROBA", "0.55"))


def _v3_min_tp_sl_wins() -> int:
    return max(5, int(os.getenv("V3_MIN_TP_SL_WINS", "15")))


def _v3_min_wf_folds() -> int:
    return max(2, int(os.getenv("V3_MIN_WF_FOLDS", "3")))


def _v3_min_wf_acc_mean() -> float:
    return float(os.getenv("V3_MIN_WF_ACC_MEAN", "0.60"))


def _walk_forward_scores(X, y):
    """
    Rolling walk-forward validation over time-ordered samples.
    Returns dict with fold_count / mean and minimum fold scores.
    """
    from xgboost import XGBClassifier
    from sklearn.metrics import accuracy_score

    n = len(X)
    min_train = max(120, int(n * 0.50))
    test_size = max(20, int(n * 0.10))
    step = test_size
    folds = []
    start = min_train
    while start + test_size <= n:
        X_train, y_train = X[:start], y[:start]
        X_test, y_test = X[start : start + test_size], y[start : start + test_size]
        if len(X_train) < 60 or len(X_test) < 20:
            start += step
            continue
        pos_ct = int(np.sum(y_train))
        neg_ct = int(len(y_train) - pos_ct)
        if pos_ct <= 0:
            start += step
            continue
        natural_rate = float(np.mean(y_test))
        model = XGBClassifier(
            n_estimators=200,
            max_depth=4,
            learning_rate=0.03,
            subsample=0.8,
            colsample_bytree=0.7,
            min_child_weight=3,
            scale_pos_weight=min(25.0, max(1.0, float(neg_ct / pos_ct))),
            eval_metric="logloss",
            random_state=42,
        )
        model.fit(X_train, y_train)
        acc = float(accuracy_score(y_test, model.predict(X_test)))
        folds.append({"acc": acc, "nat": natural_rate, "edge": acc - natural_rate})
        start += step

    if not folds:
        return {"fold_count": 0, "acc_mean": 0.0, "acc_min": 0.0, "edge_mean": 0.0, "edge_min": 0.0}

    return {
        "fold_count": len(folds),
        "acc_mean": float(np.mean([f["acc"] for f in folds])),
        "acc_min": float(np.min([f["acc"] for f in folds])),
        "edge_mean": float(np.mean([f["edge"] for f in folds])),
        "edge_min": float(np.min([f["edge"] for f in folds])),
    }


def _simulate_up_tp_sl(rows: list, entry_idx: int, hold_bars: int, vol_pct: float) -> str:
    """
    Path simulation on daily OHLC: UP trade from rows[entry_idx] close.
    Conservative same-bar rule: if both stop and target are touched, count LOSS.
    Returns WIN | LOSS | EXPIRED (mirrors live reconcile when expiry ends without hit).
    """
    entry = float(rows[entry_idx]["close"])
    if entry <= 0:
        return "EXPIRED"
    target = entry * (1 + vol_pct)
    stop = entry * (1 - stop_pct_from_vol(vol_pct))
    last = min(len(rows) - 1, entry_idx + hold_bars)
    for j in range(entry_idx + 1, last + 1):
        lo = float(rows[j]["low"])
        hi = float(rows[j]["high"])
        hit_stop = lo <= stop
        hit_tgt = hi >= target
        if hit_stop and hit_tgt:
            return "LOSS"
        if hit_stop:
            return "LOSS"
        if hit_tgt:
            return "WIN"
    return "EXPIRED"

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
    if not rows or len(rows) < 100:
        return []
    vol_pct = base_vol_pct(symbol, asset_type)
    labeled = []
    window = 220
    margin = V3_LABEL_HOLD_BARS + 1
    for i in range(window, len(rows) - margin):
        hist = rows[max(0, i - window) : i + 1]
        features = _calculate_features(hist)
        features["symbol"] = symbol
        features["asset_type"] = asset_type
        outcome = _simulate_up_tp_sl(rows, i, V3_LABEL_HOLD_BARS, vol_pct)
        labeled.append({"features": features, "label": 1 if outcome == "WIN" else 0, "outcome": outcome})
    wins = sum(1 for r in labeled if r["label"] == 1)
    LOGGER.info(
        f"Backtest {symbol}: {len(labeled)} samples (TP/SL labels, {V3_LABEL_HOLD_BARS}d bars), "
        f"{round(wins/len(labeled)*100,1) if labeled else 0}% natural WIN rate"
    )
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
            n_samples = len(rows) if rows else 0
            if not rows or n_samples < 80:
                LOGGER.info(
                    f"RETRAIN [{symbol}]: acc=NA edge=NA wf_folds=NA wf_acc_mean=NA wf_edge_mean=NA wf_acc_min=NA "
                    f"| FAIL: n_samples<{MIN_TRAIN_ROWS} ({n_samples})"
                )
                continue
            X = np.array([[r["features"].get(c, 0.0) for c in FEATURE_COLS] for r in rows])
            y = np.array([r["label"] for r in rows])
            wins_ct = int(np.sum(y))
            min_wins = _v3_min_tp_sl_wins()
            split = int(len(X) * 0.8)
            X_train, X_test = X[:split], X[split:]
            y_train, y_test = y[:split], y[split:]
            natural_rate = float(np.mean(y_test))
            pos_ct = int(np.sum(y_train))
            neg_ct = int(len(y_train) - pos_ct)
            spw = (neg_ct / pos_ct) if pos_ct > 0 else 1.0
            min_acc = _v3_min_holdout_acc()
            min_edge = _v3_min_edge()
            model = XGBClassifier(
                n_estimators=200, max_depth=4, learning_rate=0.03,
                subsample=0.8, colsample_bytree=0.7, min_child_weight=3,
                scale_pos_weight=min(25.0, max(1.0, float(spw))),
                eval_metric='logloss', random_state=42
            )
            model.fit(X_train, y_train)
            accuracy = float(accuracy_score(y_test, model.predict(X_test)))
            edge = accuracy - natural_rate
            wf = _walk_forward_scores(X, y)
            min_wf_acc = _v3_min_wf_acc_mean()
            min_wf_folds = _v3_min_wf_folds()
            min_wf_acc_min = (min_wf_acc - 0.03)
            gate_checks = [
                ("n_samples", n_samples >= MIN_TRAIN_ROWS, f"n_samples<{MIN_TRAIN_ROWS} ({n_samples})"),
                ("tp_sl_wins", wins_ct >= min_wins, f"tp_sl_wins<{min_wins} ({wins_ct})"),
                ("holdout_acc", accuracy >= min_acc, f"holdout_acc < {min_acc*100:.1f}% ({accuracy*100:.1f}%)"),
                ("edge", edge >= min_edge, f"edge < {min_edge*100:.1f}% ({edge*100:.1f}%)"),
                ("wf_folds", wf["fold_count"] >= min_wf_folds, f"wf_folds < {min_wf_folds} ({wf['fold_count']})"),
                ("wf_acc_mean", wf["acc_mean"] >= min_wf_acc, f"wf_acc_mean < {min_wf_acc*100:.1f}% ({wf['acc_mean']*100:.1f}%)"),
                ("wf_edge_mean", wf["edge_mean"] >= min_edge, f"wf_edge_mean < {min_edge*100:.1f}% ({wf['edge_mean']*100:.1f}%)"),
                ("wf_acc_min", wf["acc_min"] >= min_wf_acc_min, f"wf_acc_min < {min_wf_acc_min*100:.1f}% ({wf['acc_min']*100:.1f}%)"),
            ]
            fail_reason = next((msg for _, ok, msg in gate_checks if not ok), None)
            passes = fail_reason is None
            LOGGER.info(
                f"RETRAIN [{symbol}]: acc={accuracy*100:.1f}% edge={edge*100:.1f}% "
                f"wf_folds={wf['fold_count']} wf_acc_mean={wf['acc_mean']*100:.1f}% "
                f"wf_edge_mean={wf['edge_mean']*100:.1f}% wf_acc_min={wf['acc_min']*100:.1f}% "
                f"| {'PASS' if passes else 'FAIL: ' + fail_reason}"
            )
            if passes:
                model_bytes = base64.b64encode(pickle.dumps(model)).decode('ascii')
                meta = json.dumps({
                    "feature_cols": FEATURE_COLS, "accuracy": accuracy,
                    "natural_rate": natural_rate, "edge": edge,
                    "trained_at": time.time(), "n_samples": len(rows),
                    "engine_version": "v3.2_tp_sl_daily",
                    "label_type": LABEL_TYPE,
                    "label_hold_bars": V3_LABEL_HOLD_BARS,
                    "wf_fold_count": wf["fold_count"],
                    "wf_acc_mean": wf["acc_mean"],
                    "wf_acc_min": wf["acc_min"],
                    "wf_edge_mean": wf["edge_mean"],
                    "wf_edge_min": wf["edge_min"],
                })
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
    LOGGER.info(f"v3.2 training: {total_passed}/{len(symbols_and_types)} passed")
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
            if meta.get("label_type") != LABEL_TYPE:
                LOGGER.info("load_model %s: wrong label_type (retrain for v3.2 TP/SL)", symbol)
                return None, None, None
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
    min_edge = _v3_min_edge()
    min_acc = _v3_min_holdout_acc()
    min_p = _v3_min_win_proba()
    min_wf_acc = _v3_min_wf_acc_mean()
    edge = meta.get('edge', 0)
    wf_acc_mean = float(meta.get("wf_acc_mean", meta.get("accuracy", 0)))
    wf_edge_mean = float(meta.get("wf_edge_mean", meta.get("edge", 0)))
    wf_fold_count = int(meta.get("wf_fold_count", 0))
    if edge < min_edge:
        return None
    if meta.get('accuracy', 0) < min_acc:
        return None
    if wf_fold_count > 0 and (wf_acc_mean < min_wf_acc or wf_edge_mean < min_edge):
        return None

    # Confidence = holdout TP/SL WIN rate + strength above min win-probability
    accuracy = meta.get('accuracy', min_acc)

    if up_prob > min_p:
        signal_strength = (up_prob - min_p) * 4.0
        conf = round(min(0.95, max(0.75, accuracy + signal_strength)), 3)
        return ("UP", conf)
    # DOWN signals disabled — 1.5% WR on 274 trades, not viable
    # Ghost is BUY-only system
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
                    "wf_acc_mean": round(m.get("wf_acc_mean",0)*100,1),
                    "wf_edge_mean": round(m.get("wf_edge_mean",0)*100,1),
                    "wf_fold_count": m.get("wf_fold_count",0),
                    "n_samples": m.get("n_samples",0),
                    "engine": m.get("engine_version","v3.0"),
                    "label_type": m.get("label_type", ""),
                    "label_hold_bars": m.get("label_hold_bars", V3_LABEL_HOLD_BARS),
                }
            return {"trained": True, "models": len(symbols), "symbols": symbols}
    except Exception as e:
        return {"trained": False, "reason": str(e)}
