"""core/data_quorum.py — multi-provider price quorum with disagreement detection.

The price chain (Alpaca → yfinance → Polygon → IEX) is a *fallback* chain: it
returns the first non-None value and never compares providers. A single bad
feed (phantom quote, stale cache) can therefore win silently.

This module adds a *quorum* layer on top: probe multiple independent providers
concurrently, compare their values, and report:
  - agreement (all within tolerance),
  - disagreement (values diverge beyond tolerance — flag, don't trust blindly),
  - freshness (per-provider observation timestamp vs now).

It is read-only and advisory: it never changes the price the rest of the app
uses; it surfaces a `quorum` verdict so the detection tier can down-weight a
symbol whose price is contested.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("ghost.data_quorum")

# Max % divergence between two providers before they "disagree".
DIVERGENCE_PCT = float(__import__("os").getenv("QUORUM_DIVERGENCE_PCT", "5.0"))
# Max age (seconds) of a provider observation before it is "stale".
MAX_FRESHNESS_S = int(__import__("os").getenv("QUORUM_MAX_FRESHNESS_S", "900"))


def _provider_quotes(symbol: str) -> List[Tuple[str, Optional[float], Optional[int]]]:
    """Probe independent providers. Returns [(name, price, asof_ts), ...].

    Best-effort: each provider is isolated so one failure never blocks the rest.
    """
    from core.prices import _alpaca_trade_quote, _yfinance, _polygon_spot, _iex_spot

    quotes: List[Tuple[str, Optional[float], Optional[int]]] = []
    # Alpaca (with observation timestamp).
    try:
        px, ts = _alpaca_trade_quote(symbol)
        quotes.append(("alpaca", px, ts))
    except Exception as exc:
        LOGGER.debug("quorum alpaca %s: %s", symbol, str(exc)[:80])
        quotes.append(("alpaca", None, None))
    # yfinance (no timestamp — mark as now).
    try:
        px = _yfinance(symbol)
        quotes.append(("yfinance", px, int(time.time()) if px else None))
    except Exception as exc:
        LOGGER.debug("quorum yfinance %s: %s", symbol, str(exc)[:80])
        quotes.append(("yfinance", None, None))
    # Polygon (prev close only — mark as stale by design, still a cross-check).
    try:
        px = _polygon_spot(symbol)
        quotes.append(("polygon", px, None))
    except Exception as exc:
        LOGGER.debug("quorum polygon %s: %s", symbol, str(exc)[:80])
        quotes.append(("polygon", None, None))
    # IEX (Alpaca's alternate feed).
    try:
        px = _iex_spot(symbol)
        quotes.append(("iex", px, int(time.time()) if px else None))
    except Exception as exc:
        LOGGER.debug("quorum iex %s: %s", symbol, str(exc)[:80])
        quotes.append(("iex", None, None))
    return quotes


def _fresh(ts: Optional[int]) -> bool:
    if ts is None:
        return False
    return (time.time() - int(ts)) <= MAX_FRESHNESS_S


def evaluate_quorum(symbol: str) -> Dict[str, Any]:
    """Probe providers and return a quorum verdict for a symbol.

    Returns:
      {
        "symbol", "providers": [{name, price, asof_ts, fresh}],
        "agreeing": [names], "disagreeing": [names],
        "verdict": "agree" | "disagree" | "insufficient",
        "median_price", "spread_pct",
      }
    """
    sym = (symbol or "").upper()
    quotes = _provider_quotes(sym)
    providers = []
    prices: List[float] = []
    for name, px, ts in quotes:
        providers.append({
            "name": name,
            "price": round(float(px), 4) if px is not None else None,
            "asof_ts": ts,
            "fresh": _fresh(ts) if px is not None else False,
        })
        if px is not None and float(px) > 0:
            prices.append(float(px))

    if len(prices) < 2:
        return {
            "symbol": sym,
            "providers": providers,
            "agreeing": [],
            "disagreeing": [],
            "verdict": "insufficient",
            "median_price": round(prices[0], 4) if prices else None,
            "spread_pct": 0.0,
        }

    prices.sort()
    median = prices[len(prices) // 2]
    lo, hi = prices[0], prices[-1]
    spread = (hi - lo) / median * 100.0 if median > 0 else 0.0

    agreeing: List[str] = []
    disagreeing: List[str] = []
    for p in providers:
        if p["price"] is None:
            continue
        dev = abs(p["price"] - median) / median * 100.0 if median > 0 else 0.0
        if dev <= DIVERGENCE_PCT:
            agreeing.append(p["name"])
        else:
            disagreeing.append(p["name"])

    verdict = "disagree" if disagreeing else "agree"
    return {
        "symbol": sym,
        "providers": providers,
        "agreeing": agreeing,
        "disagreeing": disagreeing,
        "verdict": verdict,
        "median_price": round(median, 4),
        "spread_pct": round(spread, 2),
    }
