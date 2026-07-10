"""core/fundamental_features.py — point-in-time SEC fundamentals as model features (PR #165).

The model's 49 features contain ZERO fundamentals — while the fundamental
shadow brain (which reads checklist EPS/revenue scores) is the best-performing
brain on the live scoreboard. This module feeds the same signal into the
actual feature vector, POINT-IN-TIME:

For a training bar dated D, a quarterly value is visible only if its SEC
``filed`` date <= D. No lookahead: a quarter that existed at D but was filed
after D does not exist for that bar. Amended filings only win once filed.

Features (all neutral-0.0 when unavailable — honest cold-start):
  fund_eps_yoy           EPS YoY change, clamped to [-2, 2] (fraction)
  fund_rev_yoy           Revenue YoY growth, clamped to [-2, 2] (fraction)
  fund_days_since_filing Days since the latest visible filing, capped 365
                         (recency of information; 365 also = "nothing known")

Enabled via V3_FUNDAMENTAL_FEATURES=on (default OFF). Toggling changes the
feature schema exactly like V3_SECTOR_FEATURE — stored models go stale and
retrain with the new columns. Sweep-verify the edge delta offline BEFORE
enabling in production training.

Data: SEC XBRL companyconcept via core.sec_fundamentals' fetch helpers
(known-reachable from the Railway box). Full series cached per symbol
in-process with TTL; backtests hit the cache once per symbol.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.fundamental_features")

FUNDAMENTAL_FEATURE_NAMES = ["fund_eps_yoy", "fund_rev_yoy", "fund_days_since_filing"]

_EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")
_REV_TAGS = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
             "SalesRevenueNet")

_CACHE: Dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()


def enabled() -> bool:
    return (os.getenv("V3_FUNDAMENTAL_FEATURES", "off") or "off").strip().lower() in (
        "1", "on", "true", "yes")


def _cache_ttl_s() -> int:
    return max(60, int(os.getenv("FUND_FEATURES_TTL_S", "21600")))  # 6h


def _neutral() -> Dict[str, float]:
    return {"fund_eps_yoy": 0.0, "fund_rev_yoy": 0.0,
            "fund_days_since_filing": 365.0}


def _clamp(v: float, lo: float = -2.0, hi: float = 2.0) -> float:
    return max(lo, min(hi, v))


def _rows_for_tags(cik: str, tags) -> List[Dict[str, Any]]:
    """Quarterly rows (val/end/fp/filed) for the first tag with data.

    Unlike sec_fundamentals._quarterly_series, this keeps EVERY filing row
    (no dedup) — point-in-time selection needs the full filed history so an
    amendment only replaces the original once its own filed date passes.
    """
    from core.sec_fundamentals import _finite, _get_concept, _span_days
    for tag in tags:
        concept = _get_concept(cik, tag)
        units = (concept or {}).get("units") or {}
        if not units:
            continue
        key = "USD/shares" if "USD/shares" in units else next(iter(units))
        rows = []
        for x in units[key]:
            val = _finite(x.get("val"))
            if val is None or not x.get("end") or not x.get("filed"):
                continue
            if x.get("form") not in ("10-Q", "10-K"):
                continue
            if x.get("start") and _span_days(x) > 100:
                continue  # skip cumulative YTD/annual spans
            rows.append({"val": val, "end": x["end"], "fp": x.get("fp"),
                         "filed": x["filed"]})
        if rows:
            return sorted(rows, key=lambda r: (r["end"], r["filed"]))
    return []


def _series(symbol: str) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """Full filed-history EPS + revenue rows for a symbol (cached)."""
    sym = (symbol or "").upper()
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(sym)
        if hit and now < hit[0]:
            return hit[1]
    try:
        from core.sec_fundamentals import cik_for_symbol
        cik = cik_for_symbol(sym)
        if not cik:
            data = None
        else:
            data = {"eps": _rows_for_tags(cik, _EPS_TAGS),
                    "rev": _rows_for_tags(cik, _REV_TAGS)}
            if not data["eps"] and not data["rev"]:
                data = None
    except Exception as exc:
        LOGGER.debug("fundamentals series %s failed: %s", sym, str(exc)[:80])
        data = None
    with _CACHE_LOCK:
        _CACHE[sym] = (now + _cache_ttl_s(), data)
    return data


def _visible_series_asof(rows: List[Dict[str, Any]], asof: str) -> List[Dict[str, Any]]:
    """Rows filed on/before asof, deduped by period-end (latest filed wins)."""
    vis = [r for r in rows if r["filed"] <= asof]
    dedup: Dict[str, Dict[str, Any]] = {}
    for r in vis:  # rows sorted by (end, filed): later filed overwrites
        dedup[r["end"]] = r
    return sorted(dedup.values(), key=lambda r: r["end"])


def _yoy_asof(rows: List[Dict[str, Any]], asof: str) -> Optional[float]:
    """YoY change of the latest visible quarter vs same fiscal period prior year."""
    series = _visible_series_asof(rows, asof)
    if len(series) < 2:
        return None
    latest = series[-1]
    prior = None
    for x in reversed(series[:-1]):
        if latest.get("fp") and x.get("fp") == latest.get("fp") and x["end"] != latest["end"]:
            prior = x
            break
    if prior is None:
        prior = series[-2]
    base = abs(prior["val"])
    if base < 1e-9:
        return None
    return (latest["val"] - prior["val"]) / base


def _latest_filed_asof(data: Dict[str, List[Dict[str, Any]]], asof: str) -> Optional[str]:
    filed = [r["filed"] for rows in data.values() for r in rows if r["filed"] <= asof]
    return max(filed) if filed else None


def pit_features_from_series(data: Optional[Dict[str, List[Dict[str, Any]]]],
                             asof: str) -> Dict[str, float]:
    """Pure point-in-time feature computation (testable without network)."""
    out = _neutral()
    if not data or not asof:
        return out
    eps = _yoy_asof(data.get("eps") or [], asof)
    rev = _yoy_asof(data.get("rev") or [], asof)
    if eps is not None:
        out["fund_eps_yoy"] = round(_clamp(eps), 4)
    if rev is not None:
        out["fund_rev_yoy"] = round(_clamp(rev), 4)
    last_filed = _latest_filed_asof(data, asof)
    if last_filed:
        try:
            d = (_dt.date.fromisoformat(asof[:10])
                 - _dt.date.fromisoformat(last_filed[:10])).days
            out["fund_days_since_filing"] = float(max(0, min(365, d)))
        except Exception:
            pass
    return out


def get_fundamental_features_for_date(symbol: str, date_str: str) -> Dict[str, float]:
    """Point-in-time fundamental features for one bar date. Never raises."""
    try:
        return pit_features_from_series(_series(symbol), str(date_str)[:10])
    except Exception as exc:
        LOGGER.debug("fundamental features %s@%s: %s", symbol, date_str, str(exc)[:80])
        return _neutral()


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
