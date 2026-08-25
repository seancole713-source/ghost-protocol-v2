"""Bounded adapters for external squeeze/anomaly discovery.

This module writes advisory observations only. It never mutates Ghost's symbol
universe, creates candidates, sends alerts, changes confidence, or touches a
wallet. Public routes consume the completed ledger snapshot instead of polling.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from core.external_context_ledger import (
    normalize_external_observation,
    store_external_observation,
)

LOGGER = logging.getLogger("ghost.external_screener")
_YAHOO_ENDPOINT = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
_DEFAULT_SCREENS: Tuple[str, ...] = ("day_gainers", "most_shorted_stocks")


def _enabled() -> bool:
    return os.getenv("EXTERNAL_SCREENER_ENABLED", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _screens() -> Tuple[str, ...]:
    allowed = frozenset(_DEFAULT_SCREENS)
    requested = [
        item.strip().lower()
        for item in os.getenv("EXTERNAL_SCREENER_SCREENS", ",".join(_DEFAULT_SCREENS)).split(",")
        if item.strip()
    ]
    return tuple(item for item in requested if item in allowed)[:2]


def _quote_point(quote: Dict[str, Any]) -> Tuple[Any, Any]:
    """Choose a matched price/timestamp pair without using ingestion time."""
    state = str(quote.get("marketState") or "").upper()
    candidates = []
    if state in {"PRE", "PREPRE"}:
        candidates.append((quote.get("preMarketPrice"), quote.get("preMarketTime")))
    if state in {"POST", "POSTPOST", "CLOSED"}:
        candidates.append((quote.get("postMarketPrice"), quote.get("postMarketTime")))
    candidates.append((quote.get("regularMarketPrice"), quote.get("regularMarketTime")))
    for price, observed_at in candidates:
        if price is not None and observed_at is not None:
            return price, observed_at
    return None, None


def parse_yahoo_screen(
    payload: Dict[str, Any],
    *,
    screen: str,
    received_ts: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Normalize one Yahoo saved-screen response into immutable ledger rows."""
    finance = payload.get("finance") if isinstance(payload, dict) else None
    results = finance.get("result") if isinstance(finance, dict) else None
    result = results[0] if isinstance(results, list) and results else {}
    quotes = result.get("quotes") if isinstance(result, dict) else None
    if not isinstance(quotes, list):
        return []
    rows = []
    for index, quote in enumerate(quotes, start=1):
        if not isinstance(quote, dict):
            continue
        price, source_ts = _quote_point(quote)
        avg_volume = quote.get("averageDailyVolume3Month")
        volume = quote.get("regularMarketVolume")
        rows.append(normalize_external_observation(
            provider="yahoo_saved_screener",
            provider_family="yahoo",
            screen=screen,
            raw_symbol=quote.get("symbol"),
            source_ts=source_ts,
            received_ts=received_ts,
            observation_id=(
                f"{screen}:{quote.get('symbol')}:{source_ts}:"
                f"{quote.get('regularMarketVolume')}"
            ),
            rank=index,
            price=price,
            move_pct=quote.get("regularMarketChangePercent"),
            volume=volume,
            avg_volume=avg_volume,
            external_score=(
                quote.get("shortPercentOfFloat")
                if screen == "most_shorted_stocks"
                else quote.get("regularMarketChangePercent")
            ),
            delayed=bool(quote.get("exchangeDataDelayedBy", 0)),
            payload=quote,
            max_age_s=max(300, int(os.getenv("EXTERNAL_SCREENER_MAX_AGE_S", "1800"))),
        ))
    return rows


def fetch_yahoo_screen(
    screen: str,
    *,
    count: int = 25,
    timeout_s: float = 8.0,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch one allowlisted screen with a finite timeout and row bound."""
    if screen not in _DEFAULT_SCREENS:
        raise ValueError("unsupported external screen")
    from core.circuit_breaker import _yahoo_screener_cb
    if not _yahoo_screener_cb.allow():
        return [], {"status": "unavailable", "reason": "provider_breaker_open"}
    received = int(time.time())
    try:
        response = requests.get(
            _YAHOO_ENDPOINT,
            params={"formatted": "false", "scrIds": screen,
                    "count": max(1, min(50, int(count))), "start": 0},
            headers={"User-Agent": "Mozilla/5.0 GhostProtocol/2.5"},
            timeout=max(1.0, min(15.0, float(timeout_s))),
        )
        response.raise_for_status()
        payload = response.json()
        rows = parse_yahoo_screen(payload, screen=screen, received_ts=received)
        _yahoo_screener_cb.record_success()
        return rows, {"status": "available", "rows": len(rows), "received_ts": received}
    except Exception as exc:
        _yahoo_screener_cb.record_failure()
        LOGGER.warning("external screen unavailable screen=%s type=%s", screen, type(exc).__name__)
        return [], {"status": "unavailable", "reason": "provider_request_failed",
                    "received_ts": received}


def ingest_rows(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Store normalized rows and return explicit validation/quarantine counts."""
    counts = {"received": 0, "inserted": 0, "valid": 0, "invalid": 0,
              "official": 0, "quarantined": 0}
    for row in rows:
        counts["received"] += 1
        counts["valid" if row.get("validation_valid") else "invalid"] += 1
        counts["official" if row.get("in_official_watchlist") else "quarantined"] += 1
        if store_external_observation(row):
            counts["inserted"] += 1
    return counts


def run_external_screener_cycle() -> Dict[str, Any]:
    """Leader-scheduled refresh. Safe partial failure; bounded to two requests."""
    if not _enabled():
        return {"ok": True, "status": "disabled", "screens": {},
                "advisory_only": True, "decision_eligible": False}
    statuses: Dict[str, Any] = {}
    totals = {"received": 0, "inserted": 0, "valid": 0, "invalid": 0,
              "official": 0, "quarantined": 0}
    for screen in _screens():
        rows, provider_status = fetch_yahoo_screen(screen)
        counts = ingest_rows(rows) if rows else dict.fromkeys(totals, 0)
        statuses[screen] = {**provider_status, **counts}
        for key in totals:
            totals[key] += int(counts.get(key) or 0)
    available = sum(state.get("status") == "available" for state in statuses.values())
    status = "complete" if available == len(statuses) and statuses else "partial" if available else "unavailable"
    from core.external_context_ledger import prune_external_context
    retention = prune_external_context()
    return {
        "ok": available > 0, "status": status, "screens": statuses, **totals,
        "retention": retention,
        "advisory_only": True, "decision_eligible": False,
        "note": "External discovery creates visibility only; trade approval remains native.",
    }
