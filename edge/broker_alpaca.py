"""Alpaca (paper first) order updates -> edge.fills.OrderEvent. Per Alpaca's docs.

Roles come from OUR client_order_id convention, never inferred from prices:
  <forecast_id>-entry  <forecast_id>-tp  <forecast_id>-sl  <forecast_id>-tx (time exit)
Alpaca documents that bracket orders do not support extended hours, so an
entry meant to work pre-market cannot carry broker-held protection; the
session check below refuses that combination instead of discovering it live.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from edge import fills as F

_STATUS = {
    "new": F.SUBMITTED, "pending_new": F.SUBMITTED, "accepted": F.ACCEPTED,
    "partial_fill": F.PARTIAL, "partially_filled": F.PARTIAL, "fill": F.FILLED, "filled": F.FILLED,
    "canceled": F.CANCELED, "expired": F.EXPIRED, "rejected": F.REJECTED,
    "held": F.ACCEPTED,       # bracket legs wait "held" until the entry fills
}
_ROLE = {"entry": F.ENTRY, "tp": F.TARGET, "sl": F.STOP, "tx": F.TIME_EXIT_ROLE}


def _epoch(s: Optional[str]) -> int:
    return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())


def role_of(client_order_id: str) -> str:
    suffix = (client_order_id or "").rsplit("-", 1)[-1]
    return _ROLE.get(suffix, F.MANUAL)


def to_event(update: Dict[str, Any], *, prior_filled_qty: int = 0) -> Optional[F.OrderEvent]:
    """One trade_updates message -> OrderEvent. Fill quantity is made INCREMENTAL.

    Alpaca reports cumulative filled_qty on each update; edge.fills wants the
    quantity filled by THIS event, so the caller passes what it had seen before.
    """
    order = update.get("order") or {}
    status = _STATUS.get(str(update.get("event") or order.get("status") or "").lower())
    if status is None:
        return None
    cum = int(float(order.get("filled_qty") or 0))
    inc = max(0, cum - prior_filled_qty) if status in (F.PARTIAL, F.FILLED) else 0
    px = update.get("price") or order.get("filled_avg_price")
    ts = update.get("timestamp") or order.get("updated_at") or order.get("submitted_at")
    return F.OrderEvent(
        ts=_epoch(ts), order_id=str(order.get("id") or order.get("client_order_id")),
        role=role_of(str(order.get("client_order_id") or "")), status=status,
        fill_qty=inc, fill_price=float(px) if (inc and px is not None) else None,
        note=str(order.get("reject_reason") or ""),
    )


def bracket_request(forecast_id: str, symbol: str, shares: int, *, entry_stop: float, entry_limit: float,
                    target: float, stop: float, extended_hours: bool = False) -> Dict[str, Any]:
    """The order the operator (or, later, paper automation) submits."""
    if extended_hours:
        raise ValueError("bracket orders do not support extended hours (Alpaca docs); "
                         "use a plain order and an explicit protective stop after the open")
    return {
        "symbol": symbol, "qty": str(shares), "side": "buy", "type": "stop_limit",
        "stop_price": f"{entry_stop:.2f}", "limit_price": f"{entry_limit:.2f}",
        "time_in_force": "day", "order_class": "bracket",
        "client_order_id": f"{forecast_id}-entry",
        "take_profit": {"limit_price": f"{target:.2f}"},
        "stop_loss": {"stop_price": f"{stop:.2f}"},
    }
