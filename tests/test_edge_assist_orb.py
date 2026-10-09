"""gap_and_go_assist@v1 (Ghost + Claude catalyst) and gap_and_go_orb@v1 (opening-range entry).

Operator decision 2026-10-09: two separately named paper experiments, each one change away from
gap_and_go_sipref, so their records can be compared on the same days. No other experiment reads
an agent claim, and nothing about the 09:05 card's own experiments changes."""
from __future__ import annotations

import pytest

from edge import agent_catalysts as AC, orb as ORB, pipeline as P, setups as S
from edge.ledger import Ledger, MemoryStore
from tests.test_edge_pipeline import DAY, FakeAlpaca, Resp, iso, ts


@pytest.fixture
def ledger():
    return Ledger(MemoryStore())


def claim(store, sym="USAR", kind="contract", at=None, pub=None, headline="USA Rare Earth wins $50M DoD contract"):
    return AC.write(store, symbol=sym, headline=headline, kind=kind, source_url="https://example.com/news/1",
                    published_at=pub if pub is not None else ts(7, 0), author="claude-morning-research",
                    now=at if at is not None else ts(9, 0))


# ---- agent claims: validated, append-only, point-in-time ------------------------------------------

def test_a_claim_is_validated_and_never_overwritten():
    st = MemoryStore()
    ok = claim(st)
    assert ok["status"] == "written" and ok["first_seen_at"] == ts(9, 0)
    with pytest.raises(AC.ClaimError):
        claim(st)                                                     # same claim twice
    for bad in ({"kind": "price_action"}, {"pub": ts(9, 0) + 3600}, {"pub": ts(9, 0) - 90_000}):
        with pytest.raises(AC.ClaimError):
            claim(MemoryStore(), **bad)
    with pytest.raises(AC.ClaimError):
        AC.write(st, symbol="USAR", headline="x", kind="contract", source_url="http://insecure",
                 published_at="2026-09-23T07:00:00-04:00", author="a", now=ts(9, 0))
    with pytest.raises(AC.ClaimError):                                   # no time zone: ambiguous
        AC.write(st, symbol="USAR", headline="y", kind="contract", source_url="https://e.com/2",
                 published_at="2026-09-23T07:00:00", author="a", now=ts(9, 0))


def test_a_claim_counts_only_if_received_before_the_cutoff():
    st = MemoryStore()
    claim(st, at=ts(9, 11))
    assert AC.usable(st, day=DAY.isoformat(), symbol="USAR", issued_at=ts(9, 10)) == []
    assert len(AC.usable(st, day=DAY.isoformat(), symbol="USAR", issued_at=ts(9, 15))) == 1


# ---- gap_and_go_assist@v1 on the card ----------------------------------------------------------------

def test_an_agent_claim_supplies_the_catalyst_only_for_the_assist_experiment(ledger):
    """USAR: +8.9%, liquid, but its only news is sector sympathy -- rejected by every keyword
    experiment. A sourced contract claim received at 09:00 makes it eligible for assist alone."""
    claim(ledger.store)
    out = P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    assert out["status"] == "issued" and out["forecasts"] == ["SHOP"]
    card = ledger.store.get("edge_cards", DAY.isoformat())
    rows = {r["symbol"]: r for r in card["rows"]}
    assert rows["USAR"]["verdict"] != S.ELIGIBLE and rows["USAR"]["sipref_verdict"] != S.ELIGIBLE
    assert rows["USAR"]["assist_verdict"] == S.ELIGIBLE and rows["USAR"]["assist_catalyst_source"] == "agent"
    assert rows["SHOP"]["assist_catalyst_source"] == "keyword"
    assert card["assist_forecasts"] == ["SHOP", "USAR"]
    f = [x for x in ledger.store.scan("forecasts", experiment_id=P.ASSIST_EID) if x["symbol"] == "USAR"][0]
    assert f["evidence"]["assist_catalyst_source"] == "agent"
    assert f["evidence"]["assist_claims"][0]["source_url"] == "https://example.com/news/1"
    for eid in (P.EID, P.BASE_EID, P.SIPREF_EID):                      # nobody else moved
        assert all(x["symbol"] != "USAR" or eid == P.BASE_EID
                   for x in ledger.store.scan("forecasts", experiment_id=eid))


def test_a_late_claim_does_not_count_and_an_offering_claim_is_dilution(ledger):
    claim(ledger.store, at=ts(9, 11))                                   # after the 09:10 cutoff
    claim(ledger.store, sym="SHOP", kind="offering_dilution", headline="Shopify prices $1B offering")
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    rows = {r["symbol"]: r for r in ledger.store.get("edge_cards", DAY.isoformat())["rows"]}
    assert rows["USAR"]["assist_verdict"] != S.ELIGIBLE
    assert rows["SHOP"]["assist_verdict"] != S.ELIGIBLE and rows["SHOP"]["verdict"] == S.ELIGIBLE
    assert any("dilution" in r for r in rows["SHOP"]["assist_reasons"])


def test_the_catalyst_mcp_tool_writes_and_refuses(monkeypatch):
    from mcp import ghost_server
    tools = {t["name"]: t for t in ghost_server.list_tools()}
    assert "gap_and_go_assist@v1" in tools["ghost_edge_catalyst"]["description"]
    import core.db as db
    import edge.store_pg as pg
    store = MemoryStore()
    monkeypatch.setattr(pg, "PostgresStore", lambda _c: store)
    monkeypatch.setattr(db, "db_conn", object(), raising=False)
    monkeypatch.setattr("time.time", lambda: ts(9, 0))
    ok = ghost_server.invoke_tool("ghost_edge_catalyst", {
        "symbol": "hum", "headline": "Humana Announces Improved CMS Star Ratings", "kind": "fda_regulatory",
        "source_url": "https://example.com/hum", "published_at": "2026-09-23T07:02:00-04:00",
        "author": "claude-morning-research"})
    assert ok["status"] == "written" and store.get(AC.COLLECTION, ok["id"])["symbol"] == "HUM"
    bad = ghost_server.invoke_tool("ghost_edge_catalyst", {
        "symbol": "HUM", "headline": "buy it", "kind": "trade", "source_url": "https://e.com",
        "published_at": "2026-09-23T07:02:00-04:00", "author": "x"})
    assert bad["status"] == "refused"


# ---- gap_and_go_orb@v1 at 09:45 ------------------------------------------------------------------------

class OrbAlpaca(FakeAlpaca):
    """One-minute bars from 09:30: SHOP ranges 146.0-149.0 in its first 15 minutes."""

    def __init__(self, minutes=16, hi=149.0):
        super().__init__("morning")
        self.minutes, self.hi = minutes, hi

    def __call__(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        if url.endswith("/v2/stocks/bars") and params.get("timeframe") == "1Min":
            out = {}
            for s in params["symbols"].split(","):
                rows = []
                for i in range(self.minutes):
                    t = ts(9, 30) + 60 * i
                    h = self.hi if i == 7 else 147.5
                    rows.append({"t": iso(t), "o": 147.0, "h": h, "l": 146.0, "c": 147.2, "v": 1000})
                out[s] = rows
            return Resp({"bars": out})
        return super().__call__(url, params, headers, timeout)


def test_orb_issues_at_0945_above_the_opening_range_high(ledger):
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    now = ts(9, 45) + 5
    out = ORB.step(OrbAlpaca(), ledger, now=now)
    assert out == {"status": "issued", "forecasts": ["SHOP"], "abstained": []}
    f = ledger.store.scan("forecasts", experiment_id=P.ORB_EID)[0]
    assert f["entry_trigger"] == 149.15 and f["entry_limit"] == 150.64          # 149.00 * 1.001 / 1.011
    assert f["target"] == round(149.15 * 1.05, 2) and f["stop"] == round(149.15 * 0.97, 2)
    assert f["entry_expiry"] == now + 60 + 44 * 60                                 # 10:30:05 ET
    assert f["evidence"]["or_high"] == 149.0 and f["evidence"]["or_feed"] == "iex"
    assert ORB.step(OrbAlpaca(), ledger, now=now + 60)["status"] == "already_done"


def test_orb_only_issues_in_its_window_and_abstains_without_a_range(ledger):
    P.morning_card(FakeAlpaca(), ledger, now=ts(9, 10))
    assert ORB.step(OrbAlpaca(), ledger, now=ts(9, 47))["status"] == "outside_issue_window"
    assert ORB.step(OrbAlpaca(), ledger, now=ts(9, 44))["status"] == "outside_issue_window"
    out = ORB.step(OrbAlpaca(minutes=3), ledger, now=ts(9, 45) + 5)
    assert out["abstained"] == ["SHOP"]
    ab = ledger.store.scan("abstentions", experiment_id=P.ORB_EID)
    assert "opening range" in ab[0]["reasons"][0]


def test_orb_without_a_card_records_nothing(ledger):
    assert ORB.step(OrbAlpaca(), ledger, now=ts(9, 45) + 5)["status"] == "no_card"
    assert ledger.store.scan("forecasts", experiment_id=P.ORB_EID) == []


def test_the_tick_runs_orb_and_its_paper_submit_and_every_paper_step_knows_the_new_specs():
    from edge import intraday as I
    specs = {s.experiment_id for s in P._all_specs(MemoryStore(), I)}
    assert {P.ASSIST_EID, P.ORB_EID} <= specs
    src = open(P.__file__).read()
    assert 'guarded("orb"' in src and 'experiments=LATE_EXPERIMENTS' in src
