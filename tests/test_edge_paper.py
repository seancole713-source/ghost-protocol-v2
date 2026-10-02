"""Paper execution: a real broker's records, and no possible path to live money."""
from __future__ import annotations

import copy
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from edge import paper as PP
from edge.contracts import issue
from edge.ledger import Ledger, MemoryStore
from edge.pipeline import EXPERIMENTS, GAP_AND_GO_AUTO

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 23)


def ts(hh, mm):
    return int(datetime(2026, 9, 23, hh, mm, tzinfo=ET).timestamp())


def iso(t):
    return datetime.fromtimestamp(t, tz=ET).isoformat()


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("ALPACA_KEY_ID", "i")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.delenv("EDGE_PAPER_BASE_URL", raising=False)


class R:
    def __init__(self, code=200, body=None):
        self.status_code, self._b = code, body

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


FINAL = ("filled", "canceled", "expired", "rejected")


class Broker:
    """A tiny Alpaca paper account: records every call and serves its orders as FRESH copies (a
    read is a snapshot, never a live view of the broker). DELETE /v2/orders/{id} is modelled: the
    order ends canceled (a bracket parent takes its working legs with it), or filled if the
    shares filling during the cancel complete it. Knobs, by order id:
      fill_on_cancel[id] = n   n more shares fill while the cancel is in flight (the race)
      cancel_mode[id] = "pending" (acknowledged, still pending_cancel until ack(id)) | "refuse"
                        (422, order unchanged) | "error" (no answer) | "ignore" (204, keeps working)"""

    def __init__(self, orders=None, position_qty=0, reject=False):
        self.posts, self.deletes, self.orders = [], [], orders or []
        self.position_qty, self.reject = position_qty, reject
        self.fill_on_cancel, self.cancel_mode = {}, {}

    def post(self, url, json=None, headers=None, timeout=None):
        assert "paper-api.alpaca.markets" in url
        self.posts.append(json)
        if self.reject:
            return R(403, {"message": "insufficient buying power"})
        return R(200, {"id": f"o{len(self.posts)}", **json})

    def _all(self):
        return list(self.orders)

    def _find(self, oid):
        for o in self._all():
            if str(o.get("id")) == oid:
                return o
            for g in o.get("legs") or []:
                if str(g.get("id")) == oid:
                    return g
        return None

    def ack(self, oid):
        """A pending cancel completes."""
        o = self._find(oid)
        o["status"] = "canceled"
        for g in o.get("legs") or []:
            if g.get("status") not in FINAL:
                g["status"] = "canceled"

    def delete(self, url, headers=None, timeout=None):
        self.deletes.append(url)
        oid = url.rsplit("/", 1)[-1]
        mode = self.cancel_mode.get(oid)
        if mode == "error":
            raise ConnectionError("connection reset")
        if mode == "refuse":
            return R(422, {"message": "order is not cancelable"})
        o = self._find(oid)
        if o is None:
            return R(404, {"message": "order not found"})
        if o.get("status") in FINAL:
            return R(422, {"message": "order is not cancelable"})
        n = self.fill_on_cancel.pop(oid, 0)
        if n:                                                  # a fill lands while the cancel is in flight
            o["filled_qty"] = str(int(float(o.get("filled_qty") or 0)) + n)
        if mode == "ignore":
            return R(204)
        if o.get("qty") is not None and int(float(o["filled_qty"])) >= int(float(o["qty"])):
            o["status"] = "filled"                             # the cancel lost the race
            return R(204)
        if mode == "pending":
            o["status"] = "pending_cancel"
            return R(204)
        self.ack(oid)
        return R(204)

    def get(self, url, params=None, headers=None, timeout=None):
        if "by_client_order_id" in url:
            hit = [o for o in self.orders if o.get("client_order_id") == (params or {}).get("client_order_id")]
            return R(200, copy.deepcopy(hit[0])) if hit else R(404, {"message": "order not found"})
        if "/v2/orders/" in url:
            o = self._find(url.rsplit("/", 1)[-1])
            return R(200, copy.deepcopy(o)) if o else R(404, {"message": "order not found"})
        if "/v2/positions/" in url:
            return R(200, {"qty": str(self.position_qty)}) if self.position_qty else R(404, {})
        if (params or {}).get("status") == "open":
            return R(200, [o for o in self.orders if o.get("status") in ("new", "accepted", "held")])
        return R(200, self.orders)


@pytest.fixture
def ledger():
    lg = Ledger(MemoryStore())
    for spec in EXPERIMENTS:
        lg.register(spec, now=ts(8, 0))
    f = issue(GAP_AND_GO_AUTO, symbol="SHOP", session_date=DAY, entry_ref=146.71, issued_at=ts(9, 10))
    lg.record(f, now=ts(9, 11))
    lg.fc = f
    return lg


def test_a_non_paper_host_is_refused_before_anything_is_sent(monkeypatch, ledger):
    monkeypatch.setenv("EDGE_PAPER_BASE_URL", "https://api.alpaca.markets")    # LIVE
    b = Broker()
    with pytest.raises(PP.NotPaper):
        PP.submit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS)
    assert b.posts == []


def test_ghosts_base_url_is_never_read(monkeypatch, ledger):
    monkeypatch.setenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
    assert "paper-api" in PP.base_url()


def test_submit_places_the_frozen_bracket_once(ledger):
    b = Broker()
    out = PP.submit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS)
    assert out["placed"] == ["SHOP"]
    body = b.posts[0]
    f = ledger.fc
    assert body["client_order_id"] == f"{f.forecast_id}-entry"
    assert (body["stop_price"], body["take_profit"]["limit_price"], body["stop_loss"]["stop_price"]) == (
        f"{f.entry_trigger:.2f}", f"{f.target:.2f}", f"{f.stop:.2f}")
    assert PP.submit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS)["status"] == "nothing_to_submit"
    assert len(b.posts) == 1


def test_a_broker_refusal_becomes_a_rejected_entry_in_the_actual_record(ledger):
    PP.submit(Broker(reject=True), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    out = PP.reconcile(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(16, 25))
    assert out["settled"][f"gap_and_go_auto@v1:SHOP"]["actual"] == "NO_FILL"


def test_a_broker_refusal_is_a_loud_problem_once_not_a_quiet_no_fill(ledger, monkeypatch):
    # Audit 2026-09-25 U60: a paper account that stops accepting orders sent no alert.
    from edge import pipeline as P
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    class TG:
        def __init__(self):
            self.sent = []

        def post(self, url, json=None, timeout=None):
            self.sent.append(json["text"])
            return R(200, {})

    tg, b = TG(), Broker(reject=True)
    out = P.run(None, ledger, now=ts(9, 10), http=b, notifier=tg)
    assert out["paper_submit"]["refused"] == ["SHOP: HTTP 403 insufficient buying power"]
    assert out["paper_submit_refused"]["status"] == "error"
    refused = [t for t in tg.sent if t.startswith("PROBLEM") and "REFUSED" in t]
    assert len(refused) == 1 and "insufficient buying power" in refused[0]
    P.run(None, ledger, now=ts(9, 15), http=b, notifier=tg)           # recorded rejected: never re-posted
    assert len(b.posts) == 1 and len([t for t in tg.sent if "REFUSED" in t]) == 1


def test_a_transient_broker_failure_retries_without_a_refusal_alert(ledger):
    class Busy(Broker):
        def post(self, url, json=None, headers=None, timeout=None):
            self.posts.append(json)
            return R(429, {"message": "rate limit"})

    out = PP.submit(Busy(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    assert out["refused"] == [] and "retrying next tick" in out["errors"][0]


def test_unfilled_entry_is_cancelled_at_1030_and_a_filled_one_is_not(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Broker(orders=[{"id": "o1", "client_order_id": f"{f.forecast_id}-entry", "status": "new", "filled_qty": "0"}])
    PP.cancel_unfilled_entries(b, ledger, day="2026-09-23", experiments=EXPERIMENTS)
    assert b.deletes == ["https://paper-api.alpaca.markets/v2/orders/o1"]


def test_time_exit_cancels_legs_first_then_sells_what_is_open(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Broker(orders=[{"id": "o1", "client_order_id": f"{f.forecast_id}-entry", "status": "filled",
                        "filled_qty": "6", "legs": [{"id": "leg-tp", "status": "new", "filled_qty": "0"},
                                                    {"id": "leg-sl", "status": "held", "filled_qty": "0"}]}])
    out = PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS)
    assert len(b.deletes) == 2 and out["closed"] == ["SHOP"]
    assert b.posts[-1]["client_order_id"] == f"{f.forecast_id}-tx" and b.posts[-1]["type"] == "market"
    assert b.posts[-1]["qty"] == "6"


def test_reconcile_uses_the_real_fill_prices_not_the_levels(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    orders = [{
        "id": "o1", "client_order_id": f"{f.forecast_id}-entry", "status": "filled",
        "submitted_at": iso(ts(9, 11)), "filled_at": iso(ts(9, 31)),
        "filled_qty": str(f.shares), "filled_avg_price": "148.30",
        "legs": [
            {"id": "tp", "type": "limit", "status": "canceled", "submitted_at": iso(ts(9, 31)),
             "canceled_at": iso(ts(10, 2)), "filled_qty": "0"},
            {"id": "sl", "type": "stop", "status": "filled", "submitted_at": iso(ts(9, 31)),
             "filled_at": iso(ts(10, 2)), "filled_qty": str(f.shares), "filled_avg_price": "143.10"},
        ]}]
    out = PP.reconcile(Broker(orders=orders), ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(16, 25))
    row = out["settled"]["gap_and_go_auto@v1:SHOP"]
    assert row["actual"] == "LOSS"
    assert row["pnl_usd"] == pytest.approx(f.shares * (143.10 - 148.30))      # slipped past the 143.74 stop
    rep = ledger.report("gap_and_go_auto@v1")
    assert rep["records"]["actual"]["by_outcome"] == {"LOSS": 1}


def test_a_timed_out_post_the_broker_accepted_is_submitted_not_rejected(ledger):
    f = ledger.fc

    class TimesOut(Broker):
        def post(self, url, json=None, headers=None, timeout=None):
            self.posts.append(json)
            raise TimeoutError("read timed out")

    b = TimesOut(orders=[{"id": "o9", "client_order_id": f"{f.forecast_id}-entry", "status": "new"}])
    PP.submit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS)
    rec = ledger.store.get("edge_paper", f"{f.forecast_id}|paper")
    assert rec["state"] == "submitted" and rec["order_id"] == "o9"


def test_a_transient_failure_records_nothing_and_retries(ledger):
    f = ledger.fc

    class Busy(Broker):
        def post(self, url, json=None, headers=None, timeout=None):
            self.posts.append(json)
            return R(503, {"message": "try later"})

    out = PP.submit(Busy(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    assert ledger.store.get("edge_paper", f"{f.forecast_id}|paper") is None and "retrying" in out["errors"][0]
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    assert ledger.store.get("edge_paper", f"{f.forecast_id}|paper")["state"] == "submitted"


def test_two_forecasts_on_one_symbol_each_sell_only_their_own_shares(ledger):
    from edge.pipeline import GAP_BASELINE
    f1 = ledger.fc
    f2 = issue(GAP_BASELINE, symbol="SHOP", session_date=DAY, entry_ref=146.71, issued_at=ts(9, 10))
    ledger.record(f2, now=ts(9, 11))
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    orders = [{"id": "a", "client_order_id": f"{f1.forecast_id}-entry", "status": "filled", "filled_qty": "6",
               "legs": [{"id": "a-tp", "status": "new", "filled_qty": "0"}]},
              {"id": "b", "client_order_id": f"{f2.forecast_id}-entry", "status": "filled", "filled_qty": "6",
               "legs": [{"id": "b-tp", "status": "filled", "filled_qty": "6"}]}]      # b already hit target
    b = Broker(orders=orders)
    out = PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS)
    sells = [p for p in b.posts if p.get("side") == "sell"]
    assert [p["qty"] for p in sells] == ["6"] and sells[0]["client_order_id"] == f"{f1.forecast_id}-tx"
    assert out["closed"] == ["SHOP"]


def test_a_refused_exit_sell_is_retried_not_marked_done(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)

    class Held(Broker):
        def post(self, url, json=None, headers=None, timeout=None):
            self.posts.append(json)
            return R(403, {"message": "insufficient qty available for order (held_for_orders)"})

    orders = [{"id": "o1", "client_order_id": f"{f.forecast_id}-entry", "status": "filled", "filled_qty": "6",
               "legs": [{"id": "tp", "status": "pending_cancel", "filled_qty": "0"}]}]
    out = PP.time_exit(Held(orders=orders), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    assert "retrying" in out["errors"][0]
    assert not ledger.store.get("edge_paper", f"{f.forecast_id}|paper").get("time_exit_done")


# ------------------------------------------------- time-exit safety (audit 2026-09-25) --

class Exchange(Broker):
    """The Broker fake with memory: a sell it accepts becomes an order the next lookup sees,
    with `sell_status` (filled / rejected / new). `refuse_sells` refuses that many sell posts;
    `close_ok` answers DELETE /v2/positions; GET /v2/orders/{id} serves the nested bracket."""

    def __init__(self, orders=None, position_qty=0, sell_status="filled", refuse_sells=0, close_ok=True,
                 nested=None, close_status="filled", oco_status="new", refuse_oco=None, oco_timeout=False):
        super().__init__(orders=orders, position_qty=position_qty)
        self.sell_status, self.refuse_sells, self.close_ok = sell_status, refuse_sells, close_ok
        self.nested, self.closes, self.ids_seen = nested or {}, [], set()
        self.close_status = close_status          # what GET /v2/orders/close1 then reports
        # A protective OCO post: accepted as `oco_status`, refused with HTTP `refuse_oco`, or accepted
        # and then the answer lost (`oco_timeout`).
        self.oco_status, self.refuse_oco, self.oco_timeout = oco_status, refuse_oco, oco_timeout

    def _all(self):
        return list(self.orders) + list(self.nested.values())

    def _oco(self, json):
        n = len(self.posts)
        o = {"id": f"p{n}", "client_order_id": json["client_order_id"], "order_class": "oco", "side": "sell",
             "type": "limit", "limit_price": json["take_profit"]["limit_price"], "qty": json["qty"],
             "status": self.oco_status, "filled_qty": "0",
             "legs": [{"id": f"p{n}-sl", "order_class": "oco", "side": "sell", "type": "stop",
                       "stop_price": json["stop_loss"]["stop_price"], "qty": json["qty"],
                       "status": "canceled" if self.oco_status in FINAL else "held", "filled_qty": "0"}]}
        self.orders.append(o)
        if self.oco_timeout:
            raise TimeoutError("read timed out")              # the broker took it; the answer was lost
        return R(200, o)

    def post(self, url, json=None, headers=None, timeout=None):
        assert "paper-api.alpaca.markets" in url
        self.posts.append(json)
        cid = json["client_order_id"]
        if cid in self.ids_seen:                              # Alpaca: client_order_id must be unique
            return R(422, {"message": "client_order_id must be unique"})
        if json.get("order_class") == "oco" and self.refuse_oco:
            return R(self.refuse_oco, {"message": "stop price must be below the market"})
        self.ids_seen.add(cid)
        if json.get("order_class") == "oco":
            return self._oco(json)
        if json.get("side") == "sell" and self.refuse_sells:
            self.refuse_sells -= 1
            return R(403, {"message": "insufficient qty available for order (held_for_orders)"})
        o = {"id": f"s{len(self.posts)}", "client_order_id": cid, "status": self.sell_status,
             "filled_qty": json["qty"] if self.sell_status == "filled" else "0"}
        self.orders.append(o)
        return R(200, o)

    def delete(self, url, headers=None, timeout=None):
        if "/v2/positions/" not in url:
            return super().delete(url, headers=headers, timeout=timeout)
        self.deletes.append(url)
        self.closes.append(url)
        if not self.close_ok:
            return R(403, {"message": "held"})
        qty, oid = url.rsplit("qty=", 1)[-1], f"close{len(self.closes)}"
        self.nested[oid] = {"id": oid, "symbol": "SHOP", "side": "sell", "qty": qty,
                            "status": self.close_status,
                            "filled_qty": qty if self.close_status == "filled" else "0"}
        return R(200, {"id": oid, "symbol": "SHOP", "status": "accepted"})

    def get(self, url, params=None, headers=None, timeout=None):
        tail = url.rsplit("/v2/orders/", 1)[-1] if "/v2/orders/" in url else None
        if tail and tail in self.nested:
            assert (params or {}).get("nested") == "true"
            return R(200, copy.deepcopy(self.nested[tail]))
        return super().get(url, params=params, headers=headers, timeout=timeout)


def _filled_entry(f, legs=None, qty="6"):
    return {"id": "o1", "client_order_id": f"{f.forecast_id}-entry", "status": "filled", "filled_qty": qty,
            "legs": [{"id": "leg-tp", "status": "canceled", "filled_qty": "0"},
                     {"id": "leg-sl", "status": "canceled", "filled_qty": "0"}] if legs is None else legs}


def test_a_rejected_exit_sell_is_retried_under_a_new_client_id(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Exchange(orders=[_filled_entry(f)], sell_status="rejected")
    PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(15, 30))
    b.sell_status = "filled"
    out = PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(15, 35))
    sells = [p["client_order_id"] for p in b.posts if p.get("side") == "sell"]
    assert sells == [f"{f.forecast_id}-tx", f"{f.forecast_id}-tx2"] and out["closed"] == ["SHOP"]
    PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(15, 40))
    rec = ledger.store.get("edge_paper", f"{f.forecast_id}|paper")
    assert rec["time_exit_done"] and rec["tx_ids"] == sells
    assert len([p for p in b.posts if p.get("side") == "sell"]) == 2              # flat: no third sell


def test_a_working_exit_sell_is_never_doubled(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Exchange(orders=[_filled_entry(f)], sell_status="new")
    PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(15, 30))
    PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(15, 35))
    assert len([p for p in b.posts if p.get("side") == "sell"]) == 1
    assert not ledger.store.get("edge_paper", f"{f.forecast_id}|paper").get("time_exit_done")


def test_legs_missing_from_the_client_id_answer_are_read_by_order_id_and_cancelled(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    legs = [{"id": "leg-tp", "status": "new", "filled_qty": "0"},
            {"id": "leg-sl", "status": "held", "filled_qty": "0"}]
    b = Exchange(orders=[_filled_entry(f, legs=[])], nested={"o1": {**_filled_entry(f), "legs": legs}})
    out = PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(15, 30))
    assert [u.rsplit("/", 1)[-1] for u in b.deletes] == ["leg-tp", "leg-sl"]      # cancelled BEFORE the sell
    assert out["closed"] == ["SHOP"] and b.posts[-1]["qty"] == "6"


def test_the_last_tick_closes_only_this_forecasts_shares_when_the_sell_is_refused(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Exchange(orders=[_filled_entry(f)], refuse_sells=99, position_qty=10)     # 4 more belong to another
    out = PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(15, 45))
    assert "retrying" in out["errors"][0] and b.closes == []                     # not the last tick yet
    out = PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(15, 50))
    assert b.closes == ["https://paper-api.alpaca.markets/v2/positions/SHOP?qty=6"]
    assert out["closed"] == ["SHOP"] and out["not_flat"] == [] and out["status"] == "time_exit"
    sells = [p["client_order_id"] for p in b.posts if p.get("side") == "sell"]
    assert sells == [f"{f.forecast_id}-tx", f"{f.forecast_id}-tx2"]              # each attempt its own id
    rec = ledger.store.get("edge_paper", f"{f.forecast_id}|paper")
    assert rec["tx_closes"][0]["qty"] == 6 and rec["tx_closes"][0]["order_id"] == "close1"
    # after the close, the broker's close order is this forecast's time exit in the actual record
    orders = [{**_filled_entry(f), "submitted_at": iso(ts(9, 11)), "filled_at": iso(ts(9, 40)),
               "filled_avg_price": "148.30"},
              {"id": "close1", "client_order_id": "alpaca-generated", "symbol": "SHOP", "side": "sell",
               "status": "filled", "filled_qty": "6", "filled_avg_price": "150.00",
               "submitted_at": iso(ts(15, 50)), "filled_at": iso(ts(15, 50))}]
    res = PP.reconcile(Broker(orders=orders), ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(16, 25))
    assert res["settled"]["gap_and_go_auto@v1:SHOP"]["actual"] == "TIME_EXIT"
    assert "close1" in [o["id"] for o in ledger.store.get("edge_paper_orders", "2026-09-23")["orders"]]


def test_an_accepted_position_close_is_not_flat_until_its_order_shows_the_fill(ledger):
    """EDGE-04: the fallback close's accepted quantity used to count as sold, so the forecast was
    marked time_exit_done without the broker ever showing a fill. The close order is read back;
    while it is working the forecast stays in flight and NOT FLAT is raised."""
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Exchange(orders=[_filled_entry(f)], refuse_sells=99, position_qty=6, close_status="accepted")
    out = PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(15, 50))
    assert b.closes == ["https://paper-api.alpaca.markets/v2/positions/SHOP?qty=6"]
    assert out["status"] == "error" and "6 shares accepted, 0 confirmed filled (accepted)" in out["not_flat"][0]
    rec = ledger.store.get("edge_paper", f"{f.forecast_id}|paper")
    assert rec["tx_closes"][0]["order_id"] == "close1" and not rec.get("time_exit_done")
    assert "not confirmed flat" in rec["exit_alarm"]
    # a later check: still working -> still in flight, never a second close on top of it
    out = PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(15, 54))
    assert b.closes == ["https://paper-api.alpaca.markets/v2/positions/SHOP?qty=6"] and out["not_flat"]
    assert not ledger.store.get("edge_paper", f"{f.forecast_id}|paper").get("time_exit_done")
    # partly filled and done: 2 of 6 sold, 4 still open -> not flat, a close for the 4 remaining
    b.nested["close1"] = {**b.nested["close1"], "status": "canceled", "filled_qty": "2"}
    b.close_status = "filled"
    out = PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(15, 54))
    assert b.closes[-1].endswith("?qty=4")
    # the broker now shows every share sold: flat, done, nothing more sent
    PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(15, 54))
    rec = ledger.store.get("edge_paper", f"{f.forecast_id}|paper")
    assert rec["time_exit_done"] and len(b.closes) == 2


def test_an_unreadable_close_order_is_never_counted_as_filled(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Exchange(orders=[_filled_entry(f)], refuse_sells=99, position_qty=6)
    ledger.store.put("edge_paper", f"{f.forecast_id}|paper", {
        **ledger.store.get("edge_paper", f"{f.forecast_id}|paper"), "tx_ids": [f"{f.forecast_id}-tx"],
        "tx_closes": [{"qty": 6, "order_id": None, "at": ts(15, 50)}]})
    out = PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(15, 54))
    assert out["not_flat"] and "cannot confirm it is flat" in out["not_flat"][0]
    assert not ledger.store.get("edge_paper", f"{f.forecast_id}|paper").get("time_exit_done")


def test_the_last_tick_never_closes_more_than_the_account_holds(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Exchange(orders=[_filled_entry(f)], refuse_sells=99, position_qty=4)
    PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(15, 50))
    assert b.closes == ["https://paper-api.alpaca.markets/v2/positions/SHOP?qty=4"]


def test_not_flat_after_the_last_tick_is_a_loud_problem(ledger, monkeypatch):
    from edge import pipeline as P
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Exchange(orders=[_filled_entry(f)], refuse_sells=99, position_qty=6, close_ok=False)

    class TG:
        def __init__(self):
            self.sent = []

        def post(self, url, json=None, timeout=None):
            self.sent.append(json["text"])
            return R(200, {})

    tg = TG()
    P.run(None, ledger, now=ts(15, 30), http=b, notifier=tg)          # refused: an ordinary retry PROBLEM
    out = P.run(None, ledger, now=ts(15, 50), http=b, notifier=tg)
    assert out["paper_exit"]["status"] == "error" and "NOT FLAT" in out["paper_exit"]["error"]
    assert "6 shares still open with NO stop" in out["paper_exit_not_flat"]["error"]
    assert any("paper_exit_not_flat" in t and "NOT FLAT" in t for t in tg.sent)  # its own message, not swallowed
    rec = ledger.store.get("edge_paper", f"{f.forecast_id}|paper")
    assert "NO stop" in rec["exit_alarm"] and not rec.get("time_exit_done")


def test_retried_time_exit_ids_keep_the_time_exit_role():
    from edge import broker_alpaca as BA, fills as FL
    assert BA.role_of("abc-tx") == BA.role_of("abc-tx2") == BA.role_of("abc-tx13") == FL.TIME_EXIT_ROLE
    assert PP._is_time_exit("abc-tx3", "abc") and not PP._is_time_exit("abc-txx", "abc")


def test_a_broker_fill_the_rule_never_took_is_shown_but_not_counted(ledger):
    from edge.resolver import Resolution
    f = ledger.fc
    ledger.settle(f.forecast_id, Resolution("NO_FILL", resolver_version="resolver_v2"), now=ts(16, 25), record="forecast")
    ledger.settle(f.forecast_id, Resolution("NO_FILL", resolver_version="resolver_v2"), now=ts(16, 25), record="simulated")
    ledger.settle(f.forecast_id, Resolution("WIN", entry_fill=5.44, exit_price=5.78, pnl_usd=62.22),
                  now=ts(16, 25), record="actual")
    act = ledger.report("gap_and_go_auto@v1")["records"]["actual"]
    assert act["by_outcome"] == {"OUTSIDE_RULE": 1} and act["filled"] == 0 and act["wins"] == 0


def test_the_minute_guard_cancels_right_after_the_entry_window(ledger, monkeypatch):
    from edge import pipeline as P
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Broker(orders=[{"id": "o1", "client_order_id": f"{f.forecast_id}-entry", "status": "new", "filled_qty": "0"}])
    assert P.paper_guard(b, ledger, now=f.entry_expiry - 30)["status"] in ("nothing", "entries_checked") and not b.deletes
    P.paper_guard(b, ledger, now=f.entry_expiry + 30)
    assert b.deletes == ["https://paper-api.alpaca.markets/v2/orders/o1"]


def test_reconcile_keeps_the_brokers_order_record(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    orders = [{"id": "o1", "client_order_id": f"{f.forecast_id}-entry", "status": "canceled", "stop_price": "148.18",
               "limit_price": "149.65", "filled_qty": "0", "submitted_at": iso(ts(9, 11)), "canceled_at": iso(ts(10, 30))},
              {"id": "zz", "client_order_id": "someone-else", "status": "filled"}]
    PP.reconcile(Broker(orders=orders), ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(16, 25))
    kept = ledger.store.get("edge_paper_orders", "2026-09-23")["orders"]
    assert [o["id"] for o in kept] == ["o1"] and kept[0]["limit_price"] == "149.65"
    from edge import readout as RO
    row = RO.view(ledger.store, "paper", "2026-09-23")["orders"][0]
    assert row["broker_entry"]["status"] == "canceled" and row["filled_after_window"] is False


def test_a_paired_v2_forecast_places_no_order_and_is_graded_from_v1s_order():
    """2026-09-25: BENF and AESI were each bought twice, once for intraday_continuation v1 and once
    for its paired v2 (same stock, moment and levels). Only v1 places an order now; v2's actual
    record is read from v1's broker order, so both records still show the real fill."""
    from edge import intraday as I
    from edge.contracts import issue_intraday
    lg = Ledger(MemoryStore())
    for spec in I.INTRADAY_SPECS:
        lg.register(spec, now=ts(8, 0))
    assert I.INTRADAY_CONTINUATION_V2 not in I.PAPER_SPECS and I.INTRADAY_CONTINUATION in I.PAPER_SPECS
    v1, v2 = (issue_intraday(sp, symbol="BENF", session_date=DAY, entry_ref=2.12, issued_at=ts(10, 26))
              for sp in (I.INTRADAY_CONTINUATION, I.INTRADAY_CONTINUATION_V2))
    lg.record(v2, now=ts(10, 26))
    lg.record(v1, now=ts(10, 26))
    b = Broker()
    out = PP.submit(b, lg, day="2026-09-23", experiments=I.PAPER_SPECS)
    assert out["placed"] == ["BENF"] and len(b.posts) == 1
    assert b.posts[0]["client_order_id"] == f"{v1.forecast_id}-entry"
    orders = [{
        "id": "o1", "client_order_id": f"{v1.forecast_id}-entry", "status": "filled",
        "submitted_at": iso(ts(10, 26)), "filled_at": iso(ts(10, 30)),
        "filled_qty": str(v1.shares), "filled_avg_price": f"{v1.entry_trigger:.2f}",
        "legs": [
            {"id": "tp", "type": "limit", "status": "canceled", "submitted_at": iso(ts(10, 30)),
             "canceled_at": iso(ts(10, 50)), "filled_qty": "0"},
            {"id": "sl", "type": "stop", "status": "filled", "submitted_at": iso(ts(10, 30)),
             "filled_at": iso(ts(10, 50)), "filled_qty": str(v1.shares), "filled_avg_price": f"{v1.stop:.2f}"},
        ]}]
    res = PP.reconcile(Broker(orders=orders), lg, day="2026-09-23", experiments=I.INTRADAY_SPECS, now=ts(16, 25))
    s1 = res["settled"][f"{I.INTRADAY_CONTINUATION.experiment_id}:BENF"]
    s2 = res["settled"][f"{I.INTRADAY_CONTINUATION_V2.experiment_id}:BENF"]
    assert s1["actual"] == s2["actual"] == "LOSS" and s1["pnl_usd"] == s2["pnl_usd"]
    # a lone forecast with no order and no twin keeps the old answer: unresolved, never a loss
    lone = issue_intraday(I.INTRADAY_CONTINUATION_V2, symbol="AESI", session_date=DAY, entry_ref=12.8,
                          issued_at=ts(10, 41))
    lg.record(lone, now=ts(10, 41))
    res = PP.reconcile(Broker(orders=orders), lg, day="2026-09-23", experiments=I.INTRADAY_SPECS, now=ts(16, 30))
    assert res["settled"][f"{I.INTRADAY_CONTINUATION_V2.experiment_id}:AESI"]["actual"] == "UNRESOLVED"


def test_iex_and_sip_records_are_never_pooled():
    """Audit 2026-09-25: the feed was in no spec or report, so Monday's SIP forecasts would have
    joined the IEX record under the same experiment. The headline record (what promotion reads)
    is now the current regime only -- SIP once any SIP forecast exists -- and IEX sits beside it."""
    from edge.resolver import Resolution
    lg = Ledger(MemoryStore())
    for spec in EXPERIMENTS:
        lg.register(spec, now=ts(8, 0))
    eid = GAP_AND_GO_AUTO.experiment_id
    old = issue(GAP_AND_GO_AUTO, symbol="SHOP", session_date=DAY, entry_ref=146.71, issued_at=ts(9, 10))
    lg.record(old, now=ts(9, 11))
    lg.settle(old.forecast_id, Resolution("WIN", pnl_usd=50.0, resolver_version="resolver_v2"), now=ts(16, 25))
    rep = lg.report(eid)
    assert rep["feed_regime"] == "iex" and rep["records"]["simulated"]["wins"] == 1 and "other_regimes" not in rep

    monday = date(2026, 9, 28)
    lg.store.put("edge_cards", monday.isoformat(), {"day": monday.isoformat(), "live_feed": "sip"})
    new = issue(GAP_AND_GO_AUTO, symbol="GLND", session_date=monday, entry_ref=6.1,
                issued_at=int(datetime(2026, 9, 28, 9, 10, tzinfo=ET).timestamp()))
    lg.record(new, now=int(datetime(2026, 9, 28, 9, 11, tzinfo=ET).timestamp()))
    lg.settle(new.forecast_id, Resolution("LOSS", pnl_usd=-30.0, resolver_version="resolver_v2"),
              now=int(datetime(2026, 9, 28, 16, 25, tzinfo=ET).timestamp()))
    rep = lg.report(eid)
    assert rep["feed_regime"] == "sip" and rep["forecasts_in_regime"] == 1
    assert rep["records"]["simulated"]["filled"] == 1 and rep["records"]["simulated"]["wins"] == 0
    assert rep["other_regimes"]["iex"]["simulated"]["wins"] == 1


# --------------------------------------------- late broker fill (audit 2026-09-25) --

def _late_fill_case(fill_hm):
    """Intraday forecast, entry window 09:10-09:30. At 09:29 the trigger is touched but the bar runs
    straight past the limit: the MARKET record takes the trade (WIN), the simulated order rests
    unfilled (NO_FILL). The broker fills at `fill_hm`, then its target leg fills."""
    from edge.intraday import INTRADAY_CONTINUATION
    from edge.contracts import issue_intraday
    from edge.resolver import resolve_execution, resolve_market
    lg = Ledger(MemoryStore())
    lg.register(INTRADAY_CONTINUATION, now=ts(8, 0))
    f = issue_intraday(INTRADAY_CONTINUATION, symbol="WHLR", session_date=DAY, entry_ref=5.40, issued_at=ts(9, 9))
    lg.record(f, now=ts(9, 9))
    assert (f.window_start, f.entry_expiry) == (ts(9, 10), ts(9, 30))
    tape = [(ts(9, m), 5.36, 5.38, 5.35, 5.37, 1000) for m in range(10, 29)]
    tape.append((ts(9, 29), f.entry_trigger - 0.02, f.target + 0.02, f.entry_trigger - 0.03,
                 f.entry_limit + 0.10, 9000))
    tape += [(ts(9, 30 + m), f.entry_limit + 0.05, f.target + 0.05, f.entry_limit - 0.01, f.target, 5000)
             for m in range(5)]
    tape.append((ts(15, 30), f.target, f.target, f.target, f.target, 100))
    m, x = resolve_market(f, tape), resolve_execution(f, tape)
    assert m.outcome == "WIN" and x.outcome == "NO_FILL"          # the rule's records disagree by design
    lg.settle(f.forecast_id, m, now=ts(16, 20), record="forecast")
    lg.settle(f.forecast_id, x, now=ts(16, 20), record="simulated")
    fh, fm = fill_hm
    orders = [{"id": "w1", "client_order_id": f"{f.forecast_id}-entry", "symbol": "WHLR", "status": "filled",
               "submitted_at": iso(ts(9, 9)), "filled_at": iso(ts(fh, fm)),
               "filled_qty": str(f.shares), "filled_avg_price": f"{f.entry_limit:.2f}",
               "legs": [{"id": "w1-tp", "type": "limit", "status": "filled", "submitted_at": iso(ts(fh, fm)),
                         "filled_at": iso(ts(9, 44)), "filled_qty": str(f.shares),
                         "filled_avg_price": f"{f.target:.2f}"},
                        {"id": "w1-sl", "type": "stop", "status": "canceled", "submitted_at": iso(ts(fh, fm)),
                         "canceled_at": iso(ts(9, 44)), "filled_qty": "0"}]}]
    out = PP.reconcile(Broker(orders=orders), lg, day="2026-09-23", experiments=(INTRADAY_CONTINUATION,),
                       now=ts(16, 25))
    return lg, f, out


def test_a_broker_fill_after_the_entry_window_is_outside_the_rule_whatever_the_simulation_says():
    lg, f, out = _late_fill_case((9, 31))           # 09:31 fill, 09:30 expiry
    settled = out["settled"]["intraday_continuation@v1:WHLR"]
    assert settled["actual"] == "WIN"                                               # what the broker did...
    assert any("AFTER THE ENTRY WINDOW" in w for w in settled["warnings"])
    act = lg.report("intraday_continuation@v1")["records"]["actual"]
    assert act["by_outcome"] == {"OUTSIDE_RULE": 1}                                 # ...is never counted
    assert act["filled"] == 0 and act["wins"] == 0 and act["verdict"] == "no filled trades yet"
    from edge import readout as RO
    row = RO.view(lg.store, "paper", "2026-09-23")["orders"][0]
    assert row["filled_after_window"] is True and row["actual_counted"] is False
    assert "after the entry window" in row["note"]


def test_the_same_trade_filled_inside_the_window_is_counted():
    lg, f, out = _late_fill_case((9, 29))
    act = lg.report("intraday_continuation@v1")["records"]["actual"]
    assert act["by_outcome"] == {"WIN": 1} and act["filled"] == 1
    from edge import readout as RO
    row = RO.view(lg.store, "paper", "2026-09-23")["orders"][0]
    assert row["filled_after_window"] is False and row["actual_counted"] is True and row["note"] is None


def test_an_older_actual_row_without_a_fill_time_is_judged_on_the_kept_broker_record(ledger):
    from edge.resolver import Resolution
    f = ledger.fc                                    # entry window ends 10:30
    ledger.settle(f.forecast_id, Resolution("WIN", pnl_usd=40.0, resolver_version="resolver_v2"), now=ts(16, 20), record="forecast")
    ledger.settle(f.forecast_id, Resolution("WIN", entry_fill=148.3, exit_price=155.0, pnl_usd=40.2),
                  now=ts(16, 25), record="actual")                 # no entry_ts: settled before the fix
    assert ledger.report("gap_and_go_auto@v1")["records"]["actual"]["by_outcome"] == {"WIN": 1}
    ledger.store.put("edge_paper_orders", "2026-09-23", {"day": "2026-09-23", "orders": [
        {"id": "o1", "client_order_id": f"{f.forecast_id}-entry", "status": "filled", "filled_qty": "6",
         "filled_at": iso(ts(10, 34)), "legs": []}]})
    act = ledger.report("gap_and_go_auto@v1")["records"]["actual"]
    assert act["by_outcome"] == {"OUTSIDE_RULE": 1} and act["filled"] == 0
    from edge import readout as RO
    row = RO.view(ledger.store, "paper", "2026-09-23")["orders"][0]
    assert row["filled_after_window"] is True and row["actual_counted"] is False


def test_a_broker_rejected_entry_order_settles_as_an_actual_no_fill(ledger):
    f = ledger.fc
    orders = [{"id": "o1", "client_order_id": f"{f.forecast_id}-entry", "status": "rejected",
               "submitted_at": iso(ts(9, 11)), "failed_at": iso(ts(9, 11)), "filled_qty": "0",
               "reject_reason": "asset not tradable"}]
    out = PP.reconcile(Broker(orders=orders), ledger, day="2026-09-23", experiments=EXPERIMENTS, now=ts(16, 25))
    assert out["settled"]["gap_and_go_auto@v1:SHOP"]["actual"] == "NO_FILL"
    o = ledger.store.get("outcomes", f"{f.forecast_id}|actual")
    assert o["outcome"] == "NO_FILL" and o["note"] == "entry_rejected"
    assert ledger.report("gap_and_go_auto@v1")["records"]["actual"]["filled"] == 0


# ------------------------ F01: a partly filled entry at its deadline / F02: cancel-fill race --

def _partial_entry(f, filled="5", qty="99", status="partially_filled"):
    """A bracket entry part-filled at its deadline: both legs still wait 'held' -- Alpaca
    activates a bracket's legs only once its entry is FULLY filled."""
    return {"id": "o1", "client_order_id": f"{f.forecast_id}-entry", "symbol": "SHOP", "side": "buy",
            "type": "stop_limit", "order_class": "bracket", "qty": qty, "status": status, "filled_qty": filled,
            "legs": [{"id": "leg-tp", "type": "limit", "side": "sell", "qty": qty, "status": "held",
                      "filled_qty": "0"},
                     {"id": "leg-sl", "type": "stop", "side": "sell", "qty": qty, "status": "held",
                      "filled_qty": "0"}]}


def _cancel(b, lg, hm):
    return PP.cancel_unfilled_entries(b, lg, day="2026-09-23", experiments=EXPERIMENTS, now=ts(*hm))


def _exit(b, lg, hm):
    return PP.time_exit(b, lg, day="2026-09-23", experiments=EXPERIMENTS, now=ts(*hm))


def _rec(lg):
    return lg.store.get("edge_paper", f"{lg.fc.forecast_id}|paper")


def _ocos(b):
    return [p for p in b.posts if p.get("order_class") == "oco"]


def _market_sells(b):
    return [p for p in b.posts if p.get("side") == "sell" and p.get("type") == "market"]


class TG:
    def __init__(self):
        self.sent = []

    def post(self, url, json=None, timeout=None):
        self.sent.append(json["text"])
        return R(200, {})


def test_a_partly_filled_entry_is_cancelled_at_its_deadline_and_its_shares_protected(ledger):
    """F01: 5 of 99 filled at the deadline. The old step cancelled only an entry with NOTHING filled,
    marked this one checked for good, and left the 94 working -- while the 5 had no stop."""
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Exchange(orders=[_partial_entry(f)])
    assert _cancel(b, ledger, (10, 29))["status"] == "nothing" and b.deletes == []     # before its deadline
    out = _cancel(b, ledger, (10, 30))
    assert b.deletes == ["https://paper-api.alpaca.markets/v2/orders/o1"]               # the unfilled remainder
    assert b._find("o1")["status"] == "canceled"
    (oco,) = _ocos(b)
    assert (oco["qty"], oco["side"], oco["client_order_id"]) == ("5", "sell", f"{f.forecast_id}-px")
    assert (oco["take_profit"]["limit_price"], oco["stop_loss"]["stop_price"]) == (f"{f.target:.2f}",
                                                                                    f"{f.stop:.2f}")
    assert out["status"] == "entries_checked" and out["canceled"] == ["SHOP"] and out["unprotected"] == []
    assert "5 shares protected by its OCO exit" in out["protected"][0]
    rec = _rec(ledger)
    assert rec["entry_cancel_checked"] and rec["protection"] == "oco" and rec["protect_qty"] == 5
    assert _cancel(b, ledger, (10, 31))["status"] == "nothing"                         # idempotent
    assert len(_ocos(b)) == 1 and len(b.deletes) == 1
    # 15:30: the OCO is cancelled and confirmed gone, then exactly the 5 owned are sold
    out = _exit(b, ledger, (15, 30))
    assert [p["qty"] for p in _market_sells(b)] == ["5"] and out["closed"] == ["SHOP"]
    assert b._find("p1")["status"] == "canceled"
    _exit(b, ledger, (15, 35))
    assert _rec(ledger)["time_exit_done"] and len(_market_sells(b)) == 1


def test_a_fill_during_the_entry_cancel_is_protected_too(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Exchange(orders=[_partial_entry(f)])
    b.fill_on_cancel["o1"] = 2                       # 2 more fill while the cancel is in flight
    _cancel(b, ledger, (10, 30))
    assert [p["qty"] for p in _ocos(b)] == ["7"] and _rec(ledger)["protect_qty"] == 7


def test_an_entry_that_completes_during_its_cancel_keeps_its_bracket(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Exchange(orders=[_partial_entry(f)])
    b.fill_on_cancel["o1"] = 94                      # the cancel lost the race: fully filled
    out = _cancel(b, ledger, (10, 30))
    assert _ocos(b) == [] and out["canceled"] == []
    assert _rec(ledger)["entry_cancel_checked"] and _rec(ledger)["protection"] == "bracket"


def test_a_pending_entry_cancel_stays_in_flight_until_the_broker_shows_it_final(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Exchange(orders=[_partial_entry(f)])
    b.cancel_mode["o1"] = "pending"                  # 204, but the order is only pending_cancel
    out = _cancel(b, ledger, (10, 30))
    assert out["status"] == "pending" and "entry cancel pending (pending_cancel, 5 filled)" in out["pending"][0]
    assert not _rec(ledger).get("entry_cancel_checked") and _ocos(b) == []
    assert _rec(ledger)["entry_cancel"] == {"requested_at": ts(10, 30), "tries": 1}
    _cancel(b, ledger, (10, 31))
    assert len(b.deletes) == 1                       # an acknowledged cancel is not sent again
    b.ack("o1")
    _cancel(b, ledger, (10, 32))
    assert [p["qty"] for p in _ocos(b)] == ["5"] and _rec(ledger)["entry_cancel_checked"]


def test_a_refused_entry_cancel_is_retried_then_raised_as_unprotected(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Exchange(orders=[_partial_entry(f)])
    b.cancel_mode["o1"] = "refuse"
    out = _cancel(b, ledger, (10, 30))
    assert out["status"] == "error" and "entry cancel refused" in out["errors"][0] and out["unprotected"] == []
    _cancel(b, ledger, (10, 31))
    assert len(b.deletes) == 2 and not _rec(ledger).get("entry_cancel_checked")
    out = _cancel(b, ledger, (10, 35))               # PROTECT_GRACE past the deadline: a loud alarm
    assert out["status"] == "error" and "UNPROTECTED" in out["error"]
    assert "5 filled" in out["unprotected"][0] and "5 min after its entry deadline" in out["unprotected"][0]
    assert "entry cancel refused" in _rec(ledger)["protect_alarm"] and _ocos(b) == []


def test_a_refused_protective_exit_is_flattened(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Exchange(orders=[_partial_entry(f)], refuse_oco=403)
    out = _cancel(b, ledger, (10, 30))
    assert [(p["qty"], p["client_order_id"]) for p in _market_sells(b)] == [("5", f"{f.forecast_id}-pf")]
    assert out["unprotected"] == [] and out["status"] == "entries_checked"
    rec = _rec(ledger)
    assert rec["entry_cancel_checked"] and rec["protection"] == "flattened"
    _exit(b, ledger, (15, 30))                       # the flatten's fill counts: nothing left to sell
    assert len(_market_sells(b)) == 1 and _rec(ledger)["time_exit_done"]


def test_protective_exit_and_flatten_both_refused_is_a_loud_problem(ledger, monkeypatch):
    from edge import pipeline as P
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Exchange(orders=[_partial_entry(f)], refuse_oco=403, refuse_sells=99)
    out = _cancel(b, ledger, (10, 30))
    assert out["status"] == "error" and "5 shares owned with NO stop" in out["unprotected"][0]
    assert "both refused" in out["unprotected"][0]
    rec = _rec(ledger)
    assert not rec.get("entry_cancel_checked") and "NO stop" in rec["protect_alarm"]
    tg = TG()
    out = P.run(None, ledger, now=ts(10, 31), http=b, notifier=tg)        # its own PROBLEM kind
    assert "UNPROTECTED" in out["paper_cancel_unprotected"]["error"]
    assert any("paper_cancel_unprotected" in t and "UNPROTECTED" in t for t in tg.sent)
    for hm in ((10, 32), (10, 33), (10, 34)):
        out = _cancel(b, ledger, hm)
    assert len(_market_sells(b)) == PP.MAX_TRIES and out["unprotected"]      # bounded, alarm stands
    assert len(_ocos(b)) == 1


class Crash(BaseException):
    """The process dies (deploy, OOM) -- nothing after this point runs."""


class DiesAfterPost(Exchange):
    died = False

    def post(self, url, json=None, headers=None, timeout=None):
        r = super().post(url, json=json, headers=headers, timeout=timeout)
        if json.get("order_class") == "oco" and not self.died:
            self.died = True
            raise Crash()                            # the broker has the OCO; we never saw the answer
        return r


def test_a_restart_between_cancel_and_protection_never_doubles_the_protective_exit(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = DiesAfterPost(orders=[_partial_entry(f)])
    b.cancel_mode["o1"] = "pending"
    _cancel(b, ledger, (10, 30))                     # cancel asked, still pending
    b.ack("o1")
    restarted = Ledger(ledger.store)                 # restart: only the stored record survives
    with pytest.raises(Crash):
        _cancel(b, restarted, (10, 31))
    assert _rec(ledger)["protect_ids"] == [f"{f.forecast_id}-px"]                 # recorded BEFORE the post
    assert not _rec(ledger).get("entry_cancel_checked")
    out = _cancel(b, Ledger(ledger.store), (10, 32))
    assert len(_ocos(b)) == 1 and "protected by its OCO exit" in out["protected"][0]
    assert _rec(ledger)["entry_cancel_checked"] and _rec(ledger)["protection"] == "oco"


def test_an_entry_fill_during_the_time_exit_cancel_is_sold_not_marked_flat(ledger):
    """F02: an entry still working at 15:30 fills while its cancel is in flight. The old step counted
    from reads taken BEFORE the cancel (0 bought), set time_exit_done, and left 4 shares open."""
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Exchange(orders=[_partial_entry(f, filled="0", qty="6", status="new")])
    b.fill_on_cancel["o1"] = 4
    out = _exit(b, ledger, (15, 30))
    assert [p["qty"] for p in _market_sells(b)] == ["4"] and out["closed"] == ["SHOP"]
    assert not _rec(ledger).get("time_exit_done")   # done only once the broker shows the sell filled
    _exit(b, ledger, (15, 35))
    assert _rec(ledger)["time_exit_done"] and len(_market_sells(b)) == 1


@pytest.mark.parametrize("leg_fill, sells", [(6, []), (2, ["4"])])
def test_an_exit_leg_fill_during_the_time_exit_cancel_is_counted(ledger, leg_fill, sells):
    """The old step would have sold all 6 again on top of the leg's fill: a short."""
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    legs = [{"id": "leg-tp", "type": "limit", "qty": "6", "status": "new", "filled_qty": "0"},
            {"id": "leg-sl", "type": "stop", "qty": "6", "status": "held", "filled_qty": "0"}]
    b = Exchange(orders=[_filled_entry(f, legs=legs)])
    b.fill_on_cancel["leg-tp"] = leg_fill
    out = _exit(b, ledger, (15, 30))
    assert [p["qty"] for p in _market_sells(b)] == sells and out["not_flat"] == []
    _exit(b, ledger, (15, 35))
    assert _rec(ledger)["time_exit_done"] and [p["qty"] for p in _market_sells(b)] == sells


def test_a_delayed_cancel_acknowledgement_keeps_the_time_exit_in_flight(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    legs = [{"id": "leg-tp", "type": "limit", "qty": "6", "status": "new", "filled_qty": "0"},
            {"id": "leg-sl", "type": "stop", "qty": "6", "status": "held", "filled_qty": "0"}]
    b = Exchange(orders=[_filled_entry(f, legs=legs)])
    b.cancel_mode.update({"leg-tp": "pending", "leg-sl": "pending"})        # 204 is not proof
    out = _exit(b, ledger, (15, 30))
    assert _market_sells(b) == [] and "cancel pending" in out["errors"][0] and out["not_flat"] == []
    assert not _rec(ledger).get("time_exit_done")
    out = _exit(b, ledger, (15, 50))                 # the last tick: still pending -> NOT FLAT
    assert "still working after the cancel" in out["not_flat"][0] and "6 shares owned" in out["not_flat"][0]
    assert len(b.deletes) == 2 and not _rec(ledger).get("time_exit_done")    # not re-sent
    b.ack("leg-tp")
    b.ack("leg-sl")
    _exit(b, ledger, (15, 54))
    assert [p["qty"] for p in _market_sells(b)] == ["6"]


def test_a_failed_leg_cancel_at_the_time_exit_is_not_flat_on_the_last_tick(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    legs = [{"id": "leg-tp", "type": "limit", "qty": "6", "status": "new", "filled_qty": "0"}]
    b = Exchange(orders=[_filled_entry(f, legs=legs)])
    b.cancel_mode["leg-tp"] = "error"                # no answer to the cancel
    out = _exit(b, ledger, (15, 50))
    assert _market_sells(b) == [] and "cancel failed" in out["errors"][0]
    assert "could not be cancelled" in out["not_flat"][0] and not _rec(ledger).get("time_exit_done")


class Garbled(Exchange):
    """The broker answers the entry lookup with something that is not an order."""

    def get(self, url, params=None, headers=None, timeout=None):
        if "by_client_order_id" in url and params["client_order_id"].endswith("-entry"):
            return R(200, [])
        return super().get(url, params=params, headers=headers, timeout=timeout)


def test_an_unreadable_broker_answer_is_never_read_as_flat_or_unfilled(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Garbled(orders=[_partial_entry(f)])
    out = _cancel(b, ledger, (10, 30))
    assert out["status"] == "error" and "could not report its entry order" in out["errors"][0]
    assert b.deletes == [] and not _rec(ledger).get("entry_cancel_checked")
    assert "could not report its entry order" in _cancel(b, ledger, (10, 35))["unprotected"][0]
    out = _exit(b, ledger, (15, 50))
    assert "cannot confirm it is flat" in out["not_flat"][0] and _market_sells(b) == []
    assert not _rec(ledger).get("time_exit_done")


def test_an_unknown_order_status_stays_in_flight(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Exchange(orders=[_partial_entry(f, filled="3", qty="6", status="mystery_state")])
    b.cancel_mode["o1"] = "ignore"                   # 204, yet the broker keeps reporting the odd status
    out = _exit(b, ledger, (15, 30))
    assert _market_sells(b) == [] and "mystery_state" in out["errors"][0]
    assert not _rec(ledger).get("time_exit_done")


def test_a_protective_exit_is_graded_from_the_brokers_record(ledger):
    from edge import broker_alpaca as BA, fills as FL, readout as RO
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    fid = f.forecast_id
    entry = {**_partial_entry(f), "status": "canceled", "filled_avg_price": "148.30",
             "submitted_at": iso(ts(9, 11)), "filled_at": iso(ts(10, 2)), "canceled_at": iso(ts(10, 30))}
    entry["legs"] = [{**g, "status": "canceled", "submitted_at": iso(ts(9, 11)), "canceled_at": iso(ts(10, 30))}
                     for g in entry["legs"]]
    oco = {"id": "p1", "client_order_id": f"{fid}-px", "type": "limit", "side": "sell", "qty": "5",
           "status": "canceled", "filled_qty": "0", "submitted_at": iso(ts(10, 30)), "canceled_at": iso(ts(11, 0)),
           "legs": [{"id": "p1-sl", "type": "stop", "side": "sell", "qty": "5", "status": "filled",
                     "filled_qty": "5", "filled_avg_price": "143.10", "submitted_at": iso(ts(10, 30)),
                     "filled_at": iso(ts(11, 0))}]}
    out = PP.reconcile(Broker(orders=[entry, oco]), ledger, day="2026-09-23", experiments=EXPERIMENTS,
                       now=ts(16, 25))
    row = out["settled"]["gap_and_go_auto@v1:SHOP"]
    assert row["actual"] == "LOSS" and row["pnl_usd"] == pytest.approx(5 * (143.10 - 148.30))
    roles = [x["role"] for x in RO.view(ledger.store, "paper", "2026-09-23")["orders"][0]["broker_exits"]]
    assert "protective_stop" in roles and "protective_target" in roles
    assert BA.role_of(f"{fid}-px") == BA.role_of(f"{fid}-px2") == FL.TARGET
