"""
V3 validated strategies and symbol lists.
All symbol-related configuration in one place.

Based on 52,433 trade backtest analysis.
Only strategies with p < 0.05 are included.
"""
from dataclasses import dataclass
from typing import Optional, Dict, FrozenSet, Tuple

# Direction override constant — used in ghost_inverse strategies
# that flip Ghost's prediction (e.g., PANW/NET/FTNT)
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
    asset_type: Optional[str] = None  # 'stock' | None (crypto is default)


# =============================================================================
# V3 VALIDATED STRATEGIES
# WOLF-ONLY MODE (Phase 0 pivot — May 21, 2026)
# Ghost Protocol now focuses exclusively on Wolfspeed (WOLF) stock.
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
    # ── ARCHIVED — ETH, XRP, LINK, CHZ, FTNT, PANW, NET, DDOG removed May 21, 2026 ──
    # Ghost is now a single-stock WOLF intelligence system.
}


# =============================================================================
# REMOVED SYMBOLS
# These symbols were analyzed but did NOT show statistical significance
# =============================================================================
# NOTE: Edge whitelist symbols removed Feb 11, 2026
# TURBO, RNDR, IQ, ILV, CHZ have proven paper trade performance
V3_REMOVED_SYMBOLS: Dict[str, str] = {
    'SOL': 'Inverse 50.2% over 4962 trades - not significant',
    'BTC': 'Inverse 52% over large sample - not significant',
    'AVAX': 'Inverse 50.2% over 4988 trades - not significant',
    'ZEC': 'Inverse 50.1% over 3006 trades - not significant',
    'AAVE': 'Inverse 49.8% over 2844 trades - not significant',
    'BNB': 'Inverse 49.5% over 4200 trades - not significant',
    'ADA': 'Inverse 50.3% over 3800 trades - not significant',
    'LTC': 'Inverse 49.7% over 3500 trades - not significant',
}


# =============================================================================
# BLACKLISTED SYMBOLS
# Never trade these regardless of predictions
# =============================================================================
# NOTE: YFI and HBAR removed Feb 11, 2026 — they are edge whitelist symbols
V3_BLACKLIST: FrozenSet[str] = frozenset([
    'TGTX', 'SOUN', 'ABCL', '1INCH', 'SAND', 'MANA', 'DOT', 'SHIB', 'FIL',
    'VET', 'ALGO', 'ARB', 'NEAR', 'SUSHI', 'LDO', 'ETC', 'IMX',
    'APT', 'SUI', 'RLC',
])


# =============================================================================
# WHITELIST STOCKS
# WOLF-ONLY — all other stocks archived May 21, 2026
# =============================================================================
V3_WHITELIST_STOCKS: FrozenSet[str] = frozenset(['WOLF'])


# =============================================================================
# CRYPTO SYMBOLS
# All known crypto symbols for asset type detection
# =============================================================================
CRYPTO_SYMBOLS: FrozenSet[str] = frozenset([
    'BTC', 'ETH', 'SOL', 'XRP', 'BNB', 'ADA', 'AVAX', 'LINK', 'LTC', 'DOT',
    'MATIC', 'DOGE', 'SHIB', 'ATOM', 'UNI', 'AAVE', 'MKR', 'CRV', 'RNDR',
    'TURBO', 'CHZ', 'ILV', 'ZEC', 'INJ', 'SUI', 'APT', 'ARB', 'OP', 'TIA',
    'PEPE', 'WIF', 'BONK', 'FLOKI', 'MEME', 'ORDI', 'SATS', 'SEI', 'FET',
    'RENDER', 'GRT', 'SNX', 'COMP', '1INCH', 'SAND', 'MANA', 'AXS', 'ENJ',
    'VET', 'ALGO', 'NEAR', 'SUSHI', 'YFI', 'LDO', 'IMX', 'HBAR',
    'RLC', 'FIL', 'ICP', 'EGLD', 'XLM', 'XMR', 'IOTA', 'NEO', 'WAVES',
    # Edge whitelist crypto (added Feb 11, 2026)
    'JUP', 'BAND', 'IQ', 'IOTX', 'GIGA', 'ALICE', 'BRETT',
    # REMOVED Mar 22, 2026 (Phase 0.3 - Toxic Symbol Purge):
    # 'BCH' — 0% accuracy (0W/3L)
    # 'ETC' — 0% accuracy (0W/3L)
])


# =============================================================================
# DEFAULT EDGE SYMBOLS
# Single source of truth for the EDGE_SYMBOLS env var fallback.
# All code should import this instead of hardcoding the CSV string.
#
# Feb 26, 2026: Expanded from 13 → 50 symbols to increase prediction coverage.
# Sources: V3_VALIDATED_STRATEGIES, V3_WHITELIST_STOCKS, top crypto by volume,
# top stocks by liquidity. Excludes V3_BLACKLIST entries.
#
# Mar 22, 2026: REMOVED TOXIC SYMBOLS (Phase 0.3)
# - PANW: 5% accuracy (2W/35L) - 10 loss streak destroyer
# - DDOG: 17% accuracy (13W/63L) - drags down metrics
# - NET: 20% accuracy (15W/60L) - consistent loser
# - XPO: 23% accuracy (12W/40L) - below 25% kill threshold
# These 4 symbols contributed to 10-loss streak and 41% accuracy.
# Removing from active trading to improve system metrics.
# =============================================================================
# WOLF-ONLY MODE — pivoted May 21, 2026
# Ghost Protocol is now a single-stock intelligence system for Wolfspeed (WOLF).
# All crypto and non-WOLF stocks have been archived.
DEFAULT_EDGE_SYMBOLS = "WOLF"
# REMOVED Feb 25, 2026: RNDR — 11% accuracy (1/9), in HARDCODED_EXCLUSIONS.
# REMOVED Feb 27, 2026: ADA(20%), AVAX(30%), BNB(30%), DOGE(30%), YFI(11%)
#   — all in HARDCODED_EXCLUSIONS with <40% accuracy. Wasted compute.
# REMOVED Feb 27, 2026: BONK, PEPE, WIF — meme coins in HARDCODED_EXCLUSIONS.

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

def is_crypto(symbol: str) -> bool:
    """Check if a symbol is a cryptocurrency."""
    return symbol.upper() in CRYPTO_SYMBOLS


def is_v3_validated(symbol: str) -> bool:
    """Check if a symbol has a V3 validated strategy."""
    return symbol.upper() in V3_VALIDATED_STRATEGIES


def is_blacklisted(symbol: str) -> bool:
    """Check if a symbol is blacklisted."""
    return symbol.upper() in V3_BLACKLIST


def is_removed(symbol: str) -> bool:
    """Check if a symbol was analyzed but removed from V3."""
    return symbol.upper() in V3_REMOVED_SYMBOLS


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
