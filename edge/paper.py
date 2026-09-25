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
    kid = (os.getenv("ALPACA_KEY_ID") or os.getenv("APCA_API_KEY_ID") or "").strip()
    sec = (os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY") or "").strip()
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


_OPEN = {"new", "accepted", "pending_new", "accepted_for_bidding", "held", "partially_filled",
         "pending_replace", "calculated"}


def _by_client_id(http, cid: str) -> Optional[dict]:
    """The broker's own record of OUR order id, or None if it has none (or cannot say)."""
    try:
        r = http.get(f"{base_url()}/v2/orders:by_client_order_id", headers=_headers(), timeout=15,
                     params={"client_order_id": cid, "nested": "true"})
    except Exception:  # noqa: BLE001
        return None
    return (r.json() or None) if r.status_code == 200 else None


def _transient(code: Optional[int]) -> bool:
    return code is None or code == 429 or code >= 500


def submit(http, ledger: Ledger, *, day: str, experiments) -> Dict[str, Any]:
    """Place every not-yet-placed forecast of `day` as a paper bracket. Idempotent: before
    anything is recorded as rejected, the broker is asked whether it already holds OUR
    client_order_id (a timed-out POST that Alpaca accepted is `submitted`, not `rejected`),
    and a transient failure (timeout, 429, 5xx) records nothing so the next tick retries."""
    url, h, placed, errors = base_url(), _headers(), [], []
    for f in _forecasts(ledger, day, experiments):
        key = f"{f.forecast_id}|paper"
        if ledger.store.get("edge_paper", key):
            continue
        body = bracket_request(f.forecast_id, f.symbol, f.shares, entry_stop=f.entry_trigger,
                               entry_limit=f.entry_limit, target=f.target, stop=f.stop)
        code, msg, oid = None, "", None
        try:
            r = http.post(f"{url}/v2/orders", json=body, headers=h, timeout=15)
            code = r.status_code
            if 200 <= code < 300:
                oid = (r.json() or {}).get("id")
            else:
                try:
                    msg = str((r.json() or {}).get("message") or "")[:200]
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            msg = type(exc).__name__
        if oid is None:
            held = _by_client_id(http, f"{f.forecast_id}-entry")
            if held:
                oid = held.get("id")
        if oid is not None:
            ledger.store.put("edge_paper", key, {"forecast_id": f.forecast_id, "symbol": f.symbol,
                                                 "status_code": code, "order_id": oid, "state": "submitted"})
            placed.append(f.symbol)
        elif _transient(code):
            errors.append(f"{f.symbol}: transient {code or msg}, retrying next tick")
        else:
            # A definite refusal is a fact about execution, recorded as a rejected entry.
            ledger.store.put("edge_paper", key, {"forecast_id": f.forecast_id, "symbol": f.symbol,
                                                 "status_code": code, "state": "rejected", "message": msg})
            errors.append(f"{f.symbol}: HTTP {code} {msg}")
    return {"status": "submitted" if placed or errors else "nothing_to_submit",
            "placed": placed, "errors": errors}


def _delete(http, order_id: str) -> bool:
    try:
        r = http.delete(f"{base_url()}/v2/orders/{order_id}", headers=_headers(), timeout=15)
    except Exception:  # noqa: BLE001
        return False
    return r.status_code in (200, 204) or r.status_code == 404


def cancel_unfilled_entries(http, ledger: Ledger, *, day: str, experiments,
                            now: Optional[int] = None) -> Dict[str, Any]:
    """At each forecast's OWN entry expiry (10:30 ET for the morning card, issuance
    + 20 min intraday), an entry that has not filled is cancelled with its legs. Only
    THIS forecast's order is touched; a failed cancel is retried next tick. A partly
    filled entry is left alone: its legs protect the filled shares until the time exit."""
    done, touched, errors = [], False, []
    for f in _forecasts(ledger, day, experiments):
        rec = ledger.store.get("edge_paper", f"{f.forecast_id}|paper")
        if not rec or rec.get("state") != "submitted" or rec.get("entry_cancel_checked"):
            continue
        if now is not None and now < f.entry_expiry:
            continue
        o = _by_client_id(http, f"{f.forecast_id}-entry")
        if o is None:
            errors.append(f"{f.symbol}: broker has no record yet")
            continue
        if o.get("status") in _OPEN and float(o.get("filled_qty") or 0) == 0:
            if not _delete(http, o["id"]):
                errors.append(f"{f.symbol}: cancel failed, retrying")
                continue
            done.append(f.symbol)
        ledger.store.put("edge_paper", f"{f.forecast_id}|paper", {**rec, "entry_cancel_checked": True})
        touched = True
    return {"status": "entries_checked" if touched else ("error" if errors else "nothing"),
            "canceled": done, "errors": errors}


def time_exit(http, ledger: Ledger, *, day: str, experiments) -> Dict[str, Any]:
    """15:30 ET: for each forecast, cancel ITS OWN open orders, then sell the shares ITS
    entry bought and its legs have not already sold -- never the whole symbol position,
    which two experiments can share. A sell the broker refuses (legs still pending
    cancel) is retried next tick; the forecast is marked done only when it is flat."""
    closed, touched, errors = [], False, []
    for f in _forecasts(ledger, day, experiments):
        rec = ledger.store.get("edge_paper", f"{f.forecast_id}|paper")
        if not rec or rec.get("state") != "submitted" or rec.get("time_exit_done"):
            continue
        entry = _by_client_id(http, f"{f.forecast_id}-entry")
        if entry is None:
            errors.append(f"{f.symbol}: broker has no record yet")
            continue
        legs = entry.get("legs") or []
        pending = [o for o in [entry] + legs if o.get("status") in _OPEN]
        if not all(_delete(http, o["id"]) for o in pending):
            errors.append(f"{f.symbol}: cancel failed, retrying")
            continue
        bought = int(float(entry.get("filled_qty") or 0))
        sold = sum(int(float(leg.get("filled_qty") or 0)) for leg in legs)
        tx = _by_client_id(http, f"{f.forecast_id}-tx")
        remaining = bought - sold - (int(float(tx.get("filled_qty") or 0)) if tx else 0)
        if remaining > 0 and not (tx and tx.get("status") in _OPEN):
            try:
                r = http.post(f"{base_url()}/v2/orders", headers=_headers(), timeout=15, json={
                    "symbol": f.symbol, "qty": str(remaining), "side": "sell", "type": "market",
                    "time_in_force": "day", "client_order_id": f"{f.forecast_id}-tx"})
                ok = 200 <= r.status_code < 300
            except Exception:  # noqa: BLE001
                ok = False
            if not ok:
                errors.append(f"{f.symbol}: exit sell refused, retrying")
                continue
            closed.append(f.symbol)
        ledger.store.put("edge_paper", f"{f.forecast_id}|paper", {**rec, "time_exit_done": True})
        touched = True
    return {"status": "time_exit" if touched else ("error" if errors else "nothing"),
            "closed": closed, "errors": errors}


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
    ids = [f.forecast_id for f in _forecasts(ledger, day, experiments)]
    # Keep the broker's own record of every order (compact) so a simulated/actual mismatch can be
    # explained afterwards -- 2026-09-23 GLND: simulated LOSS, paper NO_FILL, nothing kept to say why.
    keep = ("id", "client_order_id", "symbol", "side", "type", "status", "stop_price", "limit_price",
            "qty", "filled_qty", "filled_avg_price", "submitted_at", "filled_at", "canceled_at",
            "expired_at", "updated_at")
    ledger.store.put("edge_paper_orders", day, {"day": day, "at": now, "orders": [
        {**{k: o.get(k) for k in keep}, "legs": [{k: g.get(k) for k in keep} for g in o.get("legs") or []]}
        for o in orders if any(str(o.get("client_order_id") or "").startswith(fid) for fid in ids)]})
    settled = {}
    todays = _forecasts(ledger, day, experiments)
    for f in todays:
        prior = ledger.store.get("outcomes", f"{f.forecast_id}|actual")
        if prior and prior.get("outcome") in TERMINAL:
            continue
        rec = ledger.store.get("edge_paper", f"{f.forecast_id}|paper")
        src = f.forecast_id if rec else (_shared_order(ledger, f, todays) or f.forecast_id)
        if src != f.forecast_id:
            rec = ledger.store.get("edge_paper", f"{src}|paper")
        if rec and rec.get("state") == "rejected":
            pos = F.Position(state="ENTRY_REJECTED")
        else:
            pos = F.reconcile(f, _events_from_orders(orders, src), now=now)
        res = F.to_resolution(f, pos)
        ledger.settle(f.forecast_id, res, now=now, record="actual")
        settled[f"{f.experiment_id}:{f.symbol}"] = {"actual": res.outcome, "pnl_usd": res.pnl_usd,
                                                    "warnings": pos.warnings}
    return {"status": "reconciled" if settled else "nothing", "settled": settled}


def _shared_order(ledger: Ledger, f: Forecast, todays) -> Optional[str]:
    """The forecast whose paper order stands for `f`: a twin issued at the same moment on the
    same stock with identical levels and size that did place an order (a paired later version
    records beside v1 and shares its order, never a second one). None when there is no twin."""
    key = (f.symbol, f.issued_at, f.entry_trigger, f.entry_limit, f.target, f.stop, f.shares)
    for g in todays:
        if g.forecast_id != f.forecast_id and (g.symbol, g.issued_at, g.entry_trigger, g.entry_limit,
                                               g.target, g.stop, g.shares) == key \
                and ledger.store.get("edge_paper", f"{g.forecast_id}|paper"):
            return g.forecast_id
    return None


def probe(http):
    """Does the PAPER trading account accept these keys and allow trading? Reads /v2/account
    on the pinned paper host; never logs the account number."""
    from edge.providers import base as B
    try:
        r = http.get(f"{base_url()}/v2/account", headers=_headers(), timeout=15)
    except Exception as exc:  # noqa: BLE001
        return B.Probe("broker.paper", "alpaca_paper", B.ERROR, note=f"{type(exc).__name__}: {str(exc)[:80]}")
    if r.status_code >= 400:
        return B.Probe("broker.paper", "alpaca_paper", B.classify(r.status_code), http_status=r.status_code,
                       note="the paper host refused these keys (live keys are refused here by design)")
    a = r.json() or {}
    blocked = [k for k in ("trading_blocked", "account_blocked", "trade_suspended_by_user") if a.get(k)]
    ok = str(a.get("status") or "").upper() == "ACTIVE" and not blocked
    return B.Probe("broker.paper", "alpaca_paper", B.OK if ok else B.ERROR, http_status=r.status_code, rows=1,
                   note=f"paper account {a.get('status')}" + (f"; blocked: {', '.join(blocked)}" if blocked else ""))
