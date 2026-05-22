"""
V3 validated strategies and symbol lists.

WOLF-ONLY MODE (Phase 0 pivot — May 21, 2026)
Ghost Protocol is now a single-stock intelligence system for Wolfspeed (WOLF).
All crypto and non-WOLF stocks have been archived.
"""
from dataclasses import dataclass
from typing import Optional, Dict, FrozenSet, Tuple

# Direction override constant — retained for legacy compatibility; no active strategy
# currently uses it.
DIRECTION_FLIP = 'flip'


@dataclass(frozen=True)
class ValidatedStrategy:
    """Configuration for a V3 validated trading strategy."""
    symbol: str
    strategy: str  # 'ghost_inverse' | 'mean_reversion'
    direction_override: Optional[str]  # 'UP' | 'DOWN' | DIRECTION_FLIP | None
    hold_hours: int
    backtest_win_rate: float
    backtest_trades: int
    p_value: float
    confidence_interval: Optional[Tuple[float, float]] = None
    asset_type: Optional[str] = None  # 'stock' for WOLF


# =============================================================================
# V3 VALIDATED STRATEGIES — WOLF only
# =============================================================================
V3_VALIDATED_STRATEGIES: Dict[str, ValidatedStrategy] = {
    'WOLF': ValidatedStrategy(
        symbol='WOLF',
        strategy='mean_reversion',
        direction_override=None,
        hold_hours=24,
        backtest_win_rate=0.55,   # placeholder — retrain in Phase 3
        backtest_trades=0,        # no backtest yet; Phase 3 will populate
        p_value=0.05,             # placeholder — walk-forward validation pending
        confidence_interval=(0.50, 0.60),
        asset_type='stock',
    ),
}


# =============================================================================
# WHITELIST STOCKS — WOLF-only
# =============================================================================
V3_WHITELIST_STOCKS: FrozenSet[str] = frozenset(['WOLF'])


# =============================================================================
# DEFAULT EDGE SYMBOLS
# Single source of truth for the EDGE_SYMBOLS env var fallback.
# =============================================================================
DEFAULT_EDGE_SYMBOLS = "WOLF"

# Cached resolved edge set (computed once, used everywhere)
_RESOLVED_EDGE_SET: Optional[FrozenSet[str]] = None


def get_edge_set() -> FrozenSet[str]:
    """
    Return the resolved edge symbol set.

    WOLF-ONLY MODE: Always returns {"WOLF"}.
    The env var EDGE_SYMBOLS is intentionally ignored to prevent stale
    Railway env vars from re-introducing archived symbols.
    """
    global _RESOLVED_EDGE_SET
    if _RESOLVED_EDGE_SET is not None:
        return _RESOLVED_EDGE_SET
    _RESOLVED_EDGE_SET = frozenset(["WOLF"])
    return _RESOLVED_EDGE_SET


def get_edge_csv() -> str:
    """Return the resolved edge symbols as a comma-separated string."""
    return ",".join(sorted(get_edge_set()))


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def is_v3_validated(symbol: str) -> bool:
    """Check if a symbol has a V3 validated strategy."""
    return symbol.upper() in V3_VALIDATED_STRATEGIES


def is_blacklisted(symbol: str) -> bool:
    """Always False — WOLF-only mode has no blacklist."""
    return False


def get_strategy(symbol: str) -> Optional[ValidatedStrategy]:
    """Get the validated strategy for a symbol, if any."""
    return V3_VALIDATED_STRATEGIES.get(symbol.upper())


def v3_strategies_as_dicts() -> Dict[str, dict]:
    """Convert V3_VALIDATED_STRATEGIES to legacy dict format.

    Legacy code expects keys: strategy, direction_override, hold_hours,
    win_rate, sample_size, p_value, confidence_interval, asset_type.
    """
    result = {}
    for sym, vs in V3_VALIDATED_STRATEGIES.items():
        d = {
            'strategy': vs.strategy,
            'direction_override': vs.direction_override,
            'hold_hours': vs.hold_hours,
            'win_rate': vs.backtest_win_rate,
            'sample_size': vs.backtest_trades,
            'p_value': vs.p_value,
        }
        if vs.confidence_interval is not None:
            d['confidence_interval'] = vs.confidence_interval
        if vs.asset_type is not None:
            d['asset_type'] = vs.asset_type
        result[sym] = d
    return result
