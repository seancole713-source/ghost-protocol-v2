"""core/sec_fundamentals.py - SEC XBRL fundamentals (EPS, revenue YoY).

PR #88 (Data Coverage Upgrade).

Feeds checklist items 1 (EPS) and 2 (revenue growth) from SEC's free XBRL
``companyconcept`` API on ``data.sec.gov`` - the same host the existing
``core/edgar_integration`` 8-K fetcher already uses successfully from the
Railway production box, so it is known-reachable where yfinance is blocked.

What we extract
---------------
- Diluted (fallback basic) quarterly EPS series -> latest reported quarter and
  the prior-year-same-quarter, used to derive a YoY EPS trend. (True analyst
  "estimate" beat/miss requires a paid estimates feed; until that exists we
  return a directional EPS-trend signal and clearly label it as such so the
  engine never pretends it is a consensus surprise.)
- Quarterly revenue series -> latest vs prior-year-same-quarter YoY growth.

Honesty rules:
- Never raise. Any failure returns ``{"available": False, ...}``.
- Never fabricate an estimate. We expose ``eps_actual`` / ``eps_year_ago`` and
  let ``super_ghost`` decide the score; the field names we emit are the ones
  ``_evaluate_company`` already understands.
- Cached per symbol (filings change at most quarterly).
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import os
import time
from typing import Any, Dict, List, Optional

import requests

LOGGER = logging.getLogger("ghost.sec_fundamentals")

_SEC_USER_AGENT = os.getenv(
    "EDGAR_USER_AGENT",
    "GhostProtocol/2.1 (seancole713-source/ghost-protocol-v2)",
)
_TIMEOUT = float(os.getenv("SEC_FUNDAMENTALS_TIMEOUT_S", "10.0"))
_CACHE_TTL_S = int(os.getenv("SEC_FUNDAMENTALS_TTL_S", "21600"))  # 6h

# Anchor CIK map (WOLF is the primary ticker). Extendable via EDGAR_CIK_<SYM>.
_WOLF_CIK = "0000895419"
_STATIC_CIK = {"WOLF": _WOLF_CIK}

# Small built-in map for common large-caps so fundamentals resolve even if the
# SEC ticker->CIK index is unreachable. Extend freely; EDGAR_CIK_<SYM> overrides.
_COMMON_CIK = {
    "AAPL": "0000320193", "MSFT": "0000789019", "NVDA": "0001045810",
    "TSLA": "0001318605", "AMZN": "0001018724", "GOOGL": "0001652044",
    "GOOG": "0001652044", "META": "0001326801", "AMD": "0000002488",
    "NFLX": "0001065280", "INTC": "0000050863", "PLTR": "0001321655",
}

# Cached ticker->CIK index from SEC (loaded lazily, best-effort).
_ticker_index: Dict[str, str] = {}
_ticker_index_loaded = False

_EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")
_REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
)

_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}


def _finite(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _load_ticker_index() -> None:
    """Best-effort load of SEC's ticker->CIK index. Never raises.

    Reachable from Railway (same SEC hosts as the working EDGAR fetcher); may be
    blocked from some sandboxes, in which case we silently keep the static maps.
    """
    global _ticker_index_loaded
    if _ticker_index_loaded:
        return
    _ticker_index_loaded = True
    for url in (
        "https://www.sec.gov/files/company_tickers.json",
        "https://data.sec.gov/files/company_tickers.json",
    ):
        try:
            r = requests.get(url, headers={"User-Agent": _SEC_USER_AGENT}, timeout=_TIMEOUT)
            if r.status_code == 200:
                data = r.json() or {}
                for v in data.values():
                    t = str(v.get("ticker") or "").upper()
                    cik = v.get("cik_str")
                    if t and cik is not None:
                        _ticker_index[t] = str(cik).zfill(10)
                if _ticker_index:
                    return
        except Exception as exc:
            LOGGER.debug("sec ticker index %s: %s", url, str(exc)[:100])


def cik_for_symbol(symbol: str) -> Optional[str]:
    sym = (symbol or "").upper()
    env = os.getenv(f"EDGAR_CIK_{sym}")
    if env:
        return env.zfill(10)
    if sym in _STATIC_CIK:
        return _STATIC_CIK[sym]
    if sym in _COMMON_CIK:
        return _COMMON_CIK[sym]
    # Best-effort dynamic resolution via SEC's public ticker index.
    _load_ticker_index()
    return _ticker_index.get(sym)


def _get_concept(cik: str, tag: str) -> Optional[Dict[str, Any]]:
    url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"
    try:
        r = requests.get(url, headers={"User-Agent": _SEC_USER_AGENT}, timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.json()
    except Exception as exc:
        LOGGER.debug("sec concept %s/%s: %s", cik, tag, str(exc)[:100])
    return None


def _span_days(row: Dict[str, Any]) -> int:
    try:
        s = _dt.date.fromisoformat(row["start"])
        e = _dt.date.fromisoformat(row["end"])
        return (e - s).days
    except Exception:
        return 999


def _quarterly_series(concept: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return deduped quarterly rows (oldest->newest) with val/end/fp/fy.

    Keeps only ~single-quarter spans (<=100 days) so YTD/annual cumulative
    figures do not contaminate the YoY comparison. Dedupes by period-end,
    keeping the most recently *filed* value (amended filings win).
    """
    units = (concept or {}).get("units") or {}
    if not units:
        return []
    key = "USD/shares" if "USD/shares" in units else next(iter(units))
    rows = []
    for x in units[key]:
        val = _finite(x.get("val"))
        if val is None or not x.get("end"):
            continue
        if x.get("form") not in ("10-Q", "10-K"):
            continue
        if x.get("start") and _span_days(x) > 100:
            continue  # skip cumulative YTD / annual spans
        rows.append({
            "val": val,
            "end": x.get("end"),
            "start": x.get("start"),
            "fp": x.get("fp"),
            "fy": x.get("fy"),
            "filed": x.get("filed") or "",
            "form": x.get("form"),
        })
    dedup: Dict[str, Dict[str, Any]] = {}
    for x in sorted(rows, key=lambda r: r.get("filed", "")):
        dedup[x["end"]] = x
    return sorted(dedup.values(), key=lambda r: r["end"])


def _yoy_from_series(series: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Latest quarter vs the same fiscal-period one year earlier."""
    if len(series) < 2:
        return None
    latest = series[-1]
    fp = latest.get("fp")
    prior = None
    for x in reversed(series[:-1]):
        if fp and x.get("fp") == fp and x.get("end") != latest.get("end"):
            prior = x
            break
    if prior is None:
        prior = series[-2]
    return {"latest": latest, "prior": prior}


def get_fundamentals(symbol: str) -> Dict[str, Any]:
    """Best-effort SEC fundamentals for a symbol.

    Returns a dict using the field names ``core.super_ghost._evaluate_company``
    already reads: ``actual_eps``/``eps_actual``, ``eps_year_ago``,
    ``revenue``, ``revenue_year_ago``, ``revenue_yoy``. ``available`` is True
    only if at least one of EPS or revenue resolved.
    """
    sym = (symbol or "").strip().upper()
    out: Dict[str, Any] = {"available": False, "symbol": sym, "source": "sec_xbrl"}
    if not sym:
        return out
    now = time.time()
    cached = _cache.get(sym)
    if cached and (now - cached[0]) < _CACHE_TTL_S:
        return dict(cached[1])

    cik = cik_for_symbol(sym)
    if not cik:
        out["reason"] = "no_cik_mapping"
        _cache[sym] = (now, out)
        return dict(out)

    # EPS series (diluted preferred, basic fallback).
    eps_series: List[Dict[str, Any]] = []
    for tag in _EPS_TAGS:
        concept = _get_concept(cik, tag)
        if concept:
            eps_series = _quarterly_series(concept)
            if eps_series:
                out["eps_tag"] = tag
                break
    if eps_series:
        eps_yoy = _yoy_from_series(eps_series)
        if eps_yoy:
            latest = eps_yoy["latest"]
            prior = eps_yoy["prior"]
            out["actual_eps"] = latest["val"]
            out["eps_actual"] = latest["val"]
            out["eps_year_ago"] = prior["val"]
            out["eps_period"] = f"{latest.get('fy')} {latest.get('fp')}"
            out["eps_basis"] = "yoy_trend"  # not a consensus surprise; honest label
            out["available"] = True

    # Revenue series.
    rev_series: List[Dict[str, Any]] = []
    for tag in _REVENUE_TAGS:
        concept = _get_concept(cik, tag)
        if concept:
            rev_series = _quarterly_series(concept)
            if rev_series:
                out["revenue_tag"] = tag
                break
    if rev_series:
        rev_yoy = _yoy_from_series(rev_series)
        if rev_yoy:
            latest = rev_yoy["latest"]["val"]
            prior = rev_yoy["prior"]["val"]
            out["revenue"] = latest
            out["revenue_year_ago"] = prior
            if prior and prior > 0:
                out["revenue_yoy"] = (latest - prior) / prior
            out["revenue_period"] = f"{rev_yoy['latest'].get('fy')} {rev_yoy['latest'].get('fp')}"
            out["available"] = True

    if not out["available"]:
        out["reason"] = "no_xbrl_facts"
    out["cik"] = cik
    out["checked_at"] = int(now)
    _cache[sym] = (now, out)
    return dict(out)


def clear_cache() -> None:
    _cache.clear()
