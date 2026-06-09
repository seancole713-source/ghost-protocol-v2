"""SQL fragments for real stock picks vs crypto-era / sandbox junk rows."""

REAL_TRADE_WHERE = (
    "entry_price IS NOT NULL AND entry_price > 0 "
    "AND COALESCE(asset_type, 'stock') = 'stock'"
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
