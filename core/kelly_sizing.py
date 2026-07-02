"""
core/kelly_sizing.py — Kelly criterion + correlation-aware position sizing (Pillar 8).

Replaces fixed 1% risk with mathematically optimal Kelly sizing:
  f* = edge / odds

Plus correlation filter: don't fire simultaneous picks on highly correlated
symbols (>0.7), and scale down when multiple positions are open.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

LOGGER = logging.getLogger("ghost.kelly")

# Kelly fraction cap (env-tunable). Full Kelly is aggressive; half-Kelly is standard.
_KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.5"))  # 0.5 = half-Kelly
_MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "5.0"))  # max 5% of portfolio
_CORRELATION_THRESHOLD = float(os.getenv("CORRELATION_THRESHOLD", "0.7"))


def kelly_fraction(
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
) -> float:
    """Compute Kelly fraction: f* = win_rate - (1-win_rate) / odds.

    Standard Kelly criterion for binary outcomes:
      f* = (p × b - q) / b  where  b = avg_win / |avg_loss|

    Simplified: f* = p - q / b = win_rate - (1-win_rate) × |avg_loss| / avg_win

    Returns fraction of capital to risk (capped at _MAX_POSITION_PCT).
    """
    if avg_win_pct <= 0 or avg_loss_pct >= 0:
        return 0.0
    abs_loss = abs(avg_loss_pct)
    if abs_loss == 0:
        return 0.0

    odds = avg_win_pct / abs_loss
    if odds <= 0:
        return 0.0

    # Correct Kelly: f* = p - (1-p) / b
    f_star = win_rate - (1.0 - win_rate) / odds
    f_star = max(0.0, f_star)

    # Apply half-Kelly fraction for safety
    f_star *= _KELLY_FRACTION

    return round(min(f_star, _MAX_POSITION_PCT / 100.0), 4)


def correlation_filter(
    candidate_symbol: str,
    open_symbols: List[str],
    correlation_matrix: Dict[str, Dict[str, float]],
    threshold: float = _CORRELATION_THRESHOLD,
) -> Tuple[bool, Optional[str]]:
    """Check if candidate is too correlated with any open position.

    Returns (allowed, blocking_symbol) — if blocked, blocking_symbol is
    the open symbol with the highest correlation.
    """
    if not open_symbols or candidate_symbol not in correlation_matrix:
        return True, None

    max_corr = 0.0
    blocker = None
    for sym in open_symbols:
        corr = correlation_matrix.get(candidate_symbol, {}).get(sym, 0.0)
        if abs(corr) > max_corr:
            max_corr = abs(corr)
            blocker = sym

    if max_corr > threshold:
        return False, blocker
    return True, None


def portfolio_heat_scale(open_count: int, max_positions: int = 5) -> float:
    """Scale down position sizes as more positions are open.

    Returns multiplier in [0.2, 1.0]. At max_positions, multiplier = 0.2.
    """
    if open_count <= 1:
        return 1.0
    return round(max(0.2, 1.0 - (open_count - 1) / max_positions), 2)


def compute_correlation_matrix(
    symbol_returns: Dict[str, List[float]],
    lookback: int = 60,
) -> Dict[str, Dict[str, float]]:
    """Pairwise correlation matrix from daily returns.

    Args:
        symbol_returns: {sym: [daily_return_1, ...]}
        lookback: number of most recent returns

    Returns:
        {sym1: {sym2: correlation, ...}, ...}
    """
    syms = sorted(symbol_returns.keys())
    matrix: Dict[str, Dict[str, float]] = {}
    for s1 in syms:
        matrix[s1] = {}
        r1 = symbol_returns.get(s1, [])[-lookback:]
        if len(r1) < 10:
            continue
        for s2 in syms:
            if s1 == s2:
                matrix[s1][s2] = 1.0
                continue
            r2 = symbol_returns.get(s2, [])[-lookback:]
            if len(r2) < 10:
                continue
            try:
                c = np.corrcoef(r1, r2)[0, 1]
                matrix[s1][s2] = round(float(c), 4) if not np.isnan(c) else 0.0
            except Exception:
                matrix[s1][s2] = 0.0
    return matrix
