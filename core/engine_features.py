"""core/engine_features.py — feature-vector construction (split from signal_engine PR #130).

_calculate_features turns an OHLCV dataframe into the model's technical feature
columns; FEATURE_COLS is the canonical column list. Sector relative-strength
alignment helpers live here too. core.signal_engine re-exports everything.
"""
import numpy as np

from core.engine_config import _v3_adx_trending_threshold, _v3_pool_training_enabled
from core.engine_indicators import (
    _adx, _atr, _bollinger, _ema, _macd, _obv_slope,
    _price_momentum, _rsi, _stochastic, _volume_ratio,
)

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
    except Exception:
        hod, dow = 12, 0

    cur = float(closes[-1])
    n = len(closes)
    ema20 = _ema(closes, 20)
    # Young-ticker EMA fallback. With too little history for the longer EMAs,
    # fall back to the longest EMA that IS valid (NOT `cur`). The old `else cur`
    # made above_emaX = (cur>cur) = 0 and ema_trend_bullish = 0 *permanently*,
    # which kept the BUY-only regime gate in WATCHING for new tickers like
    # post-Ch.11 WOLF (~168 trading days < 200). Applied inside _calculate_features
    # so train and serve stay consistent (no skew).
    ema50 = _ema(closes, 50) if n >= 50 else ema20
    ema200 = _ema(closes, 200) if n >= 200 else ema50
    # Long-trend flags degrade gracefully:
    #   n >= 200 -> full 20>50>200 stack
    #   50 <= n < 200 -> valid 20>50 stack (ema200 is a fallback, so the strict
    #                    ema50>ema200 self-comparison would wrongly read 0)
    #   20 <= n < 50 -> price-vs-ema20 (only ema20 is a true EMA)
    #   n < 20 -> neutral (1): not enough history to judge; don't block BUYs
    if n < 20:
        above_ema200_flag = 1
        ema_trend_bullish = 1
    else:
        above_ema200_flag = 1 if cur > ema200 else 0
        if n >= 200:
            ema_trend_bullish = 1 if (ema20 > ema50 and ema50 > ema200) else 0
        elif n >= 50:
            ema_trend_bullish = 1 if (cur > ema20 and ema20 > ema50) else 0
        else:
            ema_trend_bullish = 1 if cur > ema20 else 0
    adx = _adx(highs, lows, closes)
    atr = _atr(highs, lows, closes)
    obv_slope = _obv_slope(closes, volumes)
    stoch_k, stoch_d = _stochastic(highs, lows, closes)

    # macd_hist is in raw price units, so its scale tracks the share price. For
    # cross-ticker pooling (W1) that makes a $5 stock incomparable to a $200
    # one, so express it as a fraction of price. Read at runtime in both the
    # train and live paths, so the two stay consistent regardless of the flag.
    macd_hist_feat = (macd_hist / cur) if (_v3_pool_training_enabled() and cur > 0) else macd_hist

    return {
        'rsi': rsi,
        'rsi_oversold': 1 if rsi < 35 else 0,
        'rsi_overbought': 1 if rsi > 65 else 0,
        'macd_hist': macd_hist_feat,
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
        'above_ema200': above_ema200_flag,
        'ema_trend_bullish': ema_trend_bullish,
        'ema20_vs_ema50': float((ema20 - ema50) / ema50) if ema50 > 0 else 0.0,
        'adx': adx,
        'adx_trending': 1 if adx > _v3_adx_trending_threshold() else 0,
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
    # Phase 0 (PR #115): macro + cross-sectional features were computed every
    # cycle but never added to the training feature list — the model was blind
    # to VIX, yield curve, sector ranks, and peer-relative strength.
    'macro_vix_level','macro_yield_spread','macro_fed_rate',
    'macro_dxy_change','macro_spy_20d_return','macro_spy_vs_sma50',
    'macro_smh_vs_spy','macro_vix_regime',
    'cs_rsi_rank','cs_volume_rank','cs_momentum_rank','cs_sma_distance_rank',
    'cs_atr_rank','cs_adx_rank','cs_short_float_rank','cs_sector_corr',
]


def _date_key(ts) -> str:
    """Date portion (YYYY-MM-DD) of an ISO/Alpaca timestamp, used for alignment."""
    return str(ts or "")[:10]


def _align_sector_closes(target_rows, sector_rows):
    """Sector closes aligned 1:1 to target_rows by date (W3).

    For each target bar, take the sector close on the same date; if the sector
    has no bar that day (holiday/feed mismatch), forward-fill the most recent
    *prior* sector close. Returns a list parallel to target_rows (None before
    any sector data exists). Only same-or-earlier sector bars are ever used, so
    there is no look-ahead. Assumes both series are in ascending date order
    (as the feed returns them). Pure / unit-testable.
    """
    by_date = {}
    for r in sector_rows or []:
        by_date[_date_key(r.get("ts"))] = float(r.get("close", 0.0))
    sector_sorted = sorted(by_date.items())
    out, last, si = [], None, 0
    for tr in target_rows:
        d = _date_key(tr.get("ts"))
        while si < len(sector_sorted) and sector_sorted[si][0] <= d:
            last = sector_sorted[si][1]
            si += 1
        out.append(by_date.get(d, last))
    return out


def _sector_rel_at(target_rows, aligned_sector, i, lookback):
    """Point-in-time sector relative strength at bar i (W3).

    target trailing return over `lookback` bars minus the sector's, using only
    bars at or before i. Returns 0.0 when there isn't enough history or the
    aligned sector close is missing — a neutral value, never a guess from the
    future.
    """
    if i < lookback:
        return 0.0
    t_past = float(target_rows[i - lookback]["close"])
    t_cur = float(target_rows[i]["close"])
    s_past = aligned_sector[i - lookback]
    s_cur = aligned_sector[i]
    if s_past is None or s_cur is None or t_past <= 0 or s_past <= 0:
        return 0.0
    return float((t_cur - t_past) / t_past - (s_cur - s_past) / s_past)
