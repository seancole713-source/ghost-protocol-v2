"""Truthful >=5% official Ghost forecast feed for the consumer UI.

This module deliberately reads only immutable, issued prediction geometry. It
must never promote external discovery, squeeze observations, research picks, or
a later price decline into a new >=5% forecast claim.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterable, Mapping, Sequence

from config.symbols import OFFICIAL_WATCHLIST
from core.prediction_filters import NON_RESEARCH_WHERE

MIN_FORECAST_GAIN_PCT = 5.0
MIN_FORECAST_HORIZON_S = 24 * 60 * 60
MAX_FORECAST_HORIZON_S = 14 * 24 * 60 * 60
MAX_ROWS = len(OFFICIAL_WATCHLIST)


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0 or number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _query_rows(cursor: Any, now_ts: int, min_gain_pct: float) -> list[dict[str, Any]]:
    """Read active official UP forecasts with immutable issued gain >= floor."""
    cursor.execute(
        """
        SELECT id, symbol, entry_price, target_price, predicted_at, expires_at
        FROM predictions
        WHERE outcome IS NULL
          AND direction IN ('UP', 'BUY')
          AND entry_price IS NOT NULL AND entry_price > 0
          AND target_price IS NOT NULL AND target_price > entry_price
          AND predicted_at IS NOT NULL
          AND expires_at IS NOT NULL AND expires_at > %s
          AND expires_at > predicted_at
          AND expires_at - predicted_at >= %s
          AND expires_at - predicted_at <= %s
          AND COALESCE(asset_type, 'stock') = 'stock'
          AND symbol = ANY(%s)
          AND """ + NON_RESEARCH_WHERE + """
          AND ((target_price / entry_price) - 1.0) * 100.0 >= %s
        ORDER BY ((target_price / entry_price) - 1.0) DESC,
                 predicted_at DESC, id DESC
        LIMIT %s
        """,
        (
            now_ts,
            MIN_FORECAST_HORIZON_S,
            MAX_FORECAST_HORIZON_S,
            list(OFFICIAL_WATCHLIST),
            min_gain_pct,
            MAX_ROWS,
        ),
    )
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _build_items(
    rows: Iterable[Mapping[str, Any]],
    sessions: Mapping[str, Any],
    min_gain_pct: float,
) -> list[dict[str, Any]]:
    official = set(OFFICIAL_WATCHLIST)
    items: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        entry = _finite_positive(row.get("entry_price"))
        target = _finite_positive(row.get("target_price"))
        try:
            predicted_at = int(row.get("predicted_at") or 0)
            expires_at = int(row.get("expires_at") or 0)
        except (TypeError, ValueError):
            continue
        if symbol not in official or entry is None or target is None or target <= entry:
            continue
        if predicted_at <= 0 or expires_at <= predicted_at:
            continue
        horizon_s = expires_at - predicted_at
        if horizon_s < MIN_FORECAST_HORIZON_S or horizon_s > MAX_FORECAST_HORIZON_S:
            continue
        forecast_gain_pct = (target / entry - 1.0) * 100.0
        if forecast_gain_pct + 1e-9 < min_gain_pct:
            continue

        session = sessions.get(symbol) if isinstance(sessions, Mapping) else None
        session = session if isinstance(session, Mapping) else {}
        current_price = _finite_positive(session.get("price")) if session.get("ok") else None
        items.append({
            "prediction_id": row.get("id"),
            "symbol": symbol,
            "current_price": round(current_price, 6) if current_price is not None else None,
            "current_price_state": str(session.get("provider_state") or "unavailable"),
            "current_price_source": session.get("price_source"),
            "current_price_freshness_seconds": session.get("freshness_seconds"),
            "issued_entry_price": round(entry, 6),
            "forecast_target_price": round(target, 6),
            "forecast_gain_pct": round(forecast_gain_pct, 2),
            "predicted_at": predicted_at,
            "target_window_ends_at": expires_at,
            "official_live_prediction": True,
            "research_pick": False,
        })
    items.sort(key=lambda item: (-item["forecast_gain_pct"], -item["predicted_at"], str(item["symbol"])))
    return items


def big_movers_snapshot(
    min_gain_pct: float = MIN_FORECAST_GAIN_PCT,
    *,
    now_ts: int | None = None,
    db_conn_factory: Callable[[], Any] | None = None,
    session_loader: Callable[[Sequence[str]], Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Return active official forecasts at or above the >=5% contract floor."""
    try:
        requested_floor = float(min_gain_pct)
    except (TypeError, ValueError):
        requested_floor = MIN_FORECAST_GAIN_PCT
    floor = max(MIN_FORECAST_GAIN_PCT, requested_floor)
    now = int(time.time()) if now_ts is None else int(now_ts)

    if db_conn_factory is None:
        from core.db import db_conn
        db_conn_factory = db_conn
    with db_conn_factory() as connection:
        rows = _query_rows(connection.cursor(), now, floor)

    # Re-validate database output before any provider access. This prevents a
    # malformed/poison row from expanding the bounded quote request outside the
    # official forecast contract even if the SQL layer is later changed.
    eligible = _build_items(rows, {}, floor)
    symbols = list(dict.fromkeys(str(item["symbol"]) for item in eligible))
    if session_loader is None:
        from core.market_sessions import get_market_sessions

        def session_loader(requested: Sequence[str]) -> Mapping[str, Any]:
            return get_market_sessions(list(requested), max_fresh=min(8, len(requested)))

    session_snapshot = session_loader(symbols) if symbols else {"sessions": {}, "as_of_ts": now}
    sessions = session_snapshot.get("sessions", {}) if isinstance(session_snapshot, Mapping) else {}
    items = _build_items(rows, sessions if isinstance(sessions, Mapping) else {}, floor)
    return {
        "ok": True,
        "status": "active" if items else "empty",
        "items": items,
        "count": len(items),
        "as_of_ts": int(session_snapshot.get("as_of_ts") or now) if isinstance(session_snapshot, Mapping) else now,
        "min_forecast_gain_pct": floor,
        "gain_basis": "issued_target_vs_issued_entry",
        "scope": "official_watchlist",
        "universe_size": len(OFFICIAL_WATCHLIST),
        "market_wide": False,
        "live_refresh_seconds": 60,
        "max_forecast_horizon_days": 14,
        "date_semantics": "target_window_deadline_not_exact_hit_date",
        "empty_reason": (
            "No active official Ghost forecast currently has an issued gain of at least "
            f"{floor:g}%."
        ) if not items else None,
        "scope_note": (
            "Ghost forecasts only its official watchlist; external and squeeze observations "
            "are not represented as predictions."
        ),
    }
