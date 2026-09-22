"""Read-only market discovery from the persisted advisory ledger.

Intraday movers use the latest provider observation, not the largest historical
move. Daily reference bars are exposed separately. Discovery never expands the
modelled universe, creates a prediction, grants trade eligibility or sends an
external notification; the scheduled surface below writes server logs only.

Moves are also labelled against corporate actions. A stock on its ex-dividend
date steps down by roughly the cash it detached, measured against an unadjusted
previous close -- a >5% "crash" on a high-yield name where nothing happened.
Such a row is never deleted; it is moved to `corporate_action_alerts`, keeps its
observed `move_pct`, and gains an `economic_move_pct` that is what a holder
actually earned. See core/corporate_actions.py.
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Any, Dict, Optional

LOGGER = logging.getLogger("ghost.discovery")

ALERT_VERSION = "discovery_alerts_v3"


def _min_move_pct() -> float:
    """Absolute move that makes a symbol worth a human's attention.

    5%, not 10%, on the operator's instruction (2026-09-08): a 5% move is worth
    knowing about, and a discovery lane that only reports doubles is not
    watching the market. This is a DISPLAY threshold on an advisory lane -- it
    controls how much Ghost reports, never what Ghost claims. Nothing here is
    trade-eligible, so widening it loosens no proof and no gate.
    """
    try:
        return max(1.0, float(os.getenv("DISCOVERY_ALERT_MIN_MOVE_PCT", "5")))
    except Exception:
        return 5.0


def _max_age_s() -> int:
    """Overall display horizon; each provider also expires at its own read TTL.

    Daily reference bars are returned separately from current intraday movers.
    """
    try:
        return max(300, int(os.getenv("DISCOVERY_ALERT_MAX_AGE_S", "259200")))
    except Exception:
        return 259200


def _max_alerts() -> int:
    """Output cap on current movers, independent of upstream selection budgets."""
    try:
        return max(1, min(500, int(os.getenv("DISCOVERY_ALERT_MAX", "200"))))
    except Exception:
        return 200


def _per_screen() -> int:
    try:
        return max(1, min(200, int(os.getenv("DISCOVERY_ALERT_PER_SCREEN", "60"))))
    except Exception:
        return 60


def _integer(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return -1


def _observation_order(item: Dict[str, Any]) -> tuple:
    # Never use move magnitude as a tie breaker: that selects winners in hindsight.
    return (_integer(item.get("received_ts")), _integer(item.get("source_ts")),
            _integer(item.get("observation_id")), str(item.get("provider") or ""),
            str(item.get("screen") or ""))


def build_discovery_alerts(limit: int = 240) -> Dict[str, Any]:
    """Current advisory movers, with daily history in a separate labelled list.

    Read-only. These are observations, not forecasts or trade-eligible picks.
    """
    from core.corporate_actions import annotate_move
    from core.external_context_ledger import (
        observation_max_age_s,
        recent_external_discoveries,
    )

    started = int(time.time())
    min_move, max_age = _min_move_pct(), _max_age_s()
    out: Dict[str, Any] = {
        "alert_version": ALERT_VERSION, "computed_at": started,
        "min_move_pct": min_move, "max_age_s": max_age,
        "intraday_max_age_s": min(max_age, observation_max_age_s("yahoo_saved_screener")),
        "advisory_only": True, "decision_eligible": False,
        "selection": "latest_observation_not_largest_historical_move",
        "note": (
            "Observed movers only, not advance predictions or trade recommendations. "
            "Intraday observations and daily history are separate. Presence in the "
            "watchlist does not establish a trained, approved or accurate model."
        ),
        "alerts": [], "historical_alerts": [], "corporate_action_alerts": [],
        "corporate_action_note": (
            "A row here cleared the display threshold on its observed move but not "
            "on its total return: the decline was the dividend it detached, not a "
            "selloff. Listed, never deleted."
        ),
    }
    try:
        snapshot = recent_external_discoveries(limit=limit, per_screen=_per_screen())
    except Exception as exc:  # noqa: BLE001 - advisory surface, never a gate
        out["error"] = str(exc)[:160]
        return out

    # Choose the latest update BEFORE validity/threshold checks. Otherwise a
    # fade below 5%, missing move or invalid quote revives an older large gain.
    latest: Dict[tuple, Dict[str, Any]] = {}
    for item in snapshot.get("items") or []:
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        daily = item.get("provider") == "polygon_grouped_daily"
        key = (symbol, daily)
        if key not in latest or _observation_order(item) > _observation_order(latest[key]):
            latest[key] = item

    largest: Optional[float] = None
    dropped = {"no_move": 0, "stale": 0, "invalid": 0, "below_threshold": 0,
               "explained_by_corporate_action": 0}
    coverage_counts: Dict[str, int] = {}
    current, historical, corporate = [], [], []
    for (symbol, daily), item in latest.items():
        # In this ledger quarantine means outside the official watchlist, not
        # invalid evidence. It must NOT exclude the names discovery exists for.
        if item.get("validation_valid") is not True:
            dropped["invalid"] += 1
            continue
        observed = _integer(item.get("source_ts"))
        if observed <= 0 or observed > started:
            dropped["invalid"] += 1
            continue
        age = started - observed
        ttl = min(max_age, observation_max_age_s(str(item.get("provider") or "")))
        if age > ttl:
            dropped["stale"] += 1
            continue
        try:
            move = float(item.get("move_pct"))
        except (TypeError, ValueError, OverflowError):
            move = float("nan")
        if not math.isfinite(move):
            dropped["no_move"] += 1
            continue
        if not daily and (largest is None or abs(move) > abs(largest)):
            largest = move

        # Threshold on the OBSERVED move, exactly as before: this lane reports
        # price moves, and a 3% gain beside a 4% dividend is a 7% total return
        # that no intraday trader could have taken. The corporate action decides
        # only whether a qualifying decline was real -- never whether a
        # non-mover gets promoted into the list.
        if abs(move) < min_move:
            dropped["below_threshold"] += 1
            continue

        annotation = annotate_move(
            symbol, move_pct=move, price=item.get("price"), observed_ts=observed,
        )
        coverage = str(annotation.get("corporate_action_coverage") or "unavailable")
        coverage_counts[coverage] = coverage_counts.get(coverage, 0) + 1
        economic = annotation.get("economic_move_pct")
        # Rank on the total-return move when it is KNOWN, on the observed move
        # when it is not. An unavailable dividend feed must never read as "no
        # dividend" -- that would quietly re-create the bug this lane just fixed.
        ranked_move = economic if isinstance(economic, (int, float)) else move

        alert = {
            "symbol": symbol, "move_pct": round(move, 2), "price": item.get("price"),
            "volume": item.get("volume"), "avg_volume": item.get("avg_volume"),
            "screen": item.get("screen"), "provider": item.get("provider"),
            "source_ts": observed, "received_ts": item.get("received_ts"),
            "observation_id": item.get("observation_id"), "source_age_s": age,
            "move_basis": item.get("move_basis"),
            "economic_move_pct": (
                round(float(economic), 2) if isinstance(economic, (int, float)) else None
            ),
            "mechanical_move_pct": annotation.get("mechanical_move_pct"),
            "corporate_action": annotation.get("corporate_action"),
            "corporate_action_coverage": coverage,
            "observation_kind": "daily_history" if daily else "intraday_observation",
            "freshness": "fresh", "delayed": bool(item.get("delayed")),
            "in_watchlist": bool(item.get("in_official_watchlist")),
            # Kept for API compatibility, but this is scope, not model readiness.
            "ghost_can_model_it": bool(item.get("in_official_watchlist")),
            "model_readiness": "not_evaluated_by_discovery",
            "advisory_only": True, "decision_eligible": False,
        }
        # The observed move clears the bar but the economic one does not: the
        # dividend explained it. Not a market event -- and not hidden either.
        if annotation.get("corporate_action") and abs(ranked_move) < min_move:
            alert["reclassified"] = "move_explained_by_corporate_action"
            corporate.append(alert)
            dropped["explained_by_corporate_action"] += 1
            continue
        (historical if daily else current).append(alert)

    def _rank(alert: Dict[str, Any]) -> tuple:
        economic = alert.get("economic_move_pct")
        primary = economic if isinstance(economic, (int, float)) else alert["move_pct"]
        return (-abs(primary), alert["symbol"])

    current.sort(key=_rank)
    historical.sort(key=_rank)
    corporate.sort(key=_rank)
    alerts, history = current[:_max_alerts()], historical[:_max_alerts()]
    out.update({
        "alerts": alerts, "alert_count": len(alerts), "qualifying_count": len(current),
        "truncated": max(0, len(current) - len(alerts)),
        "historical_alerts": history, "historical_alert_count": len(history),
        "historical_truncated": max(0, len(historical) - len(history)),
        "corporate_action_alerts": corporate[:_max_alerts()],
        "corporate_action_alert_count": len(corporate),
        "corporate_action_coverage": coverage_counts,
        "outside_watchlist_count": sum(1 for a in alerts if not a["in_watchlist"]),
        "considered": len(snapshot.get("items") or []), "unique_considered": len(latest),
        "max_move_seen_pct": round(largest, 2) if largest is not None else None,
        "dropped": dropped,
        "discovery_coverage": {
            "available_count": snapshot.get("available_count"),
            "screen_truncated": snapshot.get("screen_truncated", 0),
            "limit_truncated": snapshot.get("limit_truncated", 0),
            "full_market_coverage": False,
        },
    })
    if out["truncated"]:
        LOGGER.warning("DISCOVERY: %d current movers cut by the display cap", out["truncated"])
    return out


def log_discovery_alerts() -> Dict[str, Any]:
    """Scheduled server-log surface, not a delivered user notification."""
    result = build_discovery_alerts()
    reclassified = result.get("corporate_action_alerts") or []
    if reclassified:
        LOGGER.warning(
            "DISCOVERY: %d mover(s) were the dividend, not the market: %s",
            len(reclassified),
            ", ".join(
                f"{a['symbol']} {a['move_pct']:+.1f}% "
                f"(ex-div ${a['corporate_action']['cash_amount']:.4g}, "
                f"total return {a['economic_move_pct']:+.1f}%)"
                for a in reclassified[:6]
                if a.get("corporate_action") and a.get("economic_move_pct") is not None
            ),
        )
    alerts = result.get("alerts") or []
    if not alerts:
        return result
    outside = result.get("outside_watchlist_count") or 0
    LOGGER.warning(
        "DISCOVERY: %d movers >=%.0f%% (%d outside the modelled universe): %s",
        len(alerts), result["min_move_pct"], outside,
        ", ".join(
            f"{a['symbol']}{'' if a['in_watchlist'] else '*'} {a['move_pct']:+.1f}%"
            for a in alerts[:8]
        ),
    )
    return result
