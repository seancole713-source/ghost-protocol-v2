"""The scheduled AI agents' plumbing: append-only notes, the AI scorecard, and the views they read."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from edge import agent_notes as AN
from edge import readout as RO
from edge import scorecard as SC
from edge.contracts import issue
from edge.ledger import Ledger, MemoryStore
from edge.pipeline import GAP_AND_GO_AUTO

ET = ZoneInfo("America/New_York")


def ts(hh, mm, d=23):
    return int(datetime(2026, 9, d, hh, mm, tzinfo=ET).timestamp())


# ---- notes -----------------------------------------------------------------

def test_a_note_is_written_once_and_never_overwritten():
    store = MemoryStore()
    out = AN.write(store, kind="brief", author="premarket-briefer", body="2 names on the card", now=ts(8, 12),
                   symbols=["shop", "SHOP", "usar"])
    rec = store.get("edge_notes", out["id"])
    assert rec["day"] == "2026-09-23" and rec["symbols"] == ["SHOP", "USAR"]
    with pytest.raises(AN.NoteError, match="never overwritten"):
        AN.write(store, kind="brief", author="premarket-briefer", body="2 names on the card", now=ts(8, 12))


@pytest.mark.parametrize("kw, msg", [
    ({"kind": "trade", "author": "a", "body": "b"}, "kind must be"),
    ({"kind": "brief", "author": "", "body": "b"}, "author is required"),
    ({"kind": "brief", "author": "a", "body": " "}, "body is required"),
    ({"kind": "brief", "author": "a", "body": "x" * 8001}, "at most 8000"),
    ({"kind": "brief", "author": "a", "body": "b", "day": "Sept 23"}, "YYYY-MM-DD"),
])
def test_bad_notes_are_refused(kw, msg):
    with pytest.raises(AN.NoteError, match=msg):
        AN.write(MemoryStore(), now=ts(8, 0), **kw)


def test_notes_read_back_newest_first_and_filter_by_day_and_kind():
    store = MemoryStore()
    AN.write(store, kind="brief", author="b", body="morning", now=ts(8, 12))
    AN.write(store, kind="report", author="r", body="evening", now=ts(15, 35))
    AN.write(store, kind="report", author="r", body="yesterday", now=ts(15, 35, d=22))
    out = RO.view(store, "notes", "2026-09-23")
    assert [n["body"] for n in out["notes"]] == ["evening", "morning"] and "never evidence" in out["label"]
    assert [n["body"] for n in RO.view(store, "notes", None, "report")["notes"]] == ["evening", "yesterday"]


# ---- scorecard -------------------------------------------------------------

def _rows(n, wins, **stamp):
    return [{"symbol": f"S{i}", "outcome": "WIN" if i < wins else "LOSS", "baseline": "ELIGIBLE", **stamp}
            for i in range(n)]


def test_research_is_judged_only_with_enough_trades_on_both_sides():
    store = MemoryStore()
    store.put("edge_card_outcomes", "2026-09-23", {"day": "2026-09-23", "rows":
              _rows(10, 6, research="ELIGIBLE") + _rows(10, 2, research="REJECTED")})
    sc = SC.scorecard(store)
    assert sc["ai_research"]["verdict"].startswith("not enough data")
    assert sc["ai_research"]["approved"]["win_rate"] == 0.6 and sc["ai_research"]["approved"]["win_rate_ci"]


def test_research_that_separates_winners_is_credited_and_one_that_hurts_is_flagged():
    store = MemoryStore()
    store.put("edge_card_outcomes", "2026-09-23", {"day": "2026-09-23", "rows":
              _rows(60, 42, research="ELIGIBLE") + _rows(60, 12, research="REJECTED")})
    assert "ADDS edge" in SC.scorecard(store)["ai_research"]["verdict"]
    store.put("edge_card_outcomes", "2026-09-23", {"day": "2026-09-23", "rows":
              _rows(60, 12, research="ELIGIBLE") + _rows(60, 42, research="REJECTED")})
    assert "HURTS" in SC.scorecard(store)["ai_research"]["verdict"]


def test_no_fills_are_counted_but_never_decided():
    r = SC._rate([{"outcome": "NO_FILL"}, {"outcome": "WIN"}, {"outcome": "LOSS"}])
    assert (r["candidates"], r["decided"], r["no_fill"], r["win_rate"]) == (3, 2, 1, 0.5)


def test_research_quality_counts_quarantines_by_reviewer():
    store = MemoryStore()
    store.put("edge_research", "2026-09-23|SHOP", {"day": "2026-09-23", "symbol": "SHOP", "made_at": 1,
              "reviewer": "openai:gpt-4.1", "cost_usd": 0.4, "review": {"dilution_found": True, "entity_ok": True},
              "claims": [{"status": "USABLE"}, {"status": "QUARANTINED", "problems": ["stale"]}]})
    q = SC.research_quality(store)["by_reviewer"]["openai:gpt-4.1"]
    assert (q["claims"], q["quarantined"], q["quarantine_rate"], q["dilution_flags"]) == (2, 1, 0.5, 1)


# ---- views -----------------------------------------------------------------

def test_new_views_explain_themselves_when_empty():
    empty = MemoryStore()
    assert "no research yet" in RO.view(empty, "research")["note"]
    assert "no radar activity" in RO.view(empty, "radar")["note"]
    assert "no forecasts yet" in RO.view(empty, "paper")["note"]
    assert RO.view(empty, "models")["current"] is None
    assert "no graded cards yet" in RO.view(empty, "scorecard")["note"]
    assert RO.view(empty, "summary")["ai_scorecard"]["sessions"] == 0


def test_the_paper_view_joins_orders_to_their_forecasts():
    store = MemoryStore()
    lg = Ledger(store)
    lg.register(GAP_AND_GO_AUTO, now=ts(8, 0))
    f = issue(GAP_AND_GO_AUTO, symbol="SHOP", session_date=date(2026, 9, 23), entry_ref=146.71, issued_at=ts(9, 10))
    lg.record(f, now=ts(9, 10))
    store.put("edge_paper", f"{f.forecast_id}|paper", {"forecast_id": f.forecast_id, "state": "submitted"})
    out = RO.view(store, "paper")
    assert out["day"] == "2026-09-23" and out["orders"][0]["paper_state"] == "submitted"
    assert "no real money" in out["note"]


def test_the_note_mcp_tool_writes_and_refuses_bad_input(monkeypatch):
    from mcp import ghost_server
    tools = {t["name"]: t for t in ghost_server.list_tools()}
    assert "never evidence" in tools["ghost_edge_note"]["description"]
    import edge.store_pg as pg
    store = MemoryStore()
    monkeypatch.setattr(pg, "PostgresStore", lambda _c: store)
    import core.db as db
    monkeypatch.setattr(db, "db_conn", object(), raising=False)
    ok = ghost_server.invoke_tool("ghost_edge_note", {"kind": "watchdog", "author": "ops-watchdog",
                                                       "body": "all jobs healthy"})
    assert ok["status"] == "written" and store.get("edge_notes", ok["id"])["kind"] == "watchdog"
    bad = ghost_server.invoke_tool("ghost_edge_note", {"kind": "trade", "author": "x", "body": "buy"})
    assert bad["status"] == "refused"
    out = ghost_server.invoke_tool("ghost_edge_report", {"view": "notes", "kind": "watchdog"})
    assert out["notes"][0]["body"] == "all jobs healthy"


def test_each_stage_logs_its_views_once_per_day_for_the_agents():
    store = MemoryStore()
    out = {"day": "2026-09-23", "card": {"status": "issued"}, "paper_submit": {"status": "submitted"}}
    lines = RO.views_to_log(store, out, now=ts(9, 10))
    assert [(n, d) for n, d, _ in lines] == [("today", "2026-09-23"), ("paper", "2026-09-23"),
                                             ("research", "2026-09-23"), ("top10", "2026-09-23")]
    assert RO.views_to_log(store, out, now=ts(9, 15)) == []          # once per stage per day
    evening = {"day": "2026-09-23", "card_graded": {"status": "graded"}, "radar_close": {"status": "closed"}}
    assert [n for n, _, _ in RO.views_to_log(store, evening, now=ts(16, 25))] == [
        "radar", "paper", "experiments", "scorecard", "top10", "control"]
    assert RO.views_to_log(store, {"day": "2026-09-23", "card": {"status": "error"}}, now=ts(9, 20)) == []
    long = RO.views_to_log(MemoryStore(), out, now=ts(9, 10), limit=50)
    assert all(len(j) <= 50 for _, _, j in long)


def test_only_one_runner_holds_the_tick_lease_until_it_expires_or_is_released():
    store = MemoryStore()
    assert store.claim("edge_shadow", owner="old", ttl_s=280, now=1000)
    assert not store.claim("edge_shadow", owner="new", ttl_s=280, now=1100)     # overlap: new waits
    assert store.claim("edge_shadow", owner="old", ttl_s=280, now=1200)         # holder renews
    store.release("edge_shadow", owner="old")
    assert store.claim("edge_shadow", owner="new", ttl_s=280, now=1210)
    assert store.claim("edge_shadow", owner="old", ttl_s=280, now=1210 + 281)   # a dead holder expires


def test_the_postgres_lease_is_one_atomic_statement():
    from edge.store_pg import PostgresStore
    sql = []

    class Cur:
        def execute(self, q, params=None):
            sql.append((q, params))

        def fetchone(self):
            return ("edge_shadow",)

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return Cur()

        def commit(self):
            pass

    assert PostgresStore(lambda: Conn()).claim("edge_shadow", owner="a", ttl_s=280, now=1000)
    q, params = sql[0]
    assert "ON CONFLICT" in q and "RETURNING" in q and params[-2:] == (720, "a")


def test_nightly_prune_touches_caches_only_never_the_ledger():
    from edge import pipeline as P
    assert not set(P.RETENTION_DAYS) & {"forecasts", "outcomes", "abstentions", "edge_cards", "experiments",
                                         "exclusions", "edge_card_outcomes", "edge_research", "edge_notes"}
    sql = []

    class Cur:
        rowcount = 7

        def execute(self, q, params=None):
            sql.append((q, params))

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return Cur()

        def commit(self):
            pass

    from edge.store_pg import PostgresStore
    out = P.prune(PostgresStore(lambda: Conn()), now=10 * 86_400)
    assert out["deleted"]["edge_rvol"] == 7
    assert ("DELETE FROM edge_rows WHERE tbl = %s AND known_at < %s", ("edge_rvol", 7 * 86_400)) in sql
