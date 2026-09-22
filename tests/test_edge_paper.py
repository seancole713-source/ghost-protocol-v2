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


def test_unfilled_entry_is_cancelled_at_1030_and_a_filled_one_is_not(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Broker(orders=[{"id": "o1", "client_order_id": f"{f.forecast_id}-entry", "status": "new", "filled_qty": "0"}])
    PP.cancel_unfilled_entries(b, ledger, day="2026-09-23", experiments=EXPERIMENTS)
    assert b.deletes == ["https://paper-api.alpaca.markets/v2/orders/o1"]


def test_time_exit_cancels_legs_first_then_sells_what_is_open(ledger):
    f = ledger.fc
    PP.submit(Broker(), ledger, day="2026-09-23", experiments=EXPERIMENTS)
    b = Broker(orders=[{"id": "leg-tp", "status": "new"}, {"id": "leg-sl", "status": "held"}], position_qty=6)
    out = PP.time_exit(b, ledger, day="2026-09-23", experiments=EXPERIMENTS)
    assert len(b.deletes) == 2 and out["closed"] == ["SHOP"]
    assert b.posts[-1]["client_order_id"] == f"{f.forecast_id}-tx" and b.posts[-1]["type"] == "market"


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
