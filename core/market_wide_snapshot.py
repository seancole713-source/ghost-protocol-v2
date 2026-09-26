"""Full-market daily snapshot — every US ticker in one request.

The discovery lane added in PR #182 reads Yahoo's saved screens, which return
at most 50 rows each and are hardcoded to two screens (day_gainers,
most_shorted_stocks). That is ~100 symbols per cycle out of ~11,000 listed US
tickers, and because there is no day_losers screen it is structurally blind to
large DECLINES -- while the alert ranker sorts by ABSOLUTE move, so it was
written for crashes it could never be shown.

Polygon's grouped-daily aggregate returns every US ticker that traded on a
given day in a single HTTP call. Two calls (the last two trading days) give a
genuine close-to-close move for the entire market. That is the difference
between "the top 100 gainers" and "the market".

Three constraints this module holds:

  * ONE BASIS. move_pct is close-to-close or it is absent. Polygon's grouped
    bar carries open and close, so an intraday (c-o)/o move is available for
    free -- and it is a DIFFERENT number that would be silently mixed into the
    same column. When the prior trading day is unavailable, this module emits
    no move rather than a cheaper one.

  * SCAN EVERYTHING, STORE WHAT MATTERS. ~11,000 rows/day at 30-day retention
    is ~330k JSONB rows for a lane nobody reads at that granularity. Rows are
    stored only above a materiality bar (price, dollar volume, move size), and
    the cycle reports the FULL scanned count next to the stored count so the
    filter can never be mistaken for the coverage.

  * ADVISORY ONLY. Rows go through the same normalize/store path as every
    other external observation, so they inherit advisory_only=True and
    decision_eligible=False. Nothing here can create a candidate, size a
    position, or change a confidence. A discovery is a reason to look.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from core.external_context_ledger import (
    normalize_external_observation,
    store_external_observation,
)

LOGGER = logging.getLogger("ghost.market_wide")

PROVIDER = "polygon_grouped_daily"
PROVIDER_FAMILY = "polygon"
SCREEN = "market_wide_daily"
_ENDPOINT = "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{day}"

# A daily bar is stamped at the START of its session, so the freshest possible
# close-to-close row is already ~16h old when it lands and ~40h old over a
# weekend. The intraday screener's 30-minute bound would mark every one of them
# stale and validation_valid=FALSE, which is how a lane dies silently.
_DEFAULT_MAX_AGE_S = 4 * 86400


def _enabled() -> bool:
    return os.getenv("MARKET_WIDE_SNAPSHOT_ENABLED", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _api_key() -> str:
    return (os.getenv("POLYGON_API_KEY") or "").strip()


# Remembered so the diagnosis does not decay. Confirmed live 2026-09-05: Polygon
# answered 403 for grouped-daily on this account's plan. After five such cycles
# the circuit breaker opens and every later cycle would report the generic
# "provider_breaker_open" instead -- turning "your plan does not cover this
# endpoint" into "something is flaky", which is precisely the degradation this
# module exists to refuse. Sticky within the process, cleared on restart so a
# plan upgrade takes effect on the next deploy.
_NOT_AUTHORIZED: Dict[str, Any] = {}


def _remember_not_authorized(detail: Dict[str, Any]) -> None:
    _NOT_AUTHORIZED.clear()
    _NOT_AUTHORIZED.update({k: v for k, v in detail.items() if k != "day"})


def _env_float(name: str, default: float, *, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


def _max_age_s() -> int:
    try:
        return max(86400, int(os.getenv("MARKET_WIDE_MAX_AGE_S", str(_DEFAULT_MAX_AGE_S))))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_AGE_S


def _store_thresholds() -> Dict[str, float]:
    """Materiality bar for STORAGE. Never a gate -- the scan is unfiltered."""
    return {
        "min_price": _env_float("MARKET_WIDE_MIN_PRICE", 1.0, low=0.0, high=1000.0),
        "min_dollar_volume": _env_float(
            "MARKET_WIDE_MIN_DOLLAR_VOLUME", 1_000_000.0, low=0.0, high=1e12),
        "min_abs_move_pct": _env_float(
            "MARKET_WIDE_MIN_MOVE_PCT", 10.0, low=0.0, high=1000.0),
        "max_rows": _env_float("MARKET_WIDE_MAX_ROWS", 400.0, low=1.0, high=5000.0),
    }


def _market_today(now_ts: Optional[int] = None) -> date:
    """Today in exchange time. A UTC date rolls over five hours early and would
    ask Polygon for a session that has not happened yet."""
    ts = time.time() if now_ts is None else float(now_ts)
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(ts, ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001 - tzdata absent; UTC date is close enough
        return datetime.utcfromtimestamp(ts).date()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def fetch_grouped_day(day: str, *, timeout_s: float = 20.0,
                      current_session: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch one session's bars for every US ticker. Empty list on a holiday.

    `current_session=True` marks a request for TODAY's bar. This account's plan
    refuses a session's grouped bar while it is still the current day and
    serves it once the session is complete -- measured in production on
    2026-09-22: the same key answered 403 for that day at 15:22 ET and 200 with
    12,626 tickers for the day before, six minutes earlier. So a 403 on the
    current session means "not yet", never "your plan lacks this": it is not
    remembered and does not count against the breaker. That misreading is what
    kept this lane disabled from 2026-09-05 onward.
    """
    key = _api_key()
    if not key:
        return [], {"status": "unavailable", "reason": "polygon_api_key_missing", "day": day}
    if _NOT_AUTHORIZED:
        # Checked BEFORE the breaker so the real reason survives.
        return [], {"status": "unavailable", "day": day, **_NOT_AUTHORIZED}
    from core.circuit_breaker import _polygon_cb
    if not _polygon_cb.allow():
        return [], {"status": "unavailable", "reason": "provider_breaker_open", "day": day}
    try:
        # A 429 is the shared key's per-minute allowance, not a verdict on the
        # data. Confirmed live 2026-09-22 19:49 UTC: Ghost's own signal-engine
        # fallbacks spend this key's allowance continuously, so the lane's one
        # grouped-daily call kept landing on "exceeded the maximum requests per
        # minute" and giving up. Wait (Retry-After, else 15s) and try twice more.
        for attempt in range(3):
            response = requests.get(
                _ENDPOINT.format(day=day),
                params={"adjusted": "true", "apiKey": key},
                timeout=max(1.0, min(60.0, float(timeout_s))),
            )
            if response.status_code != 429 or attempt == 2:
                break
            try:
                wait = float(response.headers.get("Retry-After") or 15)
            except (TypeError, ValueError, AttributeError):
                wait = 15.0
            _sleep(min(30.0, max(1.0, wait)))
        # A plan that does not include this endpoint answers 403 with
        # NOT_AUTHORIZED, and an expired key answers 401. Both are permanent,
        # and both look exactly like a flaky provider once the breaker has
        # swallowed them -- so name them instead of retrying forever.
        if response.status_code in (401, 403) and current_session and response.status_code == 403:
            LOGGER.info("grouped daily for the current session %s is not entitled yet; "
                        "using completed sessions", day)
            return [], {"status": "not_yet_entitled", "reason": "current_session_not_entitled",
                        "http_status": 403, "day": day, "permanent": False}
        if response.status_code in (401, 403):
            _polygon_cb.record_failure()
            LOGGER.error(
                "MARKET-WIDE DISABLED — Polygon returned %d for grouped-daily. "
                "The key is rejected or the plan does not include this endpoint; "
                "this will not recover on retry.", response.status_code)
            detail = {"status": "unavailable", "reason": "provider_not_authorized",
                        "http_status": response.status_code, "day": day,
                        "permanent": True}
            _remember_not_authorized(detail)
            return [], detail
        response.raise_for_status()
        payload = response.json()
        provider_status = str(payload.get("status") or "").upper()
        if provider_status in {"NOT_AUTHORIZED", "ERROR"}:
            _polygon_cb.record_failure()
            LOGGER.error("MARKET-WIDE DISABLED — Polygon body status=%s day=%s",
                         provider_status, day)
            detail = {"status": "unavailable", "reason": "provider_not_authorized",
                        "provider_status": provider_status, "day": day,
                        "permanent": True}
            _remember_not_authorized(detail)
            return [], detail
        results = payload.get("results")
        rows = [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []
        _polygon_cb.record_success()
        return rows, {"status": "available", "day": day, "rows": len(rows),
                      "trading_day": bool(rows)}
    except Exception as exc:  # noqa: BLE001 - advisory lane, degrade don't raise
        _polygon_cb.record_failure()
        LOGGER.warning("grouped daily unavailable day=%s type=%s", day, type(exc).__name__)
        return [], {"status": "unavailable", "reason": "provider_request_failed", "day": day}


def _recent_trading_days(
    *,
    needed: int = 2,
    lookback_days: int = 8,
    now_ts: Optional[int] = None,
    fetcher=None,
) -> Tuple[List[Tuple[str, List[Dict[str, Any]]]], List[Dict[str, Any]]]:
    """Walk back from today until `needed` sessions with bars are found.

    Weekends and holidays return an empty result set rather than an error, so
    the only way to know a day traded is to ask. The lookback is bounded so a
    dead provider costs a fixed number of requests, not an unbounded walk.
    """
    fetch = fetcher or fetch_grouped_day
    today = _market_today(now_ts)
    found: List[Tuple[str, List[Dict[str, Any]]]] = []
    statuses: List[Dict[str, Any]] = []
    for offset in range(0, max(1, int(lookback_days))):
        if len(found) >= needed:
            break
        day = (today - timedelta(days=offset)).isoformat()
        if offset == 0:
            try:
                rows, status = fetch(day, current_session=True)
            except TypeError:           # an injected fetcher without the flag
                rows, status = fetch(day)
        else:
            rows, status = fetch(day)
        statuses.append(status)
        if status.get("status") == "not_yet_entitled":
            continue                    # today is not served yet; completed sessions are
        if status.get("status") != "available":
            # A hard provider failure is not a holiday; stop rather than
            # marching backwards through a week of the same error.
            break
        if rows:
            found.append((day, rows))
    return found, statuses


# Split-artifact guard (audit U03). On 2026-09-25 the lane stored BCPC +606.69%
# and TPC +399.82% as fresh movers: close/prior ratios of 7.07 and 5.00 -- whole
# numbers, the signature of a share-count change (split or consolidation) that
# the adjusted grouped bar did not carry through to the prior session. Three
# things together mark such a row, and a real move shows at most two of them:
#   * close/prior within _SPLIT_RATIO_TOL of a whole multiple n >= 2 (or 1/n);
#   * the session OPENED at that multiple (a split re-bases the whole session;
#     a genuine runner opens near the prior close and gets there intraday);
#   * no dollar-volume surge: a re-based share count trades about the same
#     dollars as the day before, while a real +100%..+600% move or a halving
#     trades many times the prior day's dollars.
# Such a row is not a move of unknown size but a move of unknown EXISTENCE, so
# it is excluded from the stored movers and named in the result instead.
_SPLIT_RATIO_TOL = 0.015        # |ratio - n| / n for a whole-number n >= 2
_SPLIT_OPEN_TOL = 0.20          # the session's open sits at the same multiple
_SPLIT_MIN_RATIO = 1.9          # a move under ~+90% (or ~-47%) is never checked
_SPLIT_MAX_DOLLAR_VOLUME_RATIO = 1.5   # latest $ volume / prior $ volume


def split_artifact_factor(close: float, prior_close: float,
                          open_price: Any = None, *,
                          volume: Any = None, prior_volume: Any = None) -> Optional[int]:
    """Whole-number split factor that explains close/prior_close, else None.

    Checks both directions: n-for-1 consolidations (ratio ~ n) and 1-for-n
    forward splits (ratio ~ 1/n). The open and volume tests apply when those
    inputs are known; each can only clear a row, never flag one on its own.
    """
    try:
        c, p = float(close), float(prior_close)
    except (TypeError, ValueError):
        return None
    if c <= 0 or p <= 0:
        return None
    try:
        o = float(open_price) if open_price is not None else None
    except (TypeError, ValueError):
        o = None
    try:
        v = float(volume) if volume is not None else None
        pv = float(prior_volume) if prior_volume is not None else None
    except (TypeError, ValueError):
        v = pv = None
    if v is not None and pv is not None and v > 0 and pv > 0:
        if (c * v) / (p * pv) > _SPLIT_MAX_DOLLAR_VOLUME_RATIO:
            return None                 # real money moved: a real move
    for up in (True, False):
        r = c / p if up else p / c
        if r < _SPLIT_MIN_RATIO:
            continue
        n = int(round(r))
        if n < 2 or abs(r - n) / n > _SPLIT_RATIO_TOL:
            continue
        if o is not None and o > 0:
            open_ratio = o / p if up else p / o
            if abs(open_ratio - n) / n > _SPLIT_OPEN_TOL:
                continue
        return n
    return None


def build_market_wide_rows(
    latest: List[Dict[str, Any]],
    prior: List[Dict[str, Any]],
    *,
    latest_day: str,
    received_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """Close-to-close move for every ticker present in both sessions.

    Returns the full scan alongside the subset worth storing, so a caller can
    report coverage and materiality as two separate numbers.
    """
    now = int(received_ts or time.time())
    thresholds = _store_thresholds()
    prior_close: Dict[str, float] = {}
    prior_volume: Dict[str, float] = {}
    for bar in prior:
        ticker = str(bar.get("T") or "").strip().upper()
        try:
            close = float(bar.get("c"))
        except (TypeError, ValueError):
            continue
        if ticker and close > 0:
            prior_close[ticker] = close
            try:
                prior_volume[ticker] = float(bar.get("v") or 0.0)
            except (TypeError, ValueError):
                pass

    scanned = 0
    dropped = {"no_prior_close": 0, "bad_bar": 0, "below_price": 0,
               "below_dollar_volume": 0, "below_move": 0,
               "suspected_split": 0}
    suspected_splits: List[Dict[str, Any]] = []
    candidates: List[Tuple[float, Dict[str, Any]]] = []
    largest: Optional[float] = None

    for bar in latest:
        ticker = str(bar.get("T") or "").strip().upper()
        if not ticker:
            dropped["bad_bar"] += 1
            continue
        scanned += 1
        try:
            close = float(bar.get("c"))
            volume = float(bar.get("v") or 0.0)
        except (TypeError, ValueError):
            dropped["bad_bar"] += 1
            continue
        if close <= 0:
            dropped["bad_bar"] += 1
            continue
        base = prior_close.get(ticker)
        if base is None:
            # A new listing has no prior close. An intraday move is available
            # here and is deliberately not used: it is a different measurement.
            dropped["no_prior_close"] += 1
            continue
        move_pct = (close - base) / base * 100.0
        factor = split_artifact_factor(
            close, base, bar.get("o"), volume=volume, prior_volume=prior_volume.get(ticker),
        )
        # A suspected split is not the market's largest move either.
        if factor is None and (largest is None or abs(move_pct) > abs(largest)):
            largest = move_pct
        if close < thresholds["min_price"]:
            dropped["below_price"] += 1
            continue
        if close * volume < thresholds["min_dollar_volume"]:
            dropped["below_dollar_volume"] += 1
            continue
        if abs(move_pct) < thresholds["min_abs_move_pct"]:
            dropped["below_move"] += 1
            continue
        if factor is not None:
            # Its size is an artifact of the share count: counted and named,
            # never stored as a mover.
            dropped["suspected_split"] += 1
            suspected_splits.append({
                "ticker": ticker, "close": close, "prior_close": base,
                "raw_move_pct": round(move_pct, 2), "factor": factor,
            })
            continue
        candidates.append((abs(move_pct), {
            "ticker": ticker, "close": close, "volume": volume,
            "prior_close": base, "move_pct": move_pct, "bar": bar,
        }))

    candidates.sort(key=lambda item: -item[0])
    kept = [item for _, item in candidates[: int(thresholds["max_rows"])]]

    rows = []
    for rank, item in enumerate(kept, start=1):
        bar = item["bar"]
        try:
            source_ts = int(int(bar.get("t")) // 1000)
        except (TypeError, ValueError):
            source_ts = None
        rows.append(normalize_external_observation(
            provider=PROVIDER,
            provider_family=PROVIDER_FAMILY,
            screen=SCREEN,
            raw_symbol=item["ticker"],
            source_ts=source_ts,
            received_ts=now,
            observation_id=f"{SCREEN}:{latest_day}:{item['ticker']}",
            rank=rank,
            price=item["close"],
            move_pct=round(item["move_pct"], 4),
            volume=item["volume"],
            avg_volume=None,
            external_score=round(item["move_pct"], 4),
            delayed=True,
            payload={
                "T": item["ticker"], "day": latest_day,
                "c": item["close"], "v": item["volume"],
                "prior_close": item["prior_close"],
                "move_basis": "close_to_close",
                "o": bar.get("o"), "h": bar.get("h"), "l": bar.get("l"),
                "n": bar.get("n"), "vw": bar.get("vw"),
            },
            max_age_s=_max_age_s(),
        ))

    return {
        "rows": rows,
        "scanned": scanned,
        "with_prior_close": scanned - dropped["no_prior_close"],
        "eligible": len(candidates),
        "stored_candidates": len(kept),
        "truncated": len(candidates) - len(kept),
        "dropped": dropped,
        "max_abs_move_seen_pct": None if largest is None else round(largest, 2),
        "suspected_splits": sorted(
            suspected_splits, key=lambda r: -abs(r["raw_move_pct"]))[:25],
        "thresholds": thresholds,
        "move_basis": "close_to_close",
    }


def _store_rows(rows: List[Dict[str, Any]]) -> Tuple[int, int]:
    """Persist valid rows over ONE connection.

    store_external_observation opens its own connection when no cursor is
    passed, so the obvious loop would open several hundred of them in a single
    cycle and exhaust the pool. Invalid rows are counted, not written.
    """
    valid = [row for row in rows if row.get("validation_valid")]
    invalid = len(rows) - len(valid)
    if not valid:
        return 0, invalid
    inserted = 0
    from core.db import db_conn
    with db_conn() as conn:
        cur = conn.cursor()
        for row in valid:
            try:
                if store_external_observation(row, cur=cur):
                    inserted += 1
            except Exception as exc:  # noqa: BLE001 - one bad row must not end the scan
                LOGGER.warning("market-wide row not stored symbol=%s type=%s",
                               row.get("symbol"), type(exc).__name__)
    return inserted, invalid


def run_market_wide_cycle(*, now_ts: Optional[int] = None, fetcher=None) -> Dict[str, Any]:
    """Daily full-market scan. Advisory writes only; safe partial failure."""
    started = int(now_ts or time.time())
    base: Dict[str, Any] = {
        "provider": PROVIDER, "screen": SCREEN, "computed_at": started,
        "advisory_only": True, "decision_eligible": False,
        "note": ("Full-market discovery scan. Every US ticker is looked at; "
                 "rows are stored above a materiality bar. Nothing here is "
                 "trade-eligible or enters the modelled universe."),
    }
    if not _enabled():
        return {**base, "ok": True, "status": "disabled", "scanned": 0, "inserted": 0}
    if not _api_key():
        LOGGER.warning("market-wide scan skipped: POLYGON_API_KEY not set")
        return {**base, "ok": False, "status": "unavailable",
                "reason": "polygon_api_key_missing", "scanned": 0, "inserted": 0}

    sessions, statuses = _recent_trading_days(now_ts=started, fetcher=fetcher)
    if len(sessions) < 2:
        # Fewer than two sessions means no close-to-close basis exists. Report
        # it rather than falling back to an intraday move of a different kind.
        permanent = [st for st in statuses if st.get("permanent")]
        return {**base, "ok": False, "status": "unavailable",
                "reason": (permanent[0].get("reason") if permanent
                           else "insufficient_sessions"),
                # Surfaced at the top level so "the key is rejected" is not
                # left buried under a generic no-data result.
                "permanent_failure": bool(permanent),
                "sessions_found": len(sessions),
                "provider_statuses": statuses, "scanned": 0, "inserted": 0}

    (latest_day, latest_rows), (prior_day, prior_rows) = sessions[0], sessions[1]
    built = build_market_wide_rows(
        latest_rows, prior_rows, latest_day=latest_day, received_ts=started)

    inserted, invalid = _store_rows(built["rows"])

    result = {
        **base, "ok": True, "status": "complete",
        "latest_day": latest_day, "prior_day": prior_day,
        "scanned": built["scanned"], "with_prior_close": built["with_prior_close"],
        "eligible": built["eligible"], "inserted": inserted, "invalid": invalid,
        "truncated": built["truncated"], "dropped": built["dropped"],
        "max_abs_move_seen_pct": built["max_abs_move_seen_pct"],
        "suspected_splits": built["suspected_splits"],
        "thresholds": built["thresholds"], "move_basis": built["move_basis"],
        "provider_statuses": statuses,
    }
    LOGGER.info(
        "MARKET-WIDE scan day=%s scanned=%d eligible=%d stored=%d max_move=%s%%",
        latest_day, built["scanned"], built["eligible"], inserted,
        built["max_abs_move_seen_pct"],
    )
    return result
