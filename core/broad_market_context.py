"""Cached broad-market futures/proxy context for advisory display.

The snapshot is intentionally excluded from Hunter scoring and all trade gates.
A future and its ETF proxy are labeled separately so a proxy is never presented
as a true futures quote.
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Any, Dict, List, Optional, Sequence

LOGGER = logging.getLogger("ghost.broad_market")
MARKET_INSTRUMENTS = (
    {"symbol": "ES=F", "label": "S&P 500 E-mini", "kind": "future", "proxy_for": "SPX"},
    {"symbol": "SPY", "label": "S&P 500 ETF", "kind": "etf_proxy", "proxy_for": "SPX"},
    {"symbol": "NQ=F", "label": "Nasdaq 100 E-mini", "kind": "future", "proxy_for": "NDX"},
    {"symbol": "QQQ", "label": "Nasdaq 100 ETF", "kind": "etf_proxy", "proxy_for": "NDX"},
    {"symbol": "RTY=F", "label": "Russell 2000 E-mini", "kind": "future", "proxy_for": "RUT"},
    {"symbol": "IWM", "label": "Russell 2000 ETF", "kind": "etf_proxy", "proxy_for": "RUT"},
    {"symbol": "YM=F", "label": "Dow E-mini", "kind": "future", "proxy_for": "DJI"},
    {"symbol": "DIA", "label": "Dow ETF", "kind": "etf_proxy", "proxy_for": "DJI"},
    {"symbol": "^VIX", "label": "CBOE Volatility Index", "kind": "index", "proxy_for": "VOLATILITY"},
)


def normalize_market_observation(
    instrument: Dict[str, str],
    *,
    price: Any,
    previous_close: Any,
    source_ts: Any,
    received_at: Optional[int] = None,
    provider: str = "yahoo",
    max_age_s: int = 1800,
) -> Dict[str, Any]:
    """Validate a quote while preserving an unknown source timestamp."""
    now = int(received_at or time.time())
    reasons: List[str] = []
    try:
        px = float(price)
        if not math.isfinite(px) or px <= 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        px = None
        reasons.append("invalid_price")
    try:
        prev = float(previous_close)
        if not math.isfinite(prev) or prev <= 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        prev = None
        reasons.append("invalid_previous_close")
    try:
        observed = int(source_ts)
    except (TypeError, ValueError, OverflowError):
        observed = None
        reasons.append("missing_source_timestamp")
    age = None if observed is None else now - observed
    if age is not None and age < -60:
        reasons.append("future_source_timestamp")
    stale = age is None or age > max_age_s or age < -60
    change_pct = None
    if px is not None and prev is not None:
        change_pct = round((px - prev) / prev * 100.0, 3)
    return {
        **instrument,
        "provider": provider,
        "provider_family": "yahoo",
        "price": px,
        "previous_close": prev,
        "change_pct": change_pct,
        "source_ts": observed,
        "received_at": now,
        "source_age_s": age,
        "stale": stale,
        "valid": not reasons,
        "validation_reasons": reasons,
        "display_only": True,
        "decision_eligible": False,
    }


def build_market_snapshot(
    observations: Sequence[Dict[str, Any]],
    *,
    provider_status: Optional[Dict[str, Any]] = None,
    received_at: Optional[int] = None,
) -> Dict[str, Any]:
    now = int(received_at or time.time())
    items = list(observations)
    valid = [row for row in items if row.get("valid")]
    fresh = [row for row in valid if not row.get("stale")]
    status = (
        "complete" if len(fresh) == len(MARKET_INSTRUMENTS)
        else "partial" if fresh
        else "stale" if valid
        else "unavailable"
    )
    timestamps = [int(row["source_ts"]) for row in valid if row.get("source_ts") is not None]
    return {
        "ok": bool(fresh), "status": status,
        "snapshot_id": f"broad-market:{now // 60}",
        "observed_at": max(timestamps) if timestamps else None,
        "received_at": now, "observations": items,
        "valid_count": len(valid), "fresh_count": len(fresh),
        "expected_count": len(MARKET_INSTRUMENTS),
        "provider_status": provider_status or {},
        "display_only": True, "decision_eligible": False,
        "scoring_version": None,
        "note": "Display context only; not included in squeeze or trade scoring.",
    }


def _batch_yahoo_observations(*, received_at: int) -> List[Dict[str, Any]]:
    """One bounded batch download; derive timestamps from actual market bars."""
    import yfinance as yf

    symbols = [item["symbol"] for item in MARKET_INSTRUMENTS]
    frame = yf.download(
        tickers=symbols, period="5d", interval="5m", group_by="ticker",
        auto_adjust=False, progress=False, threads=False, timeout=10,
        prepost=True,
    )
    daily_frame = yf.download(
        tickers=symbols, period="10d", interval="1d", group_by="ticker",
        auto_adjust=False, progress=False, threads=False, timeout=10,
    )
    rows: List[Dict[str, Any]] = []
    for instrument in MARKET_INSTRUMENTS:
        symbol = instrument["symbol"]
        try:
            part = frame[symbol] if len(symbols) > 1 else frame
            closes = part["Close"].dropna()
            if closes.empty:
                raise ValueError("no bars")
            last_index = closes.index[-1]
            observed = int(last_index.timestamp())
            price = float(closes.iloc[-1])
            daily_part = daily_frame[symbol] if len(symbols) > 1 else daily_frame
            daily_closes = daily_part["Close"].dropna()
            previous_close = float(
                daily_closes.iloc[-2] if len(daily_closes) > 1 else daily_closes.iloc[-1]
            )
            rows.append(normalize_market_observation(
                instrument, price=price, previous_close=previous_close,
                source_ts=observed, received_at=received_at,
                max_age_s=max(300, int(os.getenv("BROAD_MARKET_MAX_AGE_S", "1800"))),
            ))
        except Exception:
            rows.append(normalize_market_observation(
                instrument, price=None, previous_close=None, source_ts=None,
                received_at=received_at,
            ))
    return rows


def refresh_broad_market_context() -> Dict[str, Any]:
    """Leader-scheduled provider refresh and immutable snapshot write."""
    enabled = os.getenv("BROAD_MARKET_CONTEXT_ENABLED", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if not enabled:
        return build_market_snapshot([], provider_status={"yahoo": {"status": "disabled"}})
    received = int(time.time())
    from core.circuit_breaker import _yfinance_market_context_cb
    if not _yfinance_market_context_cb.allow():
        snapshot = build_market_snapshot(
            [], provider_status={"yahoo": {"status": "unavailable", "reason": "provider_breaker_open"}},
            received_at=received,
        )
    else:
        try:
            observations = _batch_yahoo_observations(received_at=received)
            if any(row.get("valid") for row in observations):
                _yfinance_market_context_cb.record_success()
            else:
                _yfinance_market_context_cb.record_failure()
            snapshot = build_market_snapshot(
                observations,
                provider_status={"yahoo": {"status": "available", "received_at": received}},
                received_at=received,
            )
        except Exception as exc:
            _yfinance_market_context_cb.record_failure()
            LOGGER.warning("broad market refresh unavailable type=%s", type(exc).__name__)
            snapshot = build_market_snapshot(
                [], provider_status={"yahoo": {"status": "unavailable", "reason": "provider_request_failed"}},
                received_at=received,
            )
    from core.external_context_ledger import store_market_context_snapshot
    store_market_context_snapshot(snapshot)
    return snapshot


def get_broad_market_context() -> Dict[str, Any]:
    """Snapshot-only reader for public API and board surfaces."""
    from core.external_context_ledger import latest_market_context
    return latest_market_context()
