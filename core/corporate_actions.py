"""Corporate-action awareness for the advisory discovery lane.

WHY THIS EXISTS
---------------
On its ex-dividend date a stock opens lower by roughly the dividend it just
detached, against an unadjusted previous close. Nobody sold. Nothing broke.
The holder is exactly as wealthy as yesterday -- part of the value simply moved
from the share price into a cash payment.

The discovery lane ranks by ABSOLUTE move and had no idea this happens. Found
live on 2026-09-22: Frontline (FRO) detached $3.41/share on 2026-09-18, a >5%
step down on a ~$60 stock, and the lane was structurally certain to report it
as a crash -- indistinguishable, in the output, from a real collapse. Grep for
`ex_dividend`, `exDividend`, `dividendDate` or `corporate_action` across core/
returned nothing at all: the whole category was invisible.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not hide anything. The operator's standing rule is that Ghost never
misses a move, and a silent filter is exactly how a lane dies. The observed
`move_pct` is never rewritten -- it is what the tape printed and it stays. This
module only adds a SECOND, labelled number beside it:

    economic_move_pct = move_pct + (100 * cash_amount / previous_close)

which is the total return an actual holder earned. A row whose whole decline is
explained by a dividend is not deleted; it is routed to a separate, named list
so it is still visible while no longer posing as a selloff.

The adjustment is applied symmetrically, to gains as well as declines. Total
return is total return; switching it on only for the drops would be choosing
which number to believe after seeing its sign.

SOURCE
------
Polygon's `/v3/reference/dividends`, queried BY EX-DIVIDEND DATE rather than by
ticker: one bounded request returns every US ticker going ex on that session,
so the cost is per-day and not per-symbol. This is a reference endpoint, not
the grouped-daily aggregate that this account's plan already refuses (see
core/market_wide_snapshot.py), so it is checked and diagnosed independently.

FAIL OPEN, ALWAYS
-----------------
Every failure path -- no key, 403, timeout, breaker open, unparseable row --
returns coverage state and NO adjustment. A missing dividend feed must leave
the discovery lane exactly as it was, never suppress a row, and never let an
absent lookup be mistaken for "no dividend".
"""
from __future__ import annotations

import logging
import math
import os
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests

LOGGER = logging.getLogger("ghost.corporate_actions")

PROVIDER = "polygon_reference_dividends"
_ENDPOINT = "https://api.polygon.io/v3/reference/dividends"
_EXCHANGE_TZ = "America/New_York"

# Ex-dividend dates for a past session are immutable and for an upcoming one are
# published well in advance, so this is cached hard. The negative entry matters
# as much as the positive: without it a market with no dividends that day, or a
# provider that is down, would re-issue a request on every single API read.
_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, Any]], str]] = {}

# Sticky, same lesson as market_wide_snapshot: a plan that does not cover this
# endpoint is a permanent diagnosis, and letting it decay into the breaker's
# generic "unavailable" turns "your plan lacks this" into "something is flaky".
_NOT_AUTHORIZED: Dict[str, Any] = {}

_MAX_PAGES = 6
_PAGE_LIMIT = 1000


def _enabled() -> bool:
    return os.getenv("CORPORATE_ACTIONS_ENABLED", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _api_key() -> str:
    return (os.getenv("POLYGON_API_KEY") or "").strip()


def _cache_ttl_s() -> int:
    try:
        return max(300, int(os.getenv("CORPORATE_ACTIONS_CACHE_TTL_S", "21600")))
    except (TypeError, ValueError):
        return 21600


def session_date(ts: Any, *, tz_name: str = _EXCHANGE_TZ) -> Optional[str]:
    """Exchange-calendar date (YYYY-MM-DD) for an epoch-second observation.

    Ex-dividend dates are exchange dates, so they are resolved in the exchange's
    own timezone -- not the engine's SESSION_TZ (America/Chicago), which agrees
    only by luck outside the small hours.
    """
    try:
        epoch = int(ts)
    except (TypeError, ValueError, OverflowError):
        return None
    if epoch <= 0:
        return None
    try:
        from zoneinfo import ZoneInfo
        moment = datetime.fromtimestamp(epoch, tz=ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001 - tz database absent; UTC is close enough
        moment = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return moment.date().isoformat()


def _valid_day(day: str) -> bool:
    try:
        date.fromisoformat(str(day))
        return True
    except (TypeError, ValueError):
        return False


def _parse_results(payload: Any) -> Dict[str, Dict[str, Any]]:
    """Keep the LARGEST cash amount per ticker for the day.

    A ticker can detach more than one distribution on the same ex-date (a
    regular quarterly plus a special, say). Summing them would be arithmetically
    right but would silently merge two different corporate events into one
    unnamed number, so the dominant one is kept and the count is recorded.
    """
    out: Dict[str, Dict[str, Any]] = {}
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return out
    for row in results:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        try:
            cash = float(row.get("cash_amount"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(cash) or cash <= 0:
            continue
        if str(row.get("currency") or "USD").upper() != "USD":
            continue
        prior = out.get(ticker)
        if prior is not None and float(prior["cash_amount"]) >= cash:
            prior["distributions"] = int(prior.get("distributions", 1)) + 1
            continue
        out[ticker] = {
            "kind": "ex_dividend",
            "cash_amount": cash,
            "currency": "USD",
            "ex_dividend_date": str(row.get("ex_dividend_date") or ""),
            "dividend_type": row.get("dividend_type"),
            "frequency": row.get("frequency"),
            "pay_date": row.get("pay_date"),
            "provider": PROVIDER,
            "distributions": 1 if prior is None else int(prior.get("distributions", 1)) + 1,
        }
    return out


def _fetch_day(day: str, *, timeout_s: float = 6.0) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """One bounded, paginated read of every US ex-dividend on `day`."""
    key = _api_key()
    if not key:
        return {}, "no_api_key"
    if _NOT_AUTHORIZED:
        return {}, "provider_not_authorized"
    from core.circuit_breaker import _polygon_corp_actions_cb
    if not _polygon_corp_actions_cb.allow():
        return {}, "provider_breaker_open"

    found: Dict[str, Dict[str, Any]] = {}
    url: Optional[str] = _ENDPOINT
    params: Optional[Dict[str, Any]] = {
        "ex_dividend_date": day, "limit": _PAGE_LIMIT, "apiKey": key,
    }
    try:
        for _ in range(_MAX_PAGES):
            if not url:
                break
            response = requests.get(
                url, params=params,
                headers={"User-Agent": "GhostProtocol/2.5"},
                timeout=max(1.0, min(15.0, float(timeout_s))),
            )
            # 401/403 is a plan/permission verdict, not a transient fault. Record
            # it once, do not spend the breaker's failure budget describing it.
            if response.status_code in (401, 403):
                _NOT_AUTHORIZED.clear()
                _NOT_AUTHORIZED.update({
                    "status_code": response.status_code,
                    "endpoint": _ENDPOINT,
                    "observed_at": int(time.time()),
                })
                LOGGER.warning(
                    "CORPORATE ACTIONS: Polygon returned %d for the dividends "
                    "reference endpoint; ex-dividend labelling is OFF until the "
                    "plan covers it. Discovery moves stay unadjusted and are "
                    "reported as unadjusted.", response.status_code,
                )
                return {}, "provider_not_authorized"
            response.raise_for_status()
            payload = response.json()
            found.update(_parse_results(payload))
            next_url = payload.get("next_url") if isinstance(payload, dict) else None
            url = str(next_url) if next_url else None
            params = {"apiKey": key} if url else None
        _polygon_corp_actions_cb.record_success()
        return found, "available"
    except Exception as exc:  # noqa: BLE001 - advisory lane, never a gate
        _polygon_corp_actions_cb.record_failure()
        LOGGER.warning("corporate actions unavailable day=%s type=%s", day, type(exc).__name__)
        return {}, "provider_request_failed"


def ex_dividends_on(day: str) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """Every US ex-dividend on one exchange date, cached positively and negatively."""
    if not _enabled():
        return {}, "disabled"
    if not _valid_day(day):
        return {}, "invalid_day"
    cached = _CACHE.get(day)
    now = time.time()
    if cached is not None and now - cached[0] < _cache_ttl_s():
        return cached[1], cached[2]
    found, status = _fetch_day(day)
    _CACHE[day] = (now, found, status)
    return found, status


def previous_close_from(price: Any, move_pct: Any) -> Optional[float]:
    """Recover the unadjusted prior close the provider's move was measured against.

    The observation carries a price and a percentage change but not the base they
    were computed from, and the base is what a dividend must be expressed against.
    """
    try:
        last = float(price)
        move = float(move_pct)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(last) and math.isfinite(move)) or last <= 0:
        return None
    ratio = 1.0 + move / 100.0
    if ratio <= 1e-6:  # a -100% move has no recoverable base
        return None
    base = last / ratio
    return base if math.isfinite(base) and base > 0 else None


def annotate_move(
    symbol: str, *, move_pct: Any, price: Any, observed_ts: Any,
) -> Dict[str, Any]:
    """Label one observation with its corporate action and total-return move.

    Returns `economic_move_pct=None` whenever the answer is not KNOWN -- feed
    down, no key, unrecoverable base. A caller must treat None as "unadjusted",
    never as "no dividend": those are different facts and conflating them is how
    a dead feed silently becomes a clean bill of health.
    """
    out: Dict[str, Any] = {
        "corporate_action": None,
        "mechanical_move_pct": None,
        "economic_move_pct": None,
        "corporate_action_coverage": "unavailable",
        "session_date": None,
    }
    ticker = str(symbol or "").strip().upper()
    day = session_date(observed_ts)
    out["session_date"] = day
    if not ticker or not day:
        return out

    actions, status = ex_dividends_on(day)
    if status != "available":
        out["corporate_action_coverage"] = status
        return out

    action = actions.get(ticker)
    if action is None:
        # Known-good feed, no distribution: the observed move IS the economic one.
        out["corporate_action_coverage"] = "no_action"
        try:
            observed = float(move_pct)
            out["economic_move_pct"] = observed if math.isfinite(observed) else None
        except (TypeError, ValueError):
            pass
        return out

    base = previous_close_from(price, move_pct)
    if base is None:
        out["corporate_action"] = dict(action)
        out["corporate_action_coverage"] = "matched_unpriced"
        return out

    dividend_pct = 100.0 * float(action["cash_amount"]) / base
    observed = float(move_pct)
    out.update({
        "corporate_action": {**action, "previous_close": round(base, 4)},
        "mechanical_move_pct": round(-dividend_pct, 4),
        "economic_move_pct": round(observed + dividend_pct, 4),
        "corporate_action_coverage": "matched",
    })
    return out


def corporate_action_status() -> Dict[str, Any]:
    """Operator-facing state of this lane; never used as a gate."""
    return {
        "provider": PROVIDER,
        "enabled": _enabled(),
        "api_key_configured": bool(_api_key()),
        "not_authorized": dict(_NOT_AUTHORIZED) or None,
        "cached_days": sorted(_CACHE),
        "cache_status": {day: entry[2] for day, entry in sorted(_CACHE.items())},
        "advisory_only": True,
        "decision_eligible": False,
    }
