"""core/engine_indicators.py — pure technical-indicator math (split from signal_engine PR #130).

No I/O, no env reads, no state: lists/numpy in, floats out. core.signal_engine
re-exports these for backward compatibility.
"""
import numpy as np

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
