"""Market-wide discovery alerts — the layer that would have shown you GoPro.

On 2026-09-01 GPRO announced a merger and ran ~183% in five days. Ghost never
mentioned it, and the reason was not a bad prediction: GPRO is not in
config/symbols.py, and that hardcoded 107-symbol list is the ENTIRE universe
Ghost scans, models, and picks from. It was never looked at.

Meanwhile Ghost's external screener had been pulling market-wide movers hourly
the whole time, into ghost_external_observations. That lane is deliberately
walled off -- core/external_screener_ingest.py states it "never mutates Ghost's
symbol universe, creates candidates, sends alerts, changes confidence, or
touches a wallet". The boundary is correct: unvalidated third-party screen rows
must never reach the fire path.

But "must not become a pick" was implemented as "must not be seen", and those
are different requirements. This module closes only that gap. It READS the
existing advisory ledger and ranks what a human would want to know about. It
writes nothing, scores nothing, and cannot create a candidate:
external_screener_ingest keeps its invariant untouched because the alerting
lives here, in a consumer, rather than in the ingest path.

Coverage note (2026-09-05): the Yahoo saved screens this originally read are
capped at 50 rows each and hardcoded to two screens, ~100 symbols per cycle out
of ~11,000 listed US tickers, with no day_losers screen at all -- so the
absolute-move ranking below could never have been shown a crash. The
full-market side now arrives from core/market_wide_snapshot.py, which pulls
every US ticker's close-to-close move from one Polygon grouped-daily call and
writes it into this same advisory ledger.

Every alert carries decision_eligible=False. A discovery is a reason to LOOK,
never a reason to trade -- and notably, a symbol that has already run is often
the worst thing to buy. GPRO post-announcement becomes a merger-arb pinned
stock drifting to a fixed deal price, which is exactly the APGE pattern that
produced 49/49 expired outcomes in the shadow ledger.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.discovery")

ALERT_VERSION = "discovery_alerts_v1"


def _min_move_pct() -> float:
    """Absolute move that makes a symbol worth a human's attention."""
    try:
        return max(1.0, float(os.getenv("DISCOVERY_ALERT_MIN_MOVE_PCT", "10")))
    except Exception:
        return 10.0


def _max_age_s() -> int:
    """Display horizon, not a data-quality gate.

    Per-provider freshness is already enforced upstream at validation time:
    each provider passes its own max_age_s to normalize_external_observation
    (30 minutes for the intraday Yahoo screens, four days for Polygon daily
    bars) and anything past it lands with validation_valid=FALSE and never
    reaches this function. What remains is how far back a human still wants to
    look. A daily bar is stamped at the START of its session, so Friday's close
    is ~45h old by Sunday afternoon; a 24h horizon would blank the full-market
    lane every weekend.
    """
    try:
        return max(300, int(os.getenv("DISCOVERY_ALERT_MAX_AGE_S", "259200")))
    except Exception:
        return 259200


def _max_alerts() -> int:
    try:
        return max(1, min(50, int(os.getenv("DISCOVERY_ALERT_MAX", "12"))))
    except Exception:
        return 12


def _per_screen() -> int:
    try:
        return max(1, min(200, int(os.getenv("DISCOVERY_ALERT_PER_SCREEN", "60"))))
    except Exception:
        return 60


def build_discovery_alerts(limit: int = 240) -> Dict[str, Any]:
    """Rank recent market-wide discoveries a human would want to see.

    Read-only. Returns advisory items only; nothing here is trade-eligible.
    """
    from core.external_context_ledger import recent_external_discoveries

    started = int(time.time())
    out: Dict[str, Any] = {
        "alert_version": ALERT_VERSION,
        "computed_at": started,
        "min_move_pct": _min_move_pct(),
        "max_age_s": _max_age_s(),
        "advisory_only": True,
        "decision_eligible": False,
        "note": (
            "Discovery only: a reason to look, never a reason to trade. These "
            "symbols are outside Ghost's modelled universe and carry no gate, "
            "no proof and no position sizing."
        ),
        "alerts": [],
    }

    try:
        # per_screen keeps the once-a-day full-market batch from being crowded
        # out of the window by the every-15-minutes Yahoo lane.
        snapshot = recent_external_discoveries(limit=limit, per_screen=_per_screen())
    except Exception as exc:  # noqa: BLE001 - advisory surface, never a gate
        out["error"] = str(exc)[:160]
        return out

    min_move = _min_move_pct()
    max_age = _max_age_s()
    seen: Dict[str, Dict[str, Any]] = {}

    # Why-zero diagnostics. An empty alert list is ambiguous on its own -- a
    # genuinely quiet tape and a filter that can never pass look identical, and
    # that ambiguity is how a dead lane survives unnoticed. Recording the
    # largest move actually observed, and where rows were dropped, makes the
    # zero self-explanatory: a max_move_seen near the threshold means quiet, a
    # null one means nothing is arriving with a usable move at all.
    largest: Optional[float] = None
    dropped = {"no_move": 0, "stale": 0, "invalid": 0, "below_threshold": 0}

    for item in snapshot.get("items") or []:
        symbol = (item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        # NOT quarantined. In this ledger `quarantined` means "outside
        # config/symbols.py" -- normalize_external_observation sets it as
        # `symbol and not in_official_watchlist` -- so it is TRUE for every
        # symbol this lane exists to surface. Dropping on it made the alert
        # list structurally incapable of reporting GPRO, which is the one
        # thing PR #182 was built to do. The real quality filter is
        # validation_valid: bad symbol, missing or future timestamp, stale
        # beyond the provider's own bound, non-positive price.
        if item.get("validation_valid") is False:
            dropped["invalid"] += 1
            continue
        age = item.get("source_age_s")
        if age is not None and int(age) > max_age:
            dropped["stale"] += 1
            continue
        move = item.get("move_pct")
        if move is None:
            dropped["no_move"] += 1
            continue
        try:
            move = float(move)
        except (TypeError, ValueError):
            dropped["no_move"] += 1
            continue
        if largest is None or abs(move) > abs(largest):
            largest = move
        if abs(move) < min_move:
            dropped["below_threshold"] += 1
            continue

        # Keep the largest absolute move per symbol; screens overlap.
        prior = seen.get(symbol)
        if prior is not None and abs(float(prior["move_pct"])) >= abs(move):
            continue
        seen[symbol] = {
            "symbol": symbol,
            "move_pct": round(move, 2),
            "price": item.get("price"),
            "volume": item.get("volume"),
            "avg_volume": item.get("avg_volume"),
            "screen": item.get("screen"),
            "provider": item.get("provider"),
            "source_age_s": age,
            "freshness": item.get("freshness"),
            "delayed": bool(item.get("delayed")),
            # The GoPro case in one field: Ghost cannot form a view on this
            # symbol at all, because it is outside the modelled universe.
            "in_watchlist": bool(item.get("in_official_watchlist")),
            "ghost_can_model_it": bool(item.get("in_official_watchlist")),
            "decision_eligible": False,
        }

    alerts = sorted(seen.values(), key=lambda a: -abs(a["move_pct"]))[: _max_alerts()]
    out["alerts"] = alerts
    out["alert_count"] = len(alerts)
    out["outside_watchlist_count"] = sum(1 for a in alerts if not a["in_watchlist"])
    out["considered"] = len(snapshot.get("items") or [])
    out["max_move_seen_pct"] = round(largest, 2) if largest is not None else None
    out["dropped"] = dropped
    return out


def log_discovery_alerts() -> Dict[str, Any]:
    """Scheduled surface. Logs at WARNING so movers Ghost cannot model still
    reach the operator instead of dying in an advisory table."""
    result = build_discovery_alerts()
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
