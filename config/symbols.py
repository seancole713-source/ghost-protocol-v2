"""
V3 validated strategies and symbol lists.

Ghost watches the official stock watchlist (OFFICIAL_WATCHLIST / STOCK_SYMBOLS).
WOLF remains the default anchor when the list resolves empty.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Dict, FrozenSet, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    pass

_WATCHLIST_SKIP = frozenset({"GHOST", "TEST"})

# Official live watchlist — Ghost scans and forecasts these symbols only.
# Sourced from investor watchlist (mobile app screenshots, Jun 2026).
OFFICIAL_WATCHLIST: Tuple[str, ...] = (
    "ABCL", "AI", "AMC", "ARCT", "ARDT", "BB", "BILL", "BMBL", "CLNE", "CVNA",
    "DJT", "DUOL", "FLNC", "GME", "HIMS", "HOOD", "IQ", "ITRI", "LCID", "LU",
    "LULU", "NOK", "ODD", "OPK", "OPTU", "PFE", "PLTK", "PLUG", "RIG",
    "RIOT", "SABR", "SAP", "SNAP", "SOUN", "SPCE", "STUB", "TAL", "TGTX", "TLRY",
    "TME", "WOLF", "XPO", "YMM",
)
OFFICIAL_WATCHLIST_CSV = ",".join(OFFICIAL_WATCHLIST)

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
# V3 VALIDATED STRATEGIES — per-symbol metadata (WOLF seeded first; peers inherit defaults at train)
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
# WHITELIST STOCKS — official watchlist (all symbols Ghost may fire picks on)
# =============================================================================
V3_WHITELIST_STOCKS: FrozenSet[str] = frozenset(OFFICIAL_WATCHLIST)


# =============================================================================
# DEFAULT EDGE SYMBOLS
# Single source of truth for the EDGE_SYMBOLS env var fallback.
# =============================================================================
DEFAULT_EDGE_SYMBOLS = OFFICIAL_WATCHLIST_CSV

# Cached resolved edge set (computed once, used everywhere)
_RESOLVED_EDGE_SET: Optional[FrozenSet[str]] = None


def _env_watchlist_override_allowed() -> bool:
    return os.getenv("GHOST_ALLOW_ENV_WATCHLIST", "").strip().lower() in ("1", "true", "yes")


def apply_official_watchlist_env() -> None:
    """Pin STOCK_SYMBOLS to the code-defined watchlist (prod default)."""
    if _env_watchlist_override_allowed():
        return
    os.environ["STOCK_SYMBOLS"] = OFFICIAL_WATCHLIST_CSV
    global _RESOLVED_EDGE_SET
    _RESOLVED_EDGE_SET = None


def _env_stock_symbols() -> List[str]:
    raw = os.getenv("STOCK_SYMBOLS", os.getenv("EDGE_SYMBOLS", "")).strip()
    if raw:
        syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
        if syms:
            return syms
    return list(OFFICIAL_WATCHLIST)


def watchlist_symbol_pairs(include_portfolio: bool = True) -> List[Tuple[str, str]]:
    """Configured watchlist as (symbol, asset_type) pairs — official list only."""
    del include_portfolio  # portfolio rows do not expand scan/predict universe
    stocks: List[Tuple[str, str]] = [(sym, "stock") for sym in _env_stock_symbols()]
    seen = set()
    deduped: List[Tuple[str, str]] = []
    for sym, atype in stocks:
        sym = (sym or "").strip().upper()
        atype = (atype or "stock").strip().lower()
        if not sym or sym in _WATCHLIST_SKIP or sym.startswith("ZZ"):
            continue
        entry = (sym, atype)
        if entry not in seen:
            seen.add(entry)
            deduped.append(entry)
    return deduped or [("WOLF", "stock")]


def watchlist_symbols(include_portfolio: bool = True) -> FrozenSet[str]:
    return frozenset(sym for sym, _atype in watchlist_symbol_pairs(include_portfolio))


def get_edge_set() -> FrozenSet[str]:
    """Return the resolved edge symbol set from STOCK_SYMBOLS (fallback WOLF)."""
    global _RESOLVED_EDGE_SET
    if _RESOLVED_EDGE_SET is not None:
        return _RESOLVED_EDGE_SET
    _RESOLVED_EDGE_SET = watchlist_symbols(include_portfolio=False)
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
    """True when symbol is outside the official watchlist."""
    return symbol.upper() not in V3_WHITELIST_STOCKS


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


apply_official_watchlist_env()
