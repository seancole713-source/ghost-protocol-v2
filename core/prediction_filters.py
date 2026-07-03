"""SQL fragments for real stock picks vs crypto-era / sandbox junk rows."""

import os
from config.symbols import OFFICIAL_WATCHLIST

# PR #76: watchlist-membership filter — only OFFICIAL_WATCHLIST symbols appear
# in stats/journal queries. Disable via WATCHLIST_FILTER_ENABLED=0 for tests.
_WATCHLIST_FILTER = ""
if os.getenv("WATCHLIST_FILTER_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on"):
    _WATCHLIST_SQL = ",".join(f"'{s}'" for s in OFFICIAL_WATCHLIST)
    _WATCHLIST_FILTER = f" AND symbol IN ({_WATCHLIST_SQL})"

# v3.2 accounting-era watermark: predictions with id below this predate the
# current outcome/exit rules and are excluded from all win-rate math. Single
# source of truth — stats queries must reference this constant instead of
# re-hardcoding the literal (forensic audit: it appeared in 6 places).
V32_ERA_MIN_ID = 223438

REAL_TRADE_WHERE = (
    "entry_price IS NOT NULL AND entry_price > 0 "
    "AND COALESCE(asset_type, 'stock') = 'stock'"
    + _WATCHLIST_FILTER
)

# Research picks (low-bar, research_pick=true in scores) must never count
# toward credibility metrics: win rate, falsification verdict, objective gates.
NON_RESEARCH_WHERE = (
    "(scores->>'research_pick' IS NULL OR scores->>'research_pick' != 'true')"
)


def non_research_where(alias: str = "") -> str:
    """NON_RESEARCH_WHERE with an optional table alias (e.g. "p") for joined
    queries. Research picks are excluded from every outcome-based metric AND
    from live alerts — they exist to feed the learning loop, not to be acted on."""
    prefix = f"{alias}." if alias else ""
    return (
        f"({prefix}scores->>'research_pick' IS NULL "
        f"OR {prefix}scores->>'research_pick' != 'true')"
    )

CRYPTO_JUNK_WHERE = (
    "COALESCE(asset_type, 'stock') != 'stock' "
    "OR entry_price IS NULL OR entry_price <= 0"
)


def picks_where(symbol: str = "ALL", asset_type: str = None):
    """Build WHERE fragments for pick listings — excludes junk by default."""
    clauses, params = [], []
    sym = str(symbol).strip().upper()
    if sym not in ("ALL", "*", ""):
        clauses.append("symbol = %s")
        params.append(sym)
    if asset_type:
        clauses.append("asset_type = %s")
        params.append(asset_type.strip().lower())
    else:
        clauses.append("COALESCE(asset_type, 'stock') = 'stock'")
    clauses.append("entry_price IS NOT NULL AND entry_price > 0")
    return clauses, params
