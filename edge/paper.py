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
from edge.broker_alpaca import bracket_request, oco_exit_request
from edge.contracts import ET, TERMINAL, Forecast
from edge.ledger import Ledger
from shared.redaction import redact_exc

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


# Final order states: filled_qty can no longer change. Everything else -- pending_cancel, held,
# done_for_day, an unknown status -- is still in flight. A 2xx to a cancel is the broker taking
# the REQUEST, not a cancellation: an order is only ever judged final from a fresh read of it.
_FINAL = frozenset({"filled", "canceled", "expired", "rejected"})
PROTECT_GRACE = 300        # seconds past the entry deadline an unresolved entry may go before it is an alarm
MAX_TRIES = 3              # protective-exit / flatten posts per forecast before only the alarm remains


def _final(o: Optional[dict]) -> bool:
    return str((o or {}).get("status") or "") in _FINAL


def _num(o: Optional[dict], k: str) -> Optional[int]:
    try:
        return int(float((o or {}).get(k) or 0))
    except (TypeError, ValueError):
        return None


def _filled(o: Optional[dict]) -> int:
    return _num(o, "filled_qty") or 0


def _legs_of(http, o: dict, *, needed: bool, order_id: Optional[str] = None) -> Tuple[List[dict], bool]:
    """(legs, readable) of a bracket / OCO parent, from the nested answer or GET /v2/orders/{id}.
    Legs that cannot be read are fatal only when `needed` (they could have sold shares)."""
    legs = o.get("legs")
    if legs:
        return [g for g in legs if isinstance(g, dict)], True
    full = _order_by_id(http, o.get("id") or order_id)
    if full is not None:
        return [g for g in full.get("legs") or [] if isinstance(g, dict)], True
    return [], not needed


def _snapshot(http, f: Forecast, rec: dict) -> Tuple[Optional[dict], str]:
    """FRESH broker reads of every order through which this forecast holds or sells shares: its
    entry, the bracket legs, any protective OCO (-px...) with its leg, any flatten (-pf...).
    (None, why) when any of them cannot be read -- 'cannot tell' is never 'nothing there'.
    bought/sold are the broker's cumulative filled quantities on those reads."""
    entry, definite = _lookup(http, f"{f.forecast_id}-entry")
    if entry is None:
        return None, ("the broker has no record of its entry order" if definite
                      else "the broker could not report its entry order")
    # The entry's legs can only have sold shares once the entry bought some.
    legs, ok = _legs_of(http, entry, needed=_filled(entry) > 0, order_id=rec.get("order_id"))
    if not ok:
        return None, "its bracket legs could not be read"
    protect, flatten = [], []
    for field, out in (("protect_ids", protect), ("flatten_ids", flatten)):
        for cid in rec.get(field) or []:
            o, definite = _lookup(http, cid)
            if o is None:
                if not definite:
                    return None, f"its exit order {cid} could not be read"
                continue                    # definitely never placed (a post that did not land)
            out.append(o)
            if field == "protect_ids":
                sub, ok = _legs_of(http, o, needed=o.get("status") != "rejected")
                if not ok:
                    return None, f"the legs of its protective order {cid} could not be read"
                out.extend(sub)
    exits = legs + protect + flatten
    if any(_num(o, "filled_qty") is None for o in [entry] + exits):
        return None, "the broker reported a fill quantity that cannot be read"
    return {"entry": entry, "legs": legs, "protect": protect, "flatten": flatten, "exits": exits,
            "owned": [entry] + exits, "bought": _filled(entry), "sold": sum(_filled(o) for o in exits)}, ""


def _cancel_all(http, orders: List[dict]) -> List[str]:
    """Ask the broker to cancel every order still working (pending_cancel is already asked).
    Returns the ids whose cancel request was refused or failed; the caller re-reads either way."""
    refused = []
    for o in orders:
        if _final(o) or o.get("status") == "pending_cancel":
            continue
        if not o.get("id") or not _delete(http, o["id"]):
            refused.append(str(o.get("id")))
    return refused


def _remaining(o: dict) -> int:
    return (_num(o, "qty") or 0) - _filled(o)


def _legs_protect(legs: List[dict], qty: int) -> bool:
    """Do the bracket's OWN legs protect `qty` shares? Only once activated: until the entry is
    FULLY filled every leg waits 'held'. A live stop leg sized to exactly what is owned."""
    live = [g for g in legs if not _final(g) and g.get("status") != "pending_cancel"]
    if qty <= 0 or not live or all(g.get("status") == "held" for g in live):
        return False
    return any("stop" in str(g.get("type") or "") for g in live) and all(_remaining(g) == qty for g in live)


def _post(http, body: dict) -> Tuple[Optional[int], bool]:
    """(HTTP status, accepted). None status = no answer (timeout): the caller looks the id up."""
    try:
        r = http.post(f"{base_url()}/v2/orders", json=body, headers=_headers(), timeout=15)
    except Exception:  # noqa: BLE001
        return None, False
    return r.status_code, 200 <= r.status_code < 300


def _protect_entry(http, f: Forecast, rec: dict, now: Optional[int], save, note: dict) -> Tuple[str, str]:
    """One tick of the entry-deadline step for one forecast (F01). Returns (kind, message), kind:
    done | canceled (entry final; nothing owned or every owned share protected or flat),
    protected (a protective order confirmed working), pending (in flight, re-checked next tick),
    error (retry next tick), alarm (shares owned with NO confirmed protection). note["canceled"]
    is set when this tick's cancel is what ended the entry."""
    snap, why = _snapshot(http, f, rec)
    if snap is None:
        return "error", f"{f.symbol}: {why}, retrying"
    entry, canceled_now = snap["entry"], False
    if not _final(entry):
        # Cancel the unfilled REMAINDER whether or not part of it has filled: Alpaca activates the
        # bracket legs only once the entry is FULLY filled, so a partly filled entry left working
        # holds shares with no stop, and its remainder can keep filling past the rule's window.
        asked = entry.get("status") == "pending_cancel"
        ok = asked or bool(entry.get("id")) and _delete(http, entry["id"])
        c = dict(rec.get("entry_cancel") or {})
        rec = save(entry_cancel={"requested_at": c.get("requested_at") or now,
                                 "tries": int(c.get("tries") or 0) + (0 if asked else 1)})
        snap, why = _snapshot(http, f, rec)                    # fresh: a fill during the cancel counts
        if snap is None:
            return "error", f"{f.symbol}: {why}, retrying"
        entry = snap["entry"]
        if not _final(entry):
            return ("pending" if ok else "error"), (
                f"{f.symbol}: entry cancel {'pending' if ok else 'refused'} ({entry.get('status')}, "
                f"{_filled(entry)} filled), re-checked next tick")
        canceled_now = note["canceled"] = entry.get("status") == "canceled"
    bought = snap["bought"]
    if bought == 0 or entry.get("status") == "filled":
        # Nothing owned, or a FULL fill: the bracket's own legs are live and protect it to 15:30.
        save(entry_cancel_checked=True, protection="none" if bought == 0 else "bracket")
        return ("canceled" if canceled_now else "done"), ""
    # A PARTIAL fill. Its bracket legs never activate, and left alone they could later sell the
    # full requested size: they are cancelled, and a protective exit for exactly the shares owned
    # (an OCO at the forecast's target and stop) takes their place; failing that, it is flattened.
    owned = bought - snap["sold"]
    if _legs_protect(snap["legs"], owned):
        save(entry_cancel_checked=True, protection="bracket", protect_qty=owned)
        return "protected", f"{f.symbol}: {owned} shares protected by the bracket legs"
    live_legs = [g for g in snap["legs"] if not _final(g)]
    if live_legs:
        refused = _cancel_all(http, live_legs)
        snap, why = _snapshot(http, f, rec)
        if snap is None:
            return "error", f"{f.symbol}: {why}, retrying"
        still = [g for g in snap["legs"] if not _final(g)]
        if still:
            return ("error" if refused else "pending"), (
                f"{f.symbol}: bracket leg cancel {'refused' if refused else 'pending'} "
                f"({', '.join(str(g.get('status')) for g in still)}), re-checked next tick")
        owned = bought - snap["sold"]
    live_prot = [o for o in snap["protect"] if not _final(o)]
    live_flat = [o for o in snap["flatten"] if not _final(o)]
    if owned < 0:
        return "alarm", f"{f.symbol}: its exits sold {-owned} shares more than its entry bought -- check it by hand"
    if owned == 0 and not live_prot and not live_flat:
        save(entry_cancel_checked=True, protection="flat")
        return "done", ""
    if live_flat:
        return "pending", f"{f.symbol}: flatten of {owned} shares working, re-checked next tick"
    parents = [o for o in live_prot if str(o.get("client_order_id") or "").startswith(f"{f.forecast_id}-px")]
    if any(_remaining(o) == owned and o.get("status") != "pending_cancel" for o in parents):
        save(entry_cancel_checked=True, protection="oco", protect_qty=owned)
        return "protected", f"{f.symbol}: {owned} shares protected by its OCO exit"
    if live_prot:
        # Working but not sized to what is owned (or already being cancelled): replace it.
        _cancel_all(http, live_prot)
        return "pending", f"{f.symbol}: protective order does not match the {owned} shares owned; replacing"
    tried = list(rec.get("protect_ids") or [])
    # An earlier protective exit the broker refused outright, or that ended without covering the shares.
    failed = bool(rec.get("protect_refused")) or any(_final(o) for o in snap["protect"])
    if not failed and len(tried) < MAX_TRIES:
        cid = f"{f.forecast_id}-px" + (str(len(tried) + 1) if tried else "")
        rec = save(protect_ids=tried + [cid], protect_qty=owned)  # recorded BEFORE the post
        code, accepted = _post(http, oco_exit_request(cid, f.symbol, owned, target=f.target, stop=f.stop))
        if accepted or _transient(code):
            o, _ = _lookup(http, cid)                          # confirm from the broker's own record
            if o is not None and not _final(o) and _remaining(o) == owned:
                save(entry_cancel_checked=True, protection="oco")
                return "protected", f"{f.symbol}: {owned} shares protected by its OCO exit"
            if o is None or not _final(o):
                return "pending", f"{f.symbol}: protective exit for {owned} shares not yet confirmed, re-checked next tick"
        else:
            rec = save(protect_refused=code)                   # a definite refusal is not retried
        # definitely refused, or the broker shows it rejected: flatten below
    return _flatten(http, f, rec, owned, save)


def _flatten(http, f: Forecast, rec: dict, owned: int, save) -> Tuple[str, str]:
    """Protection could not be placed: sell exactly the shares owned at market (-pf, -pf2 ...)."""
    tried = list(rec.get("flatten_ids") or [])
    if len(tried) >= MAX_TRIES:
        return "alarm", (f"{f.symbol}: {owned} shares owned with NO stop after the entry deadline "
                         f"(protective exit failed, {len(tried)} flatten attempts failed) -- close it by hand")
    cid = f"{f.forecast_id}-pf" + (str(len(tried) + 1) if tried else "")
    save(flatten_ids=tried + [cid])                            # recorded BEFORE the post
    code, accepted = _post(http, {"symbol": f.symbol, "qty": str(owned), "side": "sell", "type": "market",
                                  "time_in_force": "day", "client_order_id": cid})
    o, _ = _lookup(http, cid) if (accepted or _transient(code)) else (None, True)
    if o is not None and _filled(o) >= owned:
        save(entry_cancel_checked=True, protection="flattened")
        return "done", f"{f.symbol}: protective exit failed; {owned} shares flattened"
    if o is None and not (accepted or _transient(code)):
        return "alarm", (f"{f.symbol}: {owned} shares owned with NO stop after the entry deadline "
                         "(protective exit and flatten both refused) -- close it by hand")
    return "alarm", (f"{f.symbol}: protective exit failed; flatten of {owned} shares placed, "
                     f"{_filled(o)} confirmed filled ({(o or {}).get('status') or 'no order record'}) "
                     "-- not confirmed flat")


def cancel_unfilled_entries(http, ledger: Ledger, *, day: str, experiments,
                            now: Optional[int] = None) -> Dict[str, Any]:
    """At each forecast's OWN entry expiry (10:30 ET for the morning card, issuance + 20 min
    intraday), the entry's unfilled remainder is cancelled -- filled or not -- and only THIS
    forecast's orders are touched.

    F01 (a partly filled entry used to be left working, permanently marked checked, with its
    filled shares unprotected because Alpaca activates bracket legs only on a FULL fill):
      * the cancel is a request: the entry is re-read until the broker shows it final (canceled /
        filled / expired / rejected); entry_cancel_checked is set only after that and after every
        owned share is protected or flat;
      * a partial fill gets a protective OCO (target + stop) for exactly the filled quantity, read
        back from the broker; if that is refused it is flattened (-pf); if neither can be confirmed
        it is returned under "unprotected" (a PROBLEM alert) and checked again next tick;
      * an entry still unresolved PROTECT_GRACE seconds past its deadline is an alarm too.
    Every step is stored in the edge_paper record (ids recorded before each post), so a re-run tick
    or a restart picks up where the last one stopped and never doubles an order."""
    done, touched, errors, pending, protected, unprotected = [], False, [], [], [], []
    for f in _forecasts(ledger, day, experiments):
        key = f"{f.forecast_id}|paper"
        rec = ledger.store.get("edge_paper", key)
        if not rec or rec.get("state") != "submitted" or rec.get("entry_cancel_checked"):
            continue
        if now is not None and now < f.entry_expiry:
            continue
        box = {"rec": rec}

        def save(_box=box, _key=key, **kv) -> dict:
            _box["rec"] = {**_box["rec"], **kv}
            ledger.store.put("edge_paper", _key, _box["rec"])
            return _box["rec"]

        note = {"canceled": False}
        kind, msg = _protect_entry(http, f, rec, now, save, note)
        if note["canceled"]:
            done.append(f.symbol)
        overdue = now is not None and now - f.entry_expiry >= PROTECT_GRACE
        if kind in ("pending", "error") and overdue:
            kind, msg = "alarm", (f"{msg} -- still unresolved {(now - f.entry_expiry) // 60} min after its entry "
                                  "deadline; any filled shares may have no stop")
        if kind == "alarm":
            msg = f"{msg} ({f.experiment_id})"
            unprotected.append(msg)
            save(protect_alarm=msg, protect_alarm_at=now)
        elif kind == "error":
            errors.append(msg)
        elif kind == "pending":
            pending.append(msg)
        else:
            touched = True
            if kind == "protected":
                protected.append(msg)
    status = "error" if unprotected else ("entries_checked" if touched else
                                          "error" if errors else "pending" if pending else "nothing")
    out = {"status": status, "canceled": done, "errors": errors, "pending": pending,
           "protected": protected, "unprotected": unprotected}
    if unprotected:
        out["error"] = "UNPROTECTED after the entry deadline: " + "; ".join(unprotected)
    return out


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
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = None
        # A 200 that is not an order is an answer we cannot read, never "no such order".
        return (body, True) if isinstance(body, dict) and body else (None, False)
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
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        return None
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
        NOT FLAT error is recorded and returned under "not_flat".
      * (EDGE-04) an ACCEPTED position close is not a filled one: its order is read back by id and
        only its filled_qty counts; a close still working, partly filled or unreadable keeps the
        forecast in flight (never time_exit_done) and raises NOT FLAT on the last tick.
      * (F02) the shares owned are counted only from FRESH broker reads taken AFTER the cancels,
        once the entry and every exit it owns (bracket legs, protective OCO, flatten) is final:
        a fill that lands while the cancel is in flight is counted, never lost. An order still
        working (pending_cancel, a refused cancel, an unknown status) or unreadable keeps the
        forecast in flight, retried next tick, NOT FLAT on the last tick. A 2xx to a cancel is
        the broker taking the request, not proof the order is gone."""
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
        snap, why = _snapshot(http, f, rec)
        if snap is None:
            errors.append(f"{f.symbol}: {why}, retrying")
            if last:
                alarm(f, key, rec, f"{why}; cannot confirm it is flat")
            continue
        working = [o for o in snap["owned"] if not _final(o)]
        if working:
            refused = _cancel_all(http, working)
            snap, why = _snapshot(http, f, rec)       # fresh reads AFTER the cancels (F02)
            if snap is None:
                errors.append(f"{f.symbol}: {why} after cancelling, retrying")
                if last:
                    alarm(f, key, rec, f"{why} after its orders were cancelled; cannot confirm it is flat")
                continue
            still = [o for o in snap["owned"] if not _final(o)]
            if still:
                held = f"{snap['bought'] - snap['sold']} shares owned so far"
                if set(refused) & {str(o.get("id")) for o in still}:
                    errors.append(f"{f.symbol}: cancel failed, retrying")
                    if last:
                        alarm(f, key, rec, f"its bracket orders could not be cancelled ({held}); "
                                           "the position was not closed")
                else:
                    states = ", ".join(f"{o.get('id')} {o.get('status') or 'unknown'}" for o in still)
                    errors.append(f"{f.symbol}: cancel pending ({states}), retrying next tick")
                    if last:
                        alarm(f, key, rec, f"its orders are still working after the cancel ({states}; {held}); "
                                           "cannot confirm it is flat")
                continue
        bought, sold = snap["bought"], snap["sold"]
        tried = list(rec.get("tx_ids") or [])
        filled_tx, working, unsure = 0, False, False
        for cid in tried or [f"{f.forecast_id}-tx"]:
            o, definite = _lookup(http, cid)
            if o is not None:
                filled_tx += int(float(o.get("filled_qty") or 0))
                working = working or not _final(o)          # pending_cancel / unknown: still in flight
                if cid not in tried:
                    tried.append(cid)          # an -tx placed before attempts were recorded
            elif not definite and cid in tried:
                unsure = True
        for x in rec.get("tx_closes") or []:
            # A position close the broker ACCEPTED is not one it FILLED: count only what its own
            # order record says was filled, and keep a still-working close in flight (EDGE-04).
            o = _order_by_id(http, x.get("order_id"))
            if o is None:
                unsure = True
                continue
            filled_tx += int(float(o.get("filled_qty") or 0))
            working = working or not _final(o)
        if unsure:
            errors.append(f"{f.symbol}: broker could not report its exit orders, retrying")
            if last:
                alarm(f, key, rec, "its exit orders could not be read; cannot confirm it is flat")
            continue
        remaining = bought - sold - filled_tx
        if remaining < 0 and not working:
            # Reconciled exposure must be ZERO to be done: more sold than bought is a short.
            errors.append(f"{f.symbol}: its exits sold {-remaining} more shares than its entry bought")
            alarm(f, key, rec, f"its exits sold {-remaining} more shares than its entry bought "
                               "(a short position) -- check it by hand")
            continue
        if remaining == 0 and not working:
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
            rec = {**rec, "tx_closes": list(rec.get("tx_closes") or []) + [{"qty": qty, "order_id": oid, "at": now}]}
            ledger.store.put("edge_paper", key, rec)
            closed.append(f.symbol)
            touched = True
            # Accepted is not filled: confirm from the close order's own record. Anything short of
            # the full quantity filled is NOT FLAT -- the close stays in flight (tx_closes, checked
            # again on any later tick) and the alarm stands until the broker shows it flat.
            o = _order_by_id(http, oid)
            got = int(float((o or {}).get("filled_qty") or 0))
            if got < qty:
                alarm(f, key, rec, f"position close for {qty} shares accepted, {got} confirmed filled "
                                   f"({(o or {}).get('status') or 'no order record'}); not confirmed flat "
                                   "-- check it by hand")
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


def _is_ours(cid: str, forecast_id: str, tag: str) -> bool:
    """OUR numbered ids: <forecast_id>-<tag>, then -<tag>2, -<tag>3 ... for each retried attempt."""
    head = f"{forecast_id}-{tag}"
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
        elif _is_ours(cid, forecast_id, "px"):
            # The protective OCO for a partly filled entry: its parent is the take-profit limit,
            # its leg the stop -- the same roles as the bracket legs it replaces.
            for leg in [o] + list(o.get("legs") or []):
                add(leg, F.TARGET if leg.get("type") == "limit" else F.STOP)
        elif _is_ours(cid, forecast_id, "pf"):
            add(o, F.MANUAL)            # a flatten when protection failed: judged by where it closed
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
        return B.Probe("broker.paper", "alpaca_paper", B.ERROR, note=redact_exc(exc, 120))
    if r.status_code >= 400:
        return B.Probe("broker.paper", "alpaca_paper", B.classify(r.status_code), http_status=r.status_code,
                       note="the paper host refused these keys (live keys are refused here by design)")
    a = r.json() or {}
    blocked = [k for k in ("trading_blocked", "account_blocked", "trade_suspended_by_user") if a.get(k)]
    ok = str(a.get("status") or "").upper() == "ACTIVE" and not blocked
    return B.Probe("broker.paper", "alpaca_paper", B.OK if ok else B.ERROR, http_status=r.status_code, rows=1,
                   note=f"paper account {a.get('status')}" + (f"; blocked: {', '.join(blocked)}" if blocked else ""))
