"""
core/edgar_integration.py — SEC EDGAR 8-K filing fetcher.

Fetches recent 8-K filings for WOLF via the SEC EDGAR submissions API
(free, no key required). Parses material events from filing items and
feeds structured results into core.wolf_context.

Rate limit: SEC.gov asks for ≤10 requests/second. This module makes
one request per symbol per check cycle (best-effort, cached 1h).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

LOGGER = logging.getLogger("ghost.edgar")

# SEC EDGAR requires a User-Agent identifying your organization/email.
_SEC_USER_AGENT = os.getenv(
    "EDGAR_USER_AGENT",
    "GhostProtocol/2.1 (seancole713-source/ghost-protocol-v2)",
)
_SEC_TIMEOUT = float(os.getenv("EDGAR_TIMEOUT_S", "10.0"))
_CACHE_TTL_S = int(os.getenv("EDGAR_CACHE_TTL_S", "3600"))  # 1 hour

# CIK for Wolfspeed Inc (0000895419). Hardcoded — WOLF is the anchor ticker.
_WOLF_CIK = "0000895419"

# 8-K items that are material for a directional trading signal.
_MATERIAL_ITEMS = {
    "1.01": "material_agreement",
    "1.02": "agreement_termination",
    "2.01": "asset_acquisition_disposition",
    "2.02": "earnings_results",       # quarterly results
    "2.03": "financial_obligation",
    "2.04": "triggering_event_accelerated",
    "2.05": "exit_cost_disposal",
    "2.06": "material_impairment",
    "3.01": "delisting_notice",
    "3.02": "unregistered_sales",
    "3.03": "material_modification_rights",
    "4.01": "accountant_change",
    "4.02": "non_reliance_financials",
    "5.01": "director_change",
    "5.02": "officer_departure_election",  # CEO/CFO changes
    "5.03": "articles_bylaws_amendment",
    "5.05": "code_of_ethics_amendment",
    "5.07": "say_on_pay_vote",
    "7.01": "regulation_fd_disclosure",
    "8.01": "other_events",            # often material
    "9.01": "financial_statements_exhibits",
}

# Cache: {symbol: (timestamp, payload)}
_edgar_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}


def _cik_for_symbol(symbol: str) -> Optional[str]:
    """Map ticker to CIK. Currently WOLF-only; extendable via env override."""
    sym = (symbol or "").upper()
    if sym == "WOLF":
        return os.getenv("EDGAR_WOLF_CIK", _WOLF_CIK)
    # Generic lookup via SEC company_tickers.json (free, updated daily)
    override = os.getenv(f"EDGAR_CIK_{sym}")
    if override:
        return override
    return None


def _fetch_submissions(cik: str) -> Optional[Dict[str, Any]]:
    """Fetch the submissions (filing index) for a CIK from SEC EDGAR."""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        r = requests.get(
            url,
            headers={"User-Agent": _SEC_USER_AGENT},
            timeout=_SEC_TIMEOUT,
        )
        if r.status_code == 200:
            return r.json()
        LOGGER.warning("EDGAR submissions %s: HTTP %s", cik, r.status_code)
    except Exception as e:
        LOGGER.warning("EDGAR submissions %s: %s", cik, str(e)[:120])
    return None


def _parse_8k_items(filing: Dict[str, Any]) -> List[str]:
    """Extract 8-K item numbers from a filing's items array."""
    items = []
    raw_items = filing.get("items") or []
    for item in raw_items:
        item_str = str(item).strip()
        if item_str in _MATERIAL_ITEMS:
            items.append(item_str)
    return items


def fetch_recent_8k(symbol: str, days: int = 90) -> Dict[str, Any]:
    """Fetch recent 8-K filings for a symbol from SEC EDGAR.

    Returns structured dict with filings list and material event summary.
    Cached for 1 hour per symbol.
    """
    sym = (symbol or "").upper()
    now = time.time()

    # Check cache
    cached = _edgar_cache.get(sym)
    if cached and (now - cached[0]) < _CACHE_TTL_S:
        return dict(cached[1])

    cik = _cik_for_symbol(sym)
    if not cik:
        result = {
            "available": False,
            "reason": "no_cik_mapping",
            "symbol": sym,
            "filings": [],
            "note": f"No CIK mapping for {sym}. Set EDGAR_CIK_{sym} env var.",
        }
        _edgar_cache[sym] = (now, result)
        return result

    submissions = _fetch_submissions(cik)
    if not submissions:
        result = {
            "available": False,
            "reason": "sec_api_failed",
            "symbol": sym,
            "cik": cik,
            "filings": [],
        }
        _edgar_cache[sym] = (now, result)
        return result

    # Filter to recent 8-K filings
    filings = submissions.get("filings", {})
    recent = filings.get("recent", {})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    eight_k_filings = []
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    acc_numbers = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    items_list = recent.get("items", [])

    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        filing_date = dates[i] if i < len(dates) else ""
        if filing_date < cutoff:
            continue
        acc = acc_numbers[i] if i < len(acc_numbers) else ""
        doc = primary_docs[i] if i < len(primary_docs) else ""
        items = items_list[i] if i < len(items_list) else []

        material_items = []
        for item in (items if isinstance(items, list) else [items]):
            item_str = str(item).strip()
            if item_str in _MATERIAL_ITEMS:
                material_items.append({
                    "item": item_str,
                    "category": _MATERIAL_ITEMS[item_str],
                })

        eight_k_filings.append({
            "filing_date": filing_date,
            "accession_number": acc,
            "document": doc,
            "items": material_items,
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{acc.replace('-', '')}/{doc}"
            if acc and doc else None,
        })

    # Sort newest first
    eight_k_filings.sort(key=lambda f: f["filing_date"], reverse=True)

    # Material event summary
    material_events = []
    for f in eight_k_filings:
        for item in f["items"]:
            material_events.append({
                "date": f["filing_date"],
                "category": item["category"],
                "item": item["item"],
            })

    result = {
        "available": True,
        "symbol": sym,
        "cik": cik,
        "filings_count": len(eight_k_filings),
        "filings": eight_k_filings[:20],
        "material_events": material_events[:20],
        "latest_filing_date": eight_k_filings[0]["filing_date"] if eight_k_filings else None,
        "has_earnings": any(e["category"] == "earnings_results" for e in material_events),
        "has_delisting_risk": any(e["category"] == "delisting_notice" for e in material_events),
        "has_officer_change": any(e["category"] == "officer_departure_election" for e in material_events),
        "checked_at": int(now),
    }
    _edgar_cache[sym] = (now, result)
    return result


def parse_material_events(filings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Parse material events from a list of filing dicts.

    Returns flat list of {date, category, item} for WolfContext consumption.
    """
    events = []
    for f in filings or []:
        for item in f.get("items", []):
            events.append({
                "date": f.get("filing_date", ""),
                "category": item.get("category", "unknown"),
                "item": item.get("item", ""),
            })
    return events


def edgar_status() -> Dict[str, Any]:
    """Status for /api/ghost/blueprint phase reporting."""
    return {
        "available": True,
        "status": "live",
        "note": "SEC EDGAR 8-K fetcher active — WOLF CIK 0000895419, cached 1h",
    }


# ── EDGARClient class — matches wolf_context.py's expected interface ─────

from dataclasses import dataclass as _dc


@_dc
class FilingResult:
    """Single filing result matching wolf_context.WolfEdgarAlert fields."""
    filing_date: float  # unix timestamp
    urgency: str        # "low" | "medium" | "high" | "critical"
    items: list         # e.g. ["2.02", "5.02"]
    description: str    # human-readable summary
    sentiment_score: float  # -1.0 to +1.0


def _urgency_from_items(items: list) -> str:
    """Map 8-K items to urgency level."""
    critical_items = {"2.02", "3.01", "4.02", "2.04", "2.06"}
    high_items = {"5.02", "1.01", "1.02", "2.01", "2.03", "2.05", "5.01"}
    medium_items = {"3.02", "3.03", "4.01", "5.03", "5.07", "7.01", "8.01", "9.01"}
    item_set = set(items or [])
    if item_set & critical_items:
        return "critical"
    if item_set & high_items:
        return "high"
    if item_set & medium_items:
        return "medium"
    return "low"


def _sentiment_from_items(items: list) -> float:
    """Rough sentiment from 8-K item categories."""
    bearish = {"2.04", "2.06", "3.01", "4.02", "5.02"}
    bullish = {"1.01", "2.01", "2.02"}
    score = 0.0
    for item in (items or []):
        cat = _MATERIAL_ITEMS.get(item, "")
        if cat in ("triggering_event_accelerated", "material_impairment",
                    "delisting_notice", "non_reliance_financials",
                    "officer_departure_election"):
            score -= 0.3
        elif cat in ("material_agreement", "asset_acquisition_disposition",
                      "earnings_results"):
            score += 0.2
    return round(max(-1.0, min(1.0, score)), 2)


def _description_from_items(items: list) -> str:
    """Human-readable summary of 8-K items."""
    if not items:
        return "No material items"
    cats = []
    for item in items:
        cat = _MATERIAL_ITEMS.get(item, f"item_{item}")
        cats.append(cat.replace("_", " "))
    return ", ".join(cats)[:200]


class EDGARClient:
    """Client matching wolf_context.py's expected EDGAR interface."""

    def get_company_filings(
        self, cik: str, filing_type: str = "8-K", limit: int = 5
    ) -> list:
        """Return list of FilingResult for the most recent filings of type."""
        # Use the module-level fetch, keyed by CIK
        sym = "WOLF" if cik == _WOLF_CIK else f"CIK{cik}"
        result = fetch_recent_8k(sym, days=90)
        if not result.get("available"):
            return []
        filings = result.get("filings", [])[:limit]
        out = []
        for f in filings:
            try:
                fd = f.get("filing_date", "")
                ts = datetime.strptime(fd, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc).timestamp() if fd else 0.0
            except Exception:
                ts = 0.0
            items = [it["item"] for it in f.get("items", [])]
            out.append(FilingResult(
                filing_date=ts,
                urgency=_urgency_from_items(items),
                items=items,
                description=_description_from_items(items),
                sentiment_score=_sentiment_from_items(items),
            ))
        return out

