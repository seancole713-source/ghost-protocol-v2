"""Paper execution through Alpaca -- the third record, from a real broker.

Every shadow forecast is placed as a bracket order in the Alpaca PAPER
account, run through the same day as the operator's card (cancel unfilled
entries at 10:30 ET, time exit at 15:30 ET), and reconciled after the close
from the broker's own order records into the ledger's "actual" record.
Partial fills, rejections, expiries and stop slippage become facts, not
assumptions.

PAPER ONLY, ENFORCED. The trading base URL is pinned to Alpaca's paper host and
anything else is refused -- including Ghost's APCA_API_BASE_URL, which this
module never reads. Alpaca keys are account-specific, so a live key sent to the
paper host is simply rejected. There is no live path in this file.

Idempotent throughout: client_order_id = "<forecast_id>-entry" (Alpaca rejects
a duplicate id), and each step stores a marker so a re-run tick does nothing.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from edge import fills as F
from edge.broker_alpaca import bracket_request
from edge.contracts import TERMINAL, Forecast
from edge.ledger import Ledger

PAPER_HOST = "paper-api.alpaca.markets"


class NotPaper(RuntimeError):
    pass


def base_url() -> str:
    url = (os.getenv("EDGE_PAPER_BASE_URL") or f"https://{PAPER_HOST}").rstrip("/")
    if urlparse(url).hostname != PAPER_HOST:
        raise NotPaper(f"edge paper execution refuses non-paper host {urlparse(url).hostname!r}")
    return url


def _headers() -> Dict[str, str]:
    kid = (os.getenv("ALPACA_KEY_ID") or "").strip()
    sec = (os.getenv("ALPACA_SECRET_KEY") or "").strip()
    if not kid or not sec:
        raise RuntimeError("ALPACA_KEY_ID / ALPACA_SECRET_KEY not set")
    return {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec}


def _epoch(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())


def _forecasts(ledger: Ledger, day: str, experiments) -> List[Forecast]:
    out = []
    for spec in experiments:
        for row in ledger.store.scan("forecasts", experiment_id=spec.experiment_id, session_date=day):
            out.append(Forecast(**{k: row[k] for k in Forecast.__dataclass_fields__}))
    return out


def submit(http, ledger: Ledger, *, day: str, experiments) -> Dict[str, Any]:
    """Place every not-yet-placed forecast of `day` as a paper bracket."""
    url, h, placed, errors = base_url(), _headers(), [], []
    for f in _forecasts(ledger, day, experiments):
        key = f"{f.forecast_id}|paper"
        if ledger.store.get("edge_paper", key):
            continue
        body = bracket_request(f.forecast_id, f.symbol, f.shares, entry_stop=f.entry_trigger,
                               entry_limit=f.entry_limit, target=f.target, stop=f.stop)
        r = http.post(f"{url}/v2/orders", json=body, headers=h, timeout=15)
        rec = {"forecast_id": f.forecast_id, "symbol": f.symbol, "status_code": r.status_code}
        if 200 <= r.status_code < 300:
            o = r.json() or {}
            rec.update({"order_id": o.get("id"), "state": "submitted"})
            placed.append(f.symbol)
        else:
            # A refusal is a fact about execution, recorded as a rejected entry.
            msg = ""
            try:
                msg = str((r.json() or {}).get("message") or "")[:200]
            except Exception:  # noqa: BLE001
                pass
            rec.update({"state": "rejected", "message": msg})
            errors.append(f"{f.symbol}: HTTP {r.status_code} {msg}")
        ledger.store.put("edge_paper", key, rec)
    return {"status": "submitted" if placed or errors else "nothing_to_submit",
            "placed": placed, "errors": errors}


def _open_orders(http, symbol: str) -> List[dict]:
    r = http.get(f"{base_url()}/v2/orders", params={"status": "open", "symbols": symbol, "nested": "true"},
                 headers=_headers(), timeout=15)
    r.raise_for_status()
    return list(r.json() or [])


def cancel_unfilled_entries(http, ledger: Ledger, *, day: str, experiments,
                            now: Optional[int] = None) -> Dict[str, Any]:
    """At each forecast's OWN entry expiry (10:30 ET for the morning card, issuance
    + 20 min intraday), an entry that has not filled is cancelled with its legs."""
    done, touched = [], False
    for f in _forecasts(ledger, day, experiments):
        rec = ledger.store.get("edge_paper", f"{f.forecast_id}|paper")
        if not rec or rec.get("state") != "submitted" or rec.get("entry_cancel_checked"):
            continue
        if now is not None and now < f.entry_expiry:
            continue
        for o in _open_orders(http, f.symbol):
            if o.get("client_order_id") == f"{f.forecast_id}-entry" and float(o.get("filled_qty") or 0) == 0:
                http.delete(f"{base_url()}/v2/orders/{o['id']}", headers=_headers(), timeout=15)
                done.append(f.symbol)
        ledger.store.put("edge_paper", f"{f.forecast_id}|paper", {**rec, "entry_cancel_checked": True})
        touched = True
    return {"status": "entries_checked" if touched else "nothing", "canceled": done}


def time_exit(http, ledger: Ledger, *, day: str, experiments) -> Dict[str, Any]:
    """15:30 ET: cancel the bracket legs, then close what is still open."""
    closed, touched = [], False
    for f in _forecasts(ledger, day, experiments):
        rec = ledger.store.get("edge_paper", f"{f.forecast_id}|paper")
        if not rec or rec.get("state") != "submitted" or rec.get("time_exit_done"):
            continue
        for o in _open_orders(http, f.symbol):
            http.delete(f"{base_url()}/v2/orders/{o['id']}", headers=_headers(), timeout=15)
        r = http.get(f"{base_url()}/v2/positions/{f.symbol}", headers=_headers(), timeout=15)
        if r.status_code == 200:
            qty = abs(int(float((r.json() or {}).get("qty") or 0)))
            if qty:
                http.post(f"{base_url()}/v2/orders", headers=_headers(), timeout=15, json={
                    "symbol": f.symbol, "qty": str(qty), "side": "sell", "type": "market",
                    "time_in_force": "day", "client_order_id": f"{f.forecast_id}-tx"})
                closed.append(f.symbol)
        ledger.store.put("edge_paper", f"{f.forecast_id}|paper", {**rec, "time_exit_done": True})
        touched = True
    return {"status": "time_exit" if touched else "nothing", "closed": closed}


def _events_from_orders(orders: List[dict], forecast_id: str) -> List[F.OrderEvent]:
    """Alpaca order snapshots -> OrderEvents. Roles by OUR ids and leg type, never by price."""
    ev: List[F.OrderEvent] = []

    def add(o: dict, role: str) -> None:
        oid = str(o.get("id"))
        sub = _epoch(o.get("submitted_at"))
        if sub:
            ev.append(F.OrderEvent(sub, oid, role, F.ACCEPTED))
        if o.get("status") == "rejected":
            ev.append(F.OrderEvent(_epoch(o.get("failed_at") or o.get("updated_at")) or sub or 0, oid, role,
                                   F.REJECTED, note=str(o.get("reject_reason") or "")))
        qty = int(float(o.get("filled_qty") or 0))
        if qty and o.get("filled_avg_price"):
            ev.append(F.OrderEvent(_epoch(o.get("filled_at") or o.get("updated_at")) or sub or 0, oid, role,
                                   F.FILLED, qty, float(o["filled_avg_price"])))
        for key, st in (("canceled_at", F.CANCELED), ("expired_at", F.EXPIRED)):
            if o.get(key) and not qty:
                ev.append(F.OrderEvent(_epoch(o[key]) or 0, oid, role, st))

    for o in orders:
        cid = str(o.get("client_order_id") or "")
        if cid == f"{forecast_id}-entry":
            add(o, F.ENTRY)
            for leg in o.get("legs") or []:
                role = F.TARGET if leg.get("type") == "limit" else F.STOP
                add(leg, role)
        elif cid == f"{forecast_id}-tx":
            add(o, F.TIME_EXIT_ROLE)
    return ev


def reconcile(http, ledger: Ledger, *, day: str, experiments, now: int) -> Dict[str, Any]:
    """After the close: broker records -> the 'actual' record."""
    r = http.get(f"{base_url()}/v2/orders", headers=_headers(), timeout=20,
                 params={"status": "all", "after": f"{day}T00:00:00Z", "nested": "true", "limit": 500})
    r.raise_for_status()
    orders = list(r.json() or [])
    settled = {}
    for f in _forecasts(ledger, day, experiments):
        prior = ledger.store.get("outcomes", f"{f.forecast_id}|actual")
        if prior and prior.get("outcome") in TERMINAL:
            continue
        rec = ledger.store.get("edge_paper", f"{f.forecast_id}|paper")
        if rec and rec.get("state") == "rejected":
            pos = F.Position(state="ENTRY_REJECTED")
        else:
            pos = F.reconcile(f, _events_from_orders(orders, f.forecast_id), now=now)
        res = F.to_resolution(f, pos)
        ledger.settle(f.forecast_id, res, now=now, record="actual")
        settled[f"{f.experiment_id}:{f.symbol}"] = {"actual": res.outcome, "pnl_usd": res.pnl_usd,
                                                    "warnings": pos.warnings}
    return {"status": "reconciled" if settled else "nothing", "settled": settled}
