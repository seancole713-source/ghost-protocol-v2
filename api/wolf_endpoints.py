"""
api/wolf_endpoints.py — Phase 4 WOLF Intel API
================================================
Provides /api/wolf/context  →  latest WolfContext as JSON
Called by the cockpit WOLF Intel panel (cockpit_v5.js loadWolfIntel).
"""

from __future__ import annotations

from dataclasses import asdict
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/wolf", tags=["wolf"])


@router.get("/context")
async def get_wolf_context_endpoint(direction: str = "UP"):
    """
    Return the latest WolfContext for the WOLF Intel cockpit panel.
    Cached for 15 minutes (cache lives in wolf_context module).
    Query param `direction` is passed to get_wolf_context() for news scoring.
    """
    try:
        from core.wolf_context import get_wolf_context
        ctx = get_wolf_context(direction=direction.upper())

        # Convert dataclass to dict, handling nested dataclasses
        def _to_dict(obj):
            if hasattr(obj, "__dataclass_fields__"):
                return {k: _to_dict(v) for k, v in asdict(obj).items()}
            if isinstance(obj, list):
                return [_to_dict(i) for i in obj]
            return obj

        payload = _to_dict(ctx)
        payload["ok"] = True
        return JSONResponse(content=payload)

    except Exception as exc:
        return JSONResponse(
            status_code=200,  # Don't break the front-end on error
            content={"ok": False, "error": str(exc)},
        )


@router.get("/price")
async def get_wolf_price_endpoint():
    """
    Return latest WOLF price from the quorum (Polygon / Alpaca / yfinance).
    Used as a lightweight price check from the cockpit.
    """
    try:
        from wolf_helpers import get_wolf_price
        price = await get_wolf_price()
        return JSONResponse(content={"ok": True, "symbol": "WOLF", "price": price})
    except Exception as exc:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(exc)})
