"""Dedicated post-close issuance for the completed-daily-bar model."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Dict, Optional

LOGGER = logging.getLogger("ghost.daily_model_issuance")
_STATE_KEY = "last_daily_model_issuance_date"


def run_daily_model_issuance(
    *,
    now: Optional[dt.datetime] = None,
    cycle: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Run one prediction cycle per eligible post-close market session.

    The normal market scanner remains useful for diagnostics. This job exists
    so scheduler phase, deploy time, and off-hours cadence cannot decide which
    daily model population enters the point-in-time ledger.
    """
    from core.market_hours import in_daily_model_issuance_window, session_hm

    session_now, _hm = session_hm(now)
    session_date = session_now.date().isoformat()
    if not in_daily_model_issuance_window(session_now):
        return {
            "ok": True,
            "ran": False,
            "reason": "outside_issuance_window",
            "session_date": session_date,
        }

    from core.db import db_conn, ensure_ghost_state

    with db_conn() as conn:
        cur = conn.cursor()
        ensure_ghost_state(cur)
        cur.execute("SELECT val FROM ghost_state WHERE key=%s", (_STATE_KEY,))
        row = cur.fetchone()
    if row and str(row[0]) == session_date:
        return {
            "ok": True,
            "ran": False,
            "reason": "already_issued",
            "session_date": session_date,
        }

    collect_diagnostics = cycle is None
    if collect_diagnostics:
        from core.prediction import run_prediction_cycle
        cycle = run_prediction_cycle
    cycle_result = cycle(with_diag=True) if collect_diagnostics else cycle()
    if (
        collect_diagnostics
        and isinstance(cycle_result, tuple)
        and len(cycle_result) == 2
    ):
        picks, diagnostics = cycle_result
    else:
        picks, diagnostics = cycle_result, {}

    # A zero-pick cycle is a legitimate NO_TRADE and must still claim the day.
    # An entire scan that could not obtain data is different: leave it retryable
    # for the next scheduler tick instead of silently losing the session.
    if diagnostics:
        scanned = int(diagnostics.get("symbols_scanned") or 0)
        skips = diagnostics.get("skip_counts") or {}
        unavailable = sum(
            int(skips.get(reason) or 0)
            for reason in ("no_price", "v3_engine_error")
        )
        if scanned <= 0 or unavailable >= scanned:
            return {
                "ok": False,
                "ran": False,
                "reason": "scan_data_unavailable",
                "session_date": session_date,
                "symbols_scanned": scanned,
                "unavailable": unavailable,
            }

    # Claim the date only after a successful cycle. A transient exception stays
    # retryable on the next five-minute scheduler tick.
    with db_conn() as conn:
        cur = conn.cursor()
        ensure_ghost_state(cur)
        cur.execute(
            "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
            (_STATE_KEY, session_date),
        )
    result = {
        "ok": True,
        "ran": True,
        "session_date": session_date,
        "saved": len(picks or []),
        "symbols_scanned": diagnostics.get("symbols_scanned") if diagnostics else None,
    }
    LOGGER.info("Daily model issuance completed: %s", result)
    return result
