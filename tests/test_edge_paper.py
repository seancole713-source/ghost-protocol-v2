"""Paper execution: a real broker's records, and no possible path to live money."""
from __future__ import annotations

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


class Broker:
    """A tiny Alpaca paper account: records every call, serves canned orders."""

    def __init__(self, orders=None, position_qty=0, reject=False):
        self.posts, self.deletes, self.orders = [], [], orders or []
        self.position_qty, self.reject = position_qty, reject

    def post(self, url, json=None, headers=None, timeout=None):
        assert "paper-api.alpaca.markets" in url
        self.posts.append(json)
        if self.reject:
            return R(403, {"message": "insufficient buying power"})
        return R(200, {"id": f"o{len(self.posts)}", **json})

    def delete(self, url, headers=None, timeout=None):
        self.deletes.append(url)
        return R(204)

    def get(self, url, params=None, headers=None, timeout=None):
        if "by_client_order_id" in url:
            hit = [o for o in self.orders if o.get("client_order_id") == (params or {}).get("client_order_id")]
            return R(200, hit[0]) if hit else R(404, {"message": "order not found"})
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
                 nested=None):
        super().__init__(orders=orders, position_qty=position_qty)
        self.sell_status, self.refuse_sells, self.close_ok = sell_status, refuse_sells, close_ok
        self.nested, self.closes, self.ids_seen = nested or {}, [], set()

    def post(self, url, json=None, headers=None, timeout=None):
        assert "paper-api.alpaca.markets" in url
        self.posts.append(json)
        cid = json["client_order_id"]
        if cid in self.ids_seen:                              # Alpaca: client_order_id must be unique
            return R(422, {"message": "client_order_id must be unique"})
        self.ids_seen.add(cid)
        if json.get("side") == "sell" and self.refuse_sells:
            self.refuse_sells -= 1
            return R(403, {"message": "insufficient qty available for order (held_for_orders)"})
        o = {"id": f"s{len(self.posts)}", "client_order_id": cid, "status": self.sell_status,
             "filled_qty": json["qty"] if self.sell_status == "filled" else "0"}
        self.orders.append(o)
        return R(200, o)

    def delete(self, url, headers=None, timeout=None):
        self.deletes.append(url)
        if "/v2/positions/" in url:
            self.closes.append(url)
            return R(200, {"id": "close1", "symbol": "SHOP"}) if self.close_ok else R(403, {"message": "held"})
        return R(204)

    def get(self, url, params=None, headers=None, timeout=None):
        tail = url.rsplit("/v2/orders/", 1)[-1] if "/v2/orders/" in url else None
        if tail and tail in self.nested:
            assert (params or {}).get("nested") == "true"
            return R(200, self.nested[tail])
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
    ledger.settle(f.forecast_id, Resolution("NO_FILL"), now=ts(16, 25), record="forecast")
    ledger.settle(f.forecast_id, Resolution("NO_FILL"), now=ts(16, 25), record="simulated")
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
    lg.settle(old.forecast_id, Resolution("WIN", pnl_usd=50.0), now=ts(16, 25))
    rep = lg.report(eid)
    assert rep["feed_regime"] == "iex" and rep["records"]["simulated"]["wins"] == 1 and "other_regimes" not in rep

    monday = date(2026, 9, 28)
    lg.store.put("edge_cards", monday.isoformat(), {"day": monday.isoformat(), "live_feed": "sip"})
    new = issue(GAP_AND_GO_AUTO, symbol="GLND", session_date=monday, entry_ref=6.1,
                issued_at=int(datetime(2026, 9, 28, 9, 10, tzinfo=ET).timestamp()))
    lg.record(new, now=int(datetime(2026, 9, 28, 9, 11, tzinfo=ET).timestamp()))
    lg.settle(new.forecast_id, Resolution("LOSS", pnl_usd=-30.0),
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
    ledger.settle(f.forecast_id, Resolution("WIN", pnl_usd=40.0), now=ts(16, 20), record="forecast")
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
