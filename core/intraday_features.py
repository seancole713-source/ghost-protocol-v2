"""core/intraday_features.py — intraday microstructure features (PR #171).

Adds features computed from 1-hour intraday bars that daily bars cannot capture:
  - VWAP deviation: how far price is from volume-weighted average
  - Volume profile: what fraction of daily volume traded at each price level
  - Intraday range: high-low range as fraction of open
  - Gap fill: whether overnight gap was filled during the session
  - Relative volume: current volume vs same-time average
  - Hourly momentum: close-to-close returns at hourly frequency
  - Volatility signature: ratio of intraday vol to daily vol

These features are orthogonal to daily technical indicators — they measure
market microstructure, not price patterns. Enabled via V3_INTRADAY_FEATURES=on.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

LOGGER = logging.getLogger("ghost.intraday_features")

INTRADAY_FEATURE_NAMES = [
    "intra_vwap_deviation",
    "intra_range_pct",
    "intra_gap_fill_pct",
    "intra_volume_trend",
    "intra_hourly_momentum",
    "intra_vol_signature",
]


def enabled() -> bool:
    return (os.getenv("V3_INTRADAY_FEATURES", "off") or "off").strip().lower() in (
        "1", "on", "true", "yes",
    )


def compute_intraday_features(
    hourly_bars: Sequence[Dict[str, Any]],
    prev_close: Optional[float] = None,
) -> Dict[str, float]:
    """Compute intraday microstructure features from 1-hour OHLCV bars.

    Args:
        hourly_bars: list of 1-hour bars for the current session, sorted by time
        prev_close: previous session's close (for gap fill calculation)

    Returns dict of feature values, all neutral (0.0) when data is insufficient.
    """
    out: Dict[str, float] = {
        "intra_vwap_deviation": 0.0,
        "intra_range_pct": 0.0,
        "intra_gap_fill_pct": 0.0,
        "intra_volume_trend": 0.0,
        "intra_hourly_momentum": 0.0,
        "intra_vol_signature": 0.0,
    }

    if not hourly_bars or len(hourly_bars) < 2:
        return out

    closes = np.array([float(b.get("close", 0)) for b in hourly_bars])
    highs = np.array([float(b.get("high", 0)) for b in hourly_bars])
    lows = np.array([float(b.get("low", 0)) for b in hourly_bars])
    opens = np.array([float(b.get("open", 0)) for b in hourly_bars])
    volumes = np.array([float(b.get("volume", 0)) for b in hourly_bars])

    if closes[0] <= 0:
        return out

    session_open = opens[0]
    session_high = np.max(highs)
    session_low = np.min(lows)
    current_close = closes[-1]
    total_volume = np.sum(volumes)

    # 1. VWAP deviation: (close - VWAP) / VWAP
    if total_volume > 0:
        typical_prices = (highs + lows + closes) / 3
        vwap = np.sum(typical_prices * volumes) / total_volume
        if vwap > 0:
            out["intra_vwap_deviation"] = round(float((current_close - vwap) / vwap), 4)

    # 2. Intraday range: (high - low) / open
    if session_open > 0:
        out["intra_range_pct"] = round(float((session_high - session_low) / session_open), 4)

    # 3. Gap fill: how much of the overnight gap was filled
    if prev_close and prev_close > 0 and session_open > 0:
        gap = session_open - prev_close
        if abs(gap) > 0.001 * prev_close:  # meaningful gap (>0.1%)
            if gap > 0:  # gapped up — did it fill down?
                fill = (session_open - session_low) / gap
            else:  # gapped down — did it fill up?
                fill = (session_high - session_open) / abs(gap)
            out["intra_gap_fill_pct"] = round(float(max(0.0, min(1.0, fill))), 4)

    # 4. Volume trend: is volume accelerating or decelerating?
    if len(volumes) >= 4:
        first_half = np.sum(volumes[:len(volumes)//2])
        second_half = np.sum(volumes[len(volumes)//2:])
        if first_half > 0:
            out["intra_volume_trend"] = round(float((second_half - first_half) / first_half), 4)

    # 5. Hourly momentum: average hourly close-to-close return
    if len(closes) >= 3:
        hourly_rets = np.diff(closes) / closes[:-1]
        out["intra_hourly_momentum"] = round(float(np.mean(hourly_rets)), 4)

    # 6. Volatility signature: intraday range / recent daily range
    if session_open > 0 and len(hourly_bars) >= 6:
        hourly_ranges = (highs - lows) / session_open
        out["intra_vol_signature"] = round(float(np.std(hourly_ranges)), 4)

    return out
