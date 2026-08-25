"""Truthful, advisory multi-provider live-price corroboration.

Only finite, provider-timestamped, fresh live observations vote. Reference
closes remain visible but never vote, and observations from the same vendor
family count as one independent source. Results are advisory-only.
"""
from __future__ import annotations

import logging
import math
import os
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Dict, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger("ghost.data_quorum")
DIVERGENCE_PCT = float(os.getenv("QUORUM_DIVERGENCE_PCT", "5.0"))
MAX_FRESHNESS_S = int(os.getenv("QUORUM_MAX_FRESHNESS_S", "900"))
MAX_FUTURE_SKEW_S = int(os.getenv("QUORUM_MAX_FUTURE_SKEW_S", "30"))
TOTAL_DEADLINE_S = float(os.getenv("QUORUM_TOTAL_DEADLINE_S", "6.0"))
CACHE_TTL_S = int(os.getenv("QUORUM_CACHE_TTL_S", "180"))
NEGATIVE_CACHE_TTL_S = int(os.getenv("QUORUM_NEGATIVE_CACHE_TTL_S", "45"))
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")

# Legacy three-tuples remain accepted for narrow tests and callers. Native
# observations add role and independent-family provenance.
Quote = Tuple[str, Optional[float], Optional[int], str, str]
_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
_KEY_LOCKS: Dict[str, threading.Lock] = {}


def _key_lock(symbol: str) -> threading.Lock:
    with _CACHE_LOCK:
        return _KEY_LOCKS.setdefault(symbol, threading.Lock())


def clear_quorum_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _provider_quotes(symbol: str) -> List[Quote]:
    """Probe providers concurrently under one total wall-clock budget."""
    from core.prices import (
        _alpaca_trade_quote,
        _iex_trade_quote,
        _polygon_spot,
        _yfinance_quote,
    )

    probes = {
        "alpaca": (_alpaca_trade_quote, "live", "alpaca"),
        "yfinance": (_yfinance_quote, "live", "yahoo"),
        "iex": (_iex_trade_quote, "live", "alpaca"),
        "polygon_prev": (lambda sym: (_polygon_spot(sym), None), "reference", "polygon"),
    }
    executor = ThreadPoolExecutor(max_workers=len(probes), thread_name_prefix="quorum")
    future_meta = {
        executor.submit(fn, symbol): (name, role, family)
        for name, (fn, role, family) in probes.items()
    }
    done, pending = wait(future_meta, timeout=max(0.05, TOTAL_DEADLINE_S))
    out: List[Quote] = []
    for future, (name, role, family) in future_meta.items():
        if future not in done:
            future.cancel()
            out.append((name, None, None, role, family))
            continue
        try:
            price, observed_at = future.result()
            out.append((name, price, observed_at, role, family))
        except Exception as exc:
            LOGGER.debug("quorum %s %s: %s", name, symbol, str(exc)[:100])
            out.append((name, None, None, role, family))
    # Never wait beyond the declared budget for a provider thread to finish.
    executor.shutdown(wait=False, cancel_futures=True)
    return out


def _normalize_quote(raw: Sequence[Any]) -> Quote:
    if len(raw) >= 5:
        name, price, observed_at, role, family = raw[:5]
    else:
        name, price, observed_at = raw[:3]
        role = "reference" if str(name).startswith("polygon") else "live"
        family = "alpaca" if str(name) in {"alpaca", "iex"} else str(name)
    return str(name), price, observed_at, str(role), str(family)


def _fresh(ts: Optional[int], *, now: Optional[float] = None) -> bool:
    if ts is None:
        return False
    current = time.time() if now is None else float(now)
    try:
        age = current - int(ts)
    except (TypeError, ValueError, OverflowError):
        return False
    return -MAX_FUTURE_SKEW_S <= age <= MAX_FRESHNESS_S


def _valid_price(value: Any) -> Optional[float]:
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _uncached_quorum(symbol: str) -> Dict[str, Any]:
    observed_now = time.time()
    providers: List[Dict[str, Any]] = []
    family_prices: Dict[str, List[float]] = {}
    for raw in _provider_quotes(symbol):
        name, value, observed_at, role, family = _normalize_quote(raw)
        price = _valid_price(value)
        fresh = bool(price is not None and _fresh(observed_at, now=observed_now))
        eligible = bool(price is not None and role == "live" and fresh)
        exclusion_reason = None
        if price is None:
            exclusion_reason = "missing_or_invalid_price"
        elif role != "live":
            exclusion_reason = "reference_only"
        elif observed_at is None:
            exclusion_reason = "missing_provider_timestamp"
        elif not fresh:
            exclusion_reason = "stale_or_future"
        providers.append({
            "name": name,
            "family": family,
            "role": role,
            "price": round(price, 4) if price is not None else None,
            "asof_ts": observed_at,
            "fresh": fresh,
            "eligible": eligible,
            "exclusion_reason": exclusion_reason,
        })
        if eligible:
            family_prices.setdefault(family, []).append(price)

    # Correlated observations are consolidated before consensus.
    family_votes = {
        family: float(statistics.median(values))
        for family, values in family_prices.items()
    }
    votes = list(family_votes.values())
    median_price = float(statistics.median(votes)) if votes else None
    spread_pct: Optional[float] = None
    agreeing_families: List[str] = []
    disagreeing_families: List[str] = []
    if median_price is not None and len(votes) >= 2:
        spread_pct = (max(votes) - min(votes)) / median_price * 100.0
        for family, price in family_votes.items():
            deviation = abs(price - median_price) / median_price * 100.0
            (agreeing_families if deviation <= DIVERGENCE_PCT else disagreeing_families).append(family)
        verdict = "disagree" if disagreeing_families else "agree"
    else:
        verdict = "insufficient"

    agreeing = [
        p["name"] for p in providers
        if p["eligible"] and p["family"] in agreeing_families
    ]
    disagreeing = [
        p["name"] for p in providers
        if p["eligible"] and p["family"] in disagreeing_families
    ]
    return {
        "ok": True,
        "symbol": symbol,
        "advisory_only": True,
        "providers": providers,
        "independent_groups": len(family_votes),
        "family_votes": {k: round(v, 4) for k, v in family_votes.items()},
        "agreeing": agreeing,
        "disagreeing": disagreeing,
        "agreeing_families": sorted(agreeing_families),
        "disagreeing_families": sorted(disagreeing_families),
        "verdict": verdict,
        "median_price": round(median_price, 4) if median_price is not None else None,
        "spread_pct": round(spread_pct, 2) if spread_pct is not None else None,
        "evaluated_at": int(observed_now),
        "cache_state": "live",
    }


def evaluate_quorum(symbol: str, *, use_cache: bool = False) -> Dict[str, Any]:
    """Return an advisory quorum with optional TTL cache and single-flight."""
    sym = (symbol or "").strip().upper()
    if not _SYMBOL_RE.fullmatch(sym):
        raise ValueError("invalid equity symbol")

    def cached() -> Optional[Dict[str, Any]]:
        if not use_cache:
            return None
        with _CACHE_LOCK:
            hit = _CACHE.get(sym)
        if hit and time.monotonic() < hit[0]:
            result = dict(hit[1])
            result["cache_state"] = "cached"
            return result
        return None

    hit = cached()
    if hit is not None:
        return hit
    with _key_lock(sym):
        hit = cached()
        if hit is not None:
            return hit
        result = _uncached_quorum(sym)
        if use_cache:
            ttl = CACHE_TTL_S if result["verdict"] != "insufficient" else NEGATIVE_CACHE_TTL_S
            with _CACHE_LOCK:
                _CACHE[sym] = (time.monotonic() + max(0, ttl), dict(result))
        return result
