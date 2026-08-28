"""Dynamic market observations for externally discovered symbols.

This lane enriches a small, fresh set of quarantined screener rows with factual
Alpaca bar metrics. It is structurally isolated from Ghost candidates, alerts,
outcomes, predictions, and wallets.
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Any, Dict, Iterable, List, Optional

LOGGER = logging.getLogger("ghost.external_radar")
_ALLOWED_SCREENS = frozenset({"day_gainers", "most_shorted_stocks"})


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def select_external_radar_seeds(
    observations: Iterable[Dict[str, Any]],
    *,
    now_ts: Optional[int] = None,
    max_age_s: Optional[int] = None,
    per_screen_cap: Optional[int] = None,
    total_cap: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Select fresh nonofficial symbols deterministically and preserve provenance."""
    now = int(time.time()) if now_ts is None else int(now_ts)
    max_age = max(300, int(max_age_s or os.getenv("EXTERNAL_RADAR_MAX_AGE_S", "1800")))
    per_cap = max(1, min(25, int(per_screen_cap or os.getenv("EXTERNAL_RADAR_PER_SCREEN_CAP", "10"))))
    cap = max(1, min(50, int(total_cap or os.getenv("EXTERNAL_RADAR_TOTAL_CAP", "20"))))
    min_price = max(0.01, float(os.getenv("EXTERNAL_RADAR_MIN_PRICE", "1")))
    max_price = max(min_price, float(os.getenv("EXTERNAL_RADAR_MAX_PRICE", "1000")))
    min_avg_volume = max(0.0, float(os.getenv("EXTERNAL_RADAR_MIN_AVG_VOLUME", "200000")))

    # The DB query is newest-first. Keep only the latest observation for each
    # symbol/screen before ranking, so repeated scheduler rows cannot consume
    # the bounded scan budget.
    latest: Dict[tuple[str, str], Dict[str, Any]] = {}
    for raw in observations:
        row = dict(raw)
        symbol = str(row.get("symbol") or "").strip().upper()
        screen = str(row.get("screen") or "").strip().lower()
        if not symbol or screen not in _ALLOWED_SCREENS:
            continue
        key = (symbol, screen)
        source_ts = row.get("source_ts")
        try:
            observed = int(source_ts)
        except (TypeError, ValueError, OverflowError):
            continue
        age = now - observed
        price = _finite(row.get("price"))
        volume = _finite(row.get("volume"))
        avg_volume = _finite(row.get("avg_volume"))
        if (
            age < 0 or age > max_age
            or row.get("validation_valid") is not True
            or row.get("quarantined") is not True
            or row.get("in_official_watchlist") is True
            or price is None or not min_price <= price <= max_price
            or volume is None or volume < 0
            or avg_volume is None or avg_volume < min_avg_volume
        ):
            continue
        row.update({
            "symbol": symbol,
            "screen": screen,
            "source_ts": observed,
            "source_age_s": age,
            "price": price,
            "volume": volume,
            "avg_volume": avg_volume,
        })
        previous = latest.get(key)
        if previous is None or (observed, int(row.get("received_ts") or 0)) > (
            int(previous.get("source_ts") or 0), int(previous.get("received_ts") or 0)
        ):
            latest[key] = row

    ranked = sorted(
        latest.values(),
        key=lambda row: (
            int(row.get("source_age_s") or 0),
            int(row.get("rank") or 2**31 - 1),
            -float(row.get("avg_volume") or 0.0),
            str(row.get("symbol") or ""),
            str(row.get("screen") or ""),
        ),
    )
    selected: Dict[str, Dict[str, Any]] = {}
    screen_counts: Dict[str, int] = {}
    for row in ranked:
        symbol = row["symbol"]
        screen = row["screen"]
        if screen_counts.get(screen, 0) >= per_cap:
            continue
        if symbol not in selected and len(selected) >= cap:
            continue
        origin = {
            "provider": row.get("provider"),
            "screen": screen,
            "observation_id": row.get("observation_id"),
            "source_ts": row.get("source_ts"),
            "received_ts": row.get("received_ts"),
            "rank": row.get("rank"),
            "discovery_price": row.get("price"),
            "discovery_move_pct": row.get("move_pct"),
        }
        if symbol not in selected:
            selected[symbol] = {
                "symbol": symbol,
                "first_seen_ts": row.get("first_seen_ts") or row.get("received_ts"),
                "origins": [origin],
            }
        else:
            selected[symbol]["origins"].append(origin)
            first_seen = row.get("first_seen_ts") or row.get("received_ts")
            if first_seen:
                selected[symbol]["first_seen_ts"] = min(
                    int(selected[symbol].get("first_seen_ts") or first_seen), int(first_seen)
                )
        screen_counts[screen] = screen_counts.get(screen, 0) + 1
    return list(selected.values())


def load_external_radar_seeds(*, now_ts: Optional[int] = None) -> List[Dict[str, Any]]:
    """Read a bounded recent ledger window; never calls an external provider."""
    from core.db import db_conn

    now = int(time.time()) if now_ts is None else int(now_ts)
    max_age = max(300, int(os.getenv("EXTERNAL_RADAR_MAX_AGE_S", "1800")))
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT provider, screen, symbol, source_ts, received_ts, rank,
                      price, move_pct, volume, avg_volume, observation_id,
                      validation_valid, quarantined, in_official_watchlist,
                      MIN(received_ts) OVER (PARTITION BY symbol) AS first_seen_ts
               FROM ghost_external_observations
               WHERE received_ts >= %s
                 AND screen IN ('day_gainers', 'most_shorted_stocks')
               ORDER BY COALESCE(source_ts, received_ts) DESC, received_ts DESC
               LIMIT 500""",
            (now - max_age * 2,),
        )
        rows = cur.fetchall()
    observations = [
        {
            "provider": row[0], "screen": row[1], "symbol": row[2],
            "source_ts": row[3], "received_ts": row[4], "rank": row[5],
            "price": row[6], "move_pct": row[7], "volume": row[8],
            "avg_volume": row[9], "observation_id": row[10],
            "validation_valid": bool(row[11]), "quarantined": bool(row[12]),
            "in_official_watchlist": bool(row[13]), "first_seen_ts": row[14],
        }
        for row in rows
    ]
    return select_external_radar_seeds(observations, now_ts=now, max_age_s=max_age)


def _observation_row(seed: Dict[str, Any], metrics: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "symbol": seed["symbol"],
        "first_seen_ts": seed.get("first_seen_ts"),
        "origins": list(seed.get("origins") or []),
        "market_provider": "alpaca_batch_bar",
        "market_status": "unavailable",
        "missing_reason": "missing_batch_market_data",
        "advisory_only": True,
        "decision_eligible": False,
    }
    if not metrics:
        return row
    price = _finite(metrics.get("price"))
    prior_close = _finite(metrics.get("prior_close"))
    session_high = _finite(metrics.get("session_high"))
    session_volume = _finite(metrics.get("session_volume"))
    avg_daily_volume = _finite(metrics.get("avg_daily_volume"))
    if (
        price is None or price <= 0 or prior_close is None or prior_close <= 0
        or session_high is None or session_high <= 0
        or session_volume is None or session_volume < 0
        or avg_daily_volume is None or avg_daily_volume <= 0
    ):
        row["missing_reason"] = "invalid_batch_market_data"
        return row
    from core.market_hours import is_us_premarket
    from core.squeeze_monitor import compute_rvol, rth_elapsed_fraction

    rvol = compute_rvol(
        session_volume, avg_daily_volume, rth_elapsed_fraction(),
        premarket=is_us_premarket(),
    )
    row.update({
        "market_status": "available",
        "missing_reason": None,
        "market_data_as_of": metrics.get("price_as_of_ts"),
        "observed_price": round(price, 4),
        "prior_close": round(prior_close, 4),
        "session_high": round(session_high, 4),
        "observed_current_move_pct": round((price - prior_close) / prior_close * 100.0, 4),
        "observed_peak_move_pct": round((session_high - prior_close) / prior_close * 100.0, 4),
        "session_volume": round(session_volume, 4),
        "avg_daily_volume": round(avg_daily_volume, 4),
        "observed_rvol": round(rvol, 4),
    })
    return row


def run_external_radar_cycle(*, now_ts: Optional[int] = None) -> Dict[str, Any]:
    """Enrich selected discoveries once via batch-only bars and persist a snapshot."""
    now = int(time.time()) if now_ts is None else int(now_ts)
    if os.getenv("EXTERNAL_RADAR_ENABLED", "1").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"ok": True, "status": "disabled", "advisory_only": True,
                "decision_eligible": False}
    seeds = load_external_radar_seeds(now_ts=now)
    metrics_map: Dict[str, Optional[Dict[str, Any]]] = {}
    batch_failed = False
    if seeds:
        try:
            from core.squeeze_monitor import batched_market_metrics
            metrics_map = batched_market_metrics([seed["symbol"] for seed in seeds])
        except Exception as exc:
            batch_failed = True
            LOGGER.warning("external radar batch unavailable type=%s", type(exc).__name__)
    rows = [_observation_row(seed, metrics_map.get(seed["symbol"])) for seed in seeds]
    observed = sum(row["market_status"] == "available" for row in rows)
    if not seeds:
        status = "empty"
    elif observed == len(seeds):
        status = "complete"
    elif observed:
        status = "partial"
    else:
        status = "unavailable"
    run = {
        "run_id": f"external-radar:{now}",
        "observed_at": now,
        "status": status,
        "selected_count": len(seeds),
        "observed_count": observed,
        "missing_count": len(seeds) - observed,
        "batch_failed": batch_failed,
        "note": (
            "Externally discovered market activity observed after provider discovery. "
            "Advisory only; not a prediction, candidate, trade recommendation, or "
            "evidence that Ghost identified the move before it began."
        ),
    }
    from core.external_context_ledger import store_external_radar_snapshot
    store_external_radar_snapshot(run, rows)
    return {**run, "items": rows, "ok": status in {"empty", "complete", "partial"},
            "advisory_only": True, "decision_eligible": False}
