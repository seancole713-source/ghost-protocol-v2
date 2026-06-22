"""
core/cross_sectional.py — Cross-sectional rank features (Pillar 2).

For each symbol in the watchlist, computes percentile ranks across peers
on 8 metrics. "Most oversold in the watchlist" is a stronger signal than
"RSI=32" alone.

Computed during each scan cycle after all symbols have their technical
features. Appends 8 features to the feature vector.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

LOGGER = logging.getLogger("ghost.cross_sectional")

# Feature names for the 8 cross-sectional features
CS_FEATURE_NAMES = [
    "cs_rsi_rank",           # 0=most oversold, 1=most overbought
    "cs_volume_rank",        # 0=lowest rel vol, 1=highest
    "cs_momentum_rank",      # 0=worst momentum, 1=best
    "cs_sma_distance_rank",   # 0=furthest below SMA, 1=furthest above
    "cs_atr_rank",           # 0=lowest vol, 1=highest vol
    "cs_adx_rank",           # 0=least trending, 1=most trending
    "cs_short_float_rank",   # 0=least shorted, 1=most shorted
    "cs_sector_corr",        # avg correlation with peer symbols
]


def _percentile_rank(values: List[float], own_value: float) -> float:
    """Percentile rank of own_value within values. 0=lowest, 1=highest."""
    if not values or len(values) < 2:
        return 0.5
    arr = np.array(values, dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 2:
        return 0.5
    return float((arr < own_value).sum()) / max(len(arr) - 1, 1)


def compute_cross_sectional(
    symbol: str,
    own_features: Dict[str, float],
    all_symbol_features: Dict[str, Dict[str, float]],
    sector_correlations: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Compute 8 cross-sectional features for one symbol.

    Args:
        symbol: the symbol to compute ranks for
        own_features: this symbol's technical feature dict
        all_symbol_features: {sym: {feature_name: value}} for all scanned symbols
        sector_correlations: {sym: avg_correlation_with_peers} (optional)

    Returns:
        dict of 8 cross-sectional feature values
    """
    # Collect peer values for each metric
    peer_rsi = []
    peer_vol = []
    peer_mom = []
    peer_sma_dist = []
    peer_atr = []
    peer_adx = []
    peer_short = []

    for sym, feats in all_symbol_features.items():
        if sym == symbol:
            continue
        if feats.get("rsi") is not None:
            peer_rsi.append(float(feats["rsi"]))
        if feats.get("volume_ratio") is not None:
            peer_vol.append(float(feats["volume_ratio"]))
        if feats.get("mom_4h") is not None:
            peer_mom.append(float(feats["mom_4h"]))
        if feats.get("price_in_range") is not None:
            peer_sma_dist.append(float(feats["price_in_range"]))
        if feats.get("atr_pct") is not None:
            peer_atr.append(float(feats["atr_pct"]))
        if feats.get("adx") is not None:
            peer_adx.append(float(feats["adx"]))
        sf = feats.get("short_float_pct")
        if sf is not None:
            peer_short.append(float(sf))

    own_rsi = float(own_features.get("rsi", 50))
    own_vol = float(own_features.get("volume_ratio", 1.0))
    own_mom = float(own_features.get("mom_4h", 0.0))
    own_sma = float(own_features.get("price_in_range", 0.5))
    own_atr = float(own_features.get("atr_pct", 0.02))
    own_adx = float(own_features.get("adx", 25))
    own_short = float(own_features.get("short_float_pct", 0)) if own_features.get("short_float_pct") is not None else None

    result = {
        "cs_rsi_rank": round(_percentile_rank(peer_rsi, own_rsi), 4),
        "cs_volume_rank": round(_percentile_rank(peer_vol, own_vol), 4),
        "cs_momentum_rank": round(_percentile_rank(peer_mom, own_mom), 4),
        "cs_sma_distance_rank": round(_percentile_rank(peer_sma_dist, own_sma), 4),
        "cs_atr_rank": round(_percentile_rank(peer_atr, own_atr), 4),
        "cs_adx_rank": round(_percentile_rank(peer_adx, own_adx), 4),
        "cs_short_float_rank": round(_percentile_rank(peer_short, own_short), 4) if own_short is not None else 0.5,
        "cs_sector_corr": round(sector_correlations.get(symbol, 0.0), 4) if sector_correlations else 0.0,
    }
    return result


def compute_sector_correlations(
    symbol_returns: Dict[str, List[float]],
    lookback: int = 20,
) -> Dict[str, float]:
    """Average pairwise correlation of each symbol with all others.

    Args:
        symbol_returns: {sym: [daily_return_1, daily_return_2, ...]}
        lookback: number of most recent returns to use

    Returns:
        {sym: avg_correlation_with_peers}
    """
    syms = list(symbol_returns.keys())
    if len(syms) < 2:
        return {s: 0.0 for s in syms}

    corr_matrix = {}
    for s1 in syms:
        r1 = symbol_returns.get(s1, [])[-lookback:]
        if len(r1) < 5:
            corr_matrix[s1] = 0.0
            continue
        corrs = []
        for s2 in syms:
            if s1 == s2:
                continue
            r2 = symbol_returns.get(s2, [])[-lookback:]
            if len(r2) < 5:
                continue
            try:
                c = np.corrcoef(r1, r2)[0, 1]
                if not np.isnan(c):
                    corrs.append(c)
            except Exception:
                pass
        corr_matrix[s1] = round(float(np.mean(corrs)), 4) if corrs else 0.0
    return corr_matrix
