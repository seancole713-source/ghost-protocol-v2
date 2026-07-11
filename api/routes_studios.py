"""
api/routes_studios.py — Studios booking management API
======================================================
Endpoints for Pink/Retro/Red Studio display with reconciliation.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

LOGGER = logging.getLogger("ghost.studios_api")

router = APIRouter(tags=["studios"])


@router.get("/api/studios")
def get_studios():
    """Full studios dashboard — all 3 cards + reconciliation checks."""
    try:
        from core.db import db_conn
        from core.studios import ensure_studios_tables, seed_studios, get_studios_dashboard

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_studios_tables(cur)
            seed_studios(cur)
            conn.commit()
            payload = get_studios_dashboard(cur)

        return JSONResponse({"ok": True, **payload})
    except Exception as e:
        LOGGER.error("studios dashboard failed: %s", str(e)[:200])
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.post("/api/studios/bookings")
def add_studio_booking(
    studio_name: str = Query(...),
    booking_date: str = Query(...),
    hours: float = Query(...),
    rate: Optional[float] = Query(None),
    fees: float = Query(0.0),
    source: str = Query("direct"),
    status: str = Query("expected"),
    guest_name: Optional[str] = Query(None),
    notes: Optional[str] = Query(None),
):
    """Add a booking — gross/net auto-computed from hours×rate."""
    try:
        from core.db import db_conn
        from core.studios import ensure_studios_tables, seed_studios, add_booking

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_studios_tables(cur)
            seed_studios(cur)
            result = add_booking(
                cur, studio_name, booking_date, hours,
                rate=rate, fees=fees, source=source, status=status,
                guest_name=guest_name, notes=notes,
            )
            conn.commit()
        return JSONResponse({"ok": True, **result})
    except Exception as e:
        LOGGER.error("add booking failed: %s", str(e)[:200])
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


@router.get("/api/studios/reconcile")
def reconcile_studios():
    """Run reconciliation checks only — no data mutation."""
    try:
        from core.db import db_conn
        from core.studios import ensure_studios_tables, get_studios_dashboard

        with db_conn() as conn:
            cur = conn.cursor()
            ensure_studios_tables(cur)
            payload = get_studios_dashboard(cur)

        return JSONResponse({
            "ok": True,
            "all_pass": payload["all_checks_pass"],
            "checks": payload["reconciliation"],
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)
