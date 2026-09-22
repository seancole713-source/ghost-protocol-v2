"""edge workers and delivery: research claims, broker mapping, storage, phone cards."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from edge import broker_alpaca as A, cards, fills as F, radar as R, research as RS
from edge.contracts import GAP_AND_GO_V1
from edge.ledger import Ledger
from edge.store_pg import PostgresStore

T = 1790160000


def claim(**kw):
    base = dict(symbol="SHOP", kind="contract", author="claude", made_at=T,
                statement="Shopify and Meta announced Muse agent checkout via Shop Pay.",
                citations=(RS.Citation("https://www.reuters.com/x", T - 60, "Shopify said..."),))
    base.update(kw)
    return RS.Claim(**base)


# ---------------------------------------------------------------- research --

def test_a_claim_without_a_source_is_quarantined():
    assert RS.validate(claim(citations=()))[0] == RS.QUARANTINED


def test_workers_may_not_state_probabilities():
    st, why = RS.validate(claim(statement="There is a 73% chance SHOP rises today."))
    assert st == RS.QUARANTINED and "probabilities" in why[0]


def test_numbers_need_units_and_sources_need_urls():
    st, why = RS.validate(claim(value=143.0, citations=(RS.Citation("reuters", T - 1),)))
    assert st == RS.QUARANTINED and len(why) == 2


def test_two_independent_domains_verify_one_is_single_source():
    two = claim(citations=(RS.Citation("https://www.reuters.com/a", T - 9), RS.Citation("https://investor.shopify.com/b", T - 9)))
    assert RS.validate(two)[0] == RS.VERIFIED
    assert RS.validate(claim())[0] == RS.SINGLE_SOURCE


def test_a_claim_cannot_review_itself_and_a_late_claim_is_hindsight():
    assert RS.reviewed_status(claim(), RS.Review("claude", entity_ok=True))[0] == RS.QUARANTINED
    assert RS.validate(claim(), issued_at=T - 10)[0] == RS.QUARANTINED


def test_reviewer_findings_quarantine():
    st, why = RS.reviewed_status(claim(), RS.Review("openai", entity_ok=True, contradictions=["IR page says 'pilot', not launch"]))
    assert st == RS.QUARANTINED and "pilot" in why[0]


def test_agreement_is_recorded_as_correlated_not_as_proof():
    ag = RS.agreement([claim(), claim(author="openai")])
    row = ag["SHOP:contract"]
    assert row["agree"] and "not independent" in row["note"]


# ------------------------------------------------------------------ broker --

def test_alpaca_updates_become_incremental_fill_events():
    upd = {"event": "partial_fill", "timestamp": "2026-09-23T13:31:05Z", "price": "9.50",
           "order": {"id": "o1", "client_order_id": "abc123-entry", "filled_qty": "40"}}
    e1 = A.to_event(upd)
    upd2 = {"event": "fill", "timestamp": "2026-09-23T13:31:09Z", "price": "9.51",
            "order": {"id": "o1", "client_order_id": "abc123-entry", "filled_qty": "105"}}
    e2 = A.to_event(upd2, prior_filled_qty=40)
    assert (e1.role, e1.fill_qty, e2.fill_qty) == (F.ENTRY, 40, 65)


def test_held_legs_count_as_live_protection():
    upd = {"event": "held", "timestamp": "2026-09-23T13:25:00Z",
           "order": {"id": "o2", "client_order_id": "abc123-sl", "filled_qty": "0"}}
    e = A.to_event(upd)
    assert e.role == F.STOP and e.status == F.ACCEPTED


def test_bracket_refuses_extended_hours():
    with pytest.raises(ValueError):
        A.bracket_request("abc", "SHOP", 6, entry_stop=148.2, entry_limit=149.6, target=155.6,
                          stop=143.8, extended_hours=True)
    req = A.bracket_request("abc", "SHOP", 6, entry_stop=148.2, entry_limit=149.6, target=155.6, stop=143.8)
    assert req["order_class"] == "bracket" and req["client_order_id"] == "abc-entry"


# ------------------------------------------------------------------- store --

class FakeCursor:
    def __init__(self, db):
        self.db, self._r = db, []

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("INSERT"):
            tbl, key, row, known = params
            import json
            self.db[(tbl, key)] = (json.loads(row), known)
        elif s.startswith("SELECT row FROM edge_rows WHERE tbl=%s AND key=%s"):
            v = self.db.get(tuple(params))
            self._r = [(v[0],)] if v else []
        elif s.startswith("SELECT row FROM edge_rows WHERE"):
            tbl, *pairs = params
            conds = list(zip(pairs[0::2], pairs[1::2]))
            self._r = [(row,) for (t, _k), (row, _kn) in sorted(self.db.items(), key=lambda x: x[1][1])
                       if t == tbl and all(str(row.get(k)) == v for k, v in conds)]

    def fetchone(self):
        return self._r[0] if self._r else None

    def fetchall(self):
        return self._r


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        pass


def test_the_ledger_runs_on_the_postgres_store():
    db = {}

    @contextmanager
    def connect():
        yield FakeConn(db)

    lg = Ledger(PostgresStore(connect))
    lg.register(GAP_AND_GO_V1, now=T)
    assert lg.store.get("experiments", "gap_and_go@v1")["spec_hash"] == GAP_AND_GO_V1.spec_hash()
    assert len(lg.store.scan("experiments", status="active")) == 1


# ------------------------------------------------------------------- cards --

def test_the_morning_line_counts_and_carries_health():
    items = []
    for sym, st in (("A", R.ENTRY_ELIGIBLE), ("B", R.SETUP_FORMING), ("C", R.WATCHING)):
        it = R.RadarItem(sym, "2026-09-23", T, 6.0, strategy="catalyst_breakout")
        it.state = st
        items.append(it)
    line = cards.morning_headline(items, "3 setups. Coverage healthy.")
    assert line == "3 developing setups. 1 currently eligible. 2 waiting for confirmation. Coverage healthy."
    assert cards.morning_headline([], "No qualifying setups. Coverage healthy.") == "No qualifying setups. Coverage healthy."


def test_an_unvalidated_card_says_experimental_and_never_a_percentage():
    c = cards.setup_card(symbol="SHOP", why="Meta agent checkout via Shop Pay", sources=["reuters.com"],
                         strategy="catalyst_breakout", entry_rule="close above the 5-min opening-range high",
                         max_entry=149.60, expires_et="10:30", target=155.61, stop=143.75, time_exit_et="15:30",
                         prob=0.71, prob_n=None, validated=False)
    assert "Experimental" in c and "71%" not in c
