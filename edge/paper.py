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
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from edge import fills as F
from edge.broker_alpaca import bracket_request
from edge.contracts import ET, TERMINAL, Forecast
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
    url, h, placed, errors, refused = base_url(), _headers(), [], [], []
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
            refused.append(f"{f.symbol}: HTTP {code} {msg}".strip())
    # `refused` = definite broker refusals this tick (never retried): the caller raises a PROBLEM
    # alert, so a paper account that stops accepting orders is not silent (audit 2026-09-25 U60).
    return {"status": "submitted" if placed or errors else "nothing_to_submit",
            "placed": placed, "errors": errors, "refused": refused}


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


TX_LAST_TRY = (15, 50)     # the time-exit window's last 5-minute tick (the window is 15:30-15:55 ET)


def _lookup(http, cid: str) -> Tuple[Optional[dict], bool]:
    """(order, definite) for OUR client order id. definite is False when the broker could not
    answer (network error, 5xx): 'no such order' and 'cannot tell' are never confused."""
    try:
        r = http.get(f"{base_url()}/v2/orders:by_client_order_id", headers=_headers(), timeout=15,
                     params={"client_order_id": cid, "nested": "true"})
    except Exception:  # noqa: BLE001
        return None, False
    if r.status_code == 200:
        body = r.json()
        return (body if isinstance(body, dict) and body else None), True
    return None, r.status_code == 404


def _order_by_id(http, order_id: Optional[str]) -> Optional[dict]:
    """GET /v2/orders/{id}?nested=true -- the bracket's legs when the by-client-id answer had none."""
    if not order_id:
        return None
    try:
        r = http.get(f"{base_url()}/v2/orders/{order_id}", headers=_headers(), timeout=15,
                     params={"nested": "true"})
    except Exception:  # noqa: BLE001
        return None
    if r.status_code != 200:
        return None
    body = r.json()
    return body if isinstance(body, dict) else None


def _position_qty(http, symbol: str) -> Optional[int]:
    """Shares the paper account holds in `symbol`: 0 when it holds none (404), None if unknown."""
    try:
        r = http.get(f"{base_url()}/v2/positions/{symbol}", headers=_headers(), timeout=15)
    except Exception:  # noqa: BLE001
        return None
    if r.status_code == 404:
        return 0
    if r.status_code != 200:
        return None
    try:
        return int(float((r.json() or {}).get("qty") or 0))
    except (TypeError, ValueError):
        return None


def _close_own_shares(http, symbol: str, qty: int) -> Tuple[bool, Optional[str]]:
    """DELETE /v2/positions/{symbol}?qty=N: Alpaca closes N shares only, never the rest."""
    try:
        r = http.delete(f"{base_url()}/v2/positions/{symbol}?qty={int(qty)}", headers=_headers(), timeout=15)
    except Exception:  # noqa: BLE001
        return False, None
    if not 200 <= r.status_code < 300:
        return False, None
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = None
    return True, (body.get("id") if isinstance(body, dict) else None)


def _et_at(day: str, hh: int, mm: int) -> int:
    d = datetime.strptime(day[:10], "%Y-%m-%d")
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).timestamp())


def time_exit(http, ledger: Ledger, *, day: str, experiments, now: Optional[int] = None) -> Dict[str, Any]:
    """15:30 ET: for each forecast, cancel ITS OWN open orders, then sell the shares ITS
    entry bought and its legs have not already sold -- never the whole symbol position,
    which two experiments can share. A sell the broker refuses (legs still pending
    cancel) is retried next tick; the forecast is marked done only when the broker's own
    orders show it flat.

    Audit 2026-09-25, each fixed here:
      * every sell attempt has its OWN client id (-tx, -tx2, -tx3 ..., counted from the attempts
        recorded in edge_paper): Alpaca refuses a reused id, so a rejected/expired -tx used to
        block every retry;
      * legs missing from the by-client-id answer are read from GET /v2/orders/{id}?nested=true,
        so a sell is not refused for shares still held by an uncancelled leg;
      * the legs are cancelled BEFORE the sell, so a failed sell leaves the shares with no stop.
        On the window's last tick (>= 15:50 ET) a refused sell falls back to closing exactly this
        forecast's remaining shares (DELETE /v2/positions/{symbol}?qty=N, capped by what the
        account holds); if that fails too, or the forecast cannot be confirmed flat, a loud
        NOT FLAT error is recorded and returned under "not_flat"."""
    closed, touched, errors, not_flat = [], False, [], []
    last = now is not None and now >= _et_at(day, *TX_LAST_TRY)

    def alarm(f: Forecast, key: str, rec: dict, why: str) -> None:
        msg = f"{f.symbol} ({f.experiment_id}): {why}"
        not_flat.append(msg)
        ledger.store.put("edge_paper", key, {**rec, "exit_alarm": msg, "exit_alarm_at": now})

    for f in _forecasts(ledger, day, experiments):
        key = f"{f.forecast_id}|paper"
        rec = ledger.store.get("edge_paper", key)
        if not rec or rec.get("state") != "submitted" or rec.get("time_exit_done"):
            continue
        entry = _by_client_id(http, f"{f.forecast_id}-entry")
        if entry is None:
            errors.append(f"{f.symbol}: broker has no record yet")
            if last:
                alarm(f, key, rec, "the broker gave no record of its entry order; cannot confirm it is flat")
            continue
        legs = entry.get("legs") or []
        if not legs:
            legs = (_order_by_id(http, entry.get("id") or rec.get("order_id")) or {}).get("legs") or []
        pending = [o for o in [entry] + legs if o.get("status") in _OPEN]
        if not all(_delete(http, o["id"]) for o in pending):
            errors.append(f"{f.symbol}: cancel failed, retrying")
            if last:
                alarm(f, key, rec, "its bracket orders could not be cancelled; the position was not closed")
            continue
        bought = int(float(entry.get("filled_qty") or 0))
        sold = sum(int(float(leg.get("filled_qty") or 0)) for leg in legs)
        tried = list(rec.get("tx_ids") or [])
        filled_tx, working, unsure = 0, False, False
        for cid in tried or [f"{f.forecast_id}-tx"]:
            o, definite = _lookup(http, cid)
            if o is not None:
                filled_tx += int(float(o.get("filled_qty") or 0))
                working = working or o.get("status") in _OPEN
                if cid not in tried:
                    tried.append(cid)          # an -tx placed before attempts were recorded
            elif not definite and cid in tried:
                unsure = True
        filled_tx += sum(int(float(x.get("qty") or 0)) for x in rec.get("tx_closes") or [])
        if unsure:
            errors.append(f"{f.symbol}: broker could not report its exit orders, retrying")
            if last:
                alarm(f, key, rec, "its exit orders could not be read; cannot confirm it is flat")
            continue
        remaining = bought - sold - filled_tx
        if remaining <= 0 and not working:
            ledger.store.put("edge_paper", key, {**rec, "tx_ids": tried, "time_exit_done": True})
            touched = True
            continue
        if working:
            # A sell is already working: never a second one on top of it (never sell more than we own).
            errors.append(f"{f.symbol}: exit sell working, checked next tick")
            if last:
                alarm(f, key, rec, f"its exit sell is still working at the last check ({remaining} shares open)")
            continue
        cid = f"{f.forecast_id}-tx" if not tried else f"{f.forecast_id}-tx{len(tried) + 1}"
        rec = {**rec, "tx_ids": tried + [cid]}
        ledger.store.put("edge_paper", key, rec)      # recorded BEFORE the post: a timed-out post is checked
        try:
            r = http.post(f"{base_url()}/v2/orders", headers=_headers(), timeout=15, json={
                "symbol": f.symbol, "qty": str(remaining), "side": "sell", "type": "market",
                "time_in_force": "day", "client_order_id": cid})
            ok = 200 <= r.status_code < 300
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            closed.append(f.symbol)
            touched = True
            continue
        if not last:
            errors.append(f"{f.symbol}: exit sell refused, retrying")
            continue
        # Last tick: close exactly this forecast's remaining shares, never more than the account holds.
        held = _position_qty(http, f.symbol)
        qty = remaining if held is None else min(remaining, held)
        if qty <= 0:
            ledger.store.put("edge_paper", key, {**rec, "time_exit_done": True,
                                                 "exit_note": "account holds no shares of it at the last check"})
            touched = True
            continue
        done, oid = _close_own_shares(http, f.symbol, qty)
        if done:
            ledger.store.put("edge_paper", key, {**rec, "tx_closes": list(rec.get("tx_closes") or []) + [
                {"qty": qty, "order_id": oid, "at": now}]})
            closed.append(f.symbol)
            touched = True
        else:
            errors.append(f"{f.symbol}: exit sell and position close both refused")
            alarm(f, key, rec, f"{remaining} shares still open with NO stop after the time exit "
                               "(sell and position close both refused) -- close it by hand")
    status = "error" if not_flat else ("time_exit" if touched else ("error" if errors else "nothing"))
    out = {"status": status, "closed": closed, "errors": errors, "not_flat": not_flat}
    if not_flat:
        out["error"] = "NOT FLAT after the time exit: " + "; ".join(not_flat)
    return out


def _is_time_exit(cid: str, forecast_id: str) -> bool:
    """OUR time-exit ids for this forecast: -tx, then -tx2, -tx3 ... for each retried attempt."""
    head = f"{forecast_id}-tx"
    return cid == head or (cid.startswith(head) and cid[len(head):].isdigit())


def _close_ids(rec: Optional[dict]) -> set:
    """Broker order ids of the last-tick position closes (Alpaca names those orders itself)."""
    return {str(x["order_id"]) for x in (rec or {}).get("tx_closes") or [] if x.get("order_id")}


def _events_from_orders(orders: List[dict], forecast_id: str, close_ids=frozenset()) -> List[F.OrderEvent]:
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
        elif _is_time_exit(cid, forecast_id) or str(o.get("id")) in close_ids:
            add(o, F.TIME_EXIT_ROLE)
    return ev


def reconcile(http, ledger: Ledger, *, day: str, experiments, now: int) -> Dict[str, Any]:
    """After the close: broker records -> the 'actual' record."""
    r = http.get(f"{base_url()}/v2/orders", headers=_headers(), timeout=20,
                 params={"status": "all", "after": f"{day}T00:00:00Z", "nested": "true", "limit": 500})
    r.raise_for_status()
    orders = list(r.json() or [])
    ids = [f.forecast_id for f in _forecasts(ledger, day, experiments)]
    closes = set().union(*[_close_ids(ledger.store.get("edge_paper", f"{fid}|paper")) for fid in ids])
    # Keep the broker's own record of every order (compact) so a simulated/actual mismatch can be
    # explained afterwards -- 2026-09-23 GLND: simulated LOSS, paper NO_FILL, nothing kept to say why.
    keep = ("id", "client_order_id", "symbol", "side", "type", "status", "stop_price", "limit_price",
            "qty", "filled_qty", "filled_avg_price", "submitted_at", "filled_at", "canceled_at",
            "expired_at", "updated_at")
    ledger.store.put("edge_paper_orders", day, {"day": day, "at": now, "orders": [
        {**{k: o.get(k) for k in keep}, "legs": [{k: g.get(k) for k in keep} for g in o.get("legs") or []]}
        for o in orders if any(str(o.get("client_order_id") or "").startswith(fid) for fid in ids)
        or str(o.get("id")) in closes]})
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
            pos = F.reconcile(f, _events_from_orders(orders, src, _close_ids(rec)), now=now)
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
