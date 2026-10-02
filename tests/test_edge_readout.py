"""edge readout: what a phone or a Claude session sees -- read-only, intervals always."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from edge import readout as RO
from edge.contracts import issue
from edge.ledger import Ledger, MemoryStore
from edge.pipeline import EXPERIMENTS, GAP_AND_GO_AUTO
from edge.resolver import Resolution

ET = ZoneInfo("America/New_York")


def ts(hh, mm):
    return int(datetime(2026, 9, 23, hh, mm, tzinfo=ET).timestamp())


def seeded():
    store = MemoryStore()
    lg = Ledger(store)
    for spec in EXPERIMENTS:
        lg.register(spec, now=ts(8, 0))
    f = issue(GAP_AND_GO_AUTO, symbol="SHOP", session_date=date(2026, 9, 23), entry_ref=146.71, issued_at=ts(9, 10))
    lg.record(f, now=ts(9, 11))
    lg.settle(f.forecast_id, Resolution("WIN", pnl_usd=48.0, resolver_version="resolver_v2"), now=ts(16, 25), record="simulated")
    store.put("edge_cards", "2026-09-23", {"day": "2026-09-23", "forecasts": ["SHOP"], "baseline_forecasts": ["SHOP", "USAR"],
                                           "health_banner": "1 setup. Coverage healthy.", "rows": [{"symbol": "SHOP", "verdict": "ELIGIBLE"}]})
    store.put("edge_backtest", "v1", {"window": ["2026-06-26", "2026-09-18"], "experiments": {}, "limits": ["NOT the forward record"],
                                      "completed_at": 1, "sessions_detail": [{"big": "x" * 1000}]})
    return store


def test_summary_carries_intervals_and_the_disclaimer():
    s = RO.view(seeded(), "summary")
    sim = s["experiments"]["gap_and_go_auto@v1"]["records"]["simulated"]
    assert sim["wins"] == 1 and sim["win_rate_ci"] is not None
    assert "nothing here is a trade recommendation" in s["note"]
    assert s["today"]["forecasts"] == ["SHOP"]


def test_backtest_view_never_ships_the_bulky_detail():
    b = RO.view(seeded(), "backtest")
    assert "sessions_detail" not in b and b["limits"] == ["NOT the forward record"]


def test_empty_views_explain_themselves():
    empty = MemoryStore()
    assert "first one is written" in RO.view(empty, "today")["note"]
    assert "runs once, overnight" in RO.view(empty, "backtest")["note"]
    assert RO.view(empty, "nope")["error"].startswith("view must be")


def test_the_mcp_tool_is_listed_and_routes_to_the_readout(monkeypatch):
    from mcp import ghost_server
    tools = {t["name"]: t for t in ghost_server.list_tools()}
    assert "ghost_edge_report" in tools
    assert "never a trade recommendation" in tools["ghost_edge_report"]["description"]
    import edge.store_pg as pg
    store = seeded()
    monkeypatch.setattr(pg, "PostgresStore", lambda _c: store)
    import core.db as db
    monkeypatch.setattr(db, "db_conn", object(), raising=False)
    out = ghost_server.invoke_tool("ghost_edge_report", {"view": "today"})
    assert out["forecasts"] == ["SHOP"]
    # I02: the MCP readout says which session it served and that it is not today's.
    assert out["served_session"] == "2026-09-23"
    assert out["requested_session"] != "2026-09-23" and out["stale"] is True


def _at(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=ET).timestamp())


def test_today_never_silently_serves_yesterdays_card():
    """I02: Oct 2 had no card; the today view served Oct 1 with no marker."""
    store = MemoryStore()
    store.put("edge_cards", "2026-10-01", {"day": "2026-10-01", "forecasts": ["AAA"], "rows": []})
    out = RO.view(store, "today", now=_at(2026, 10, 2, 11, 0))   # Fri, after the card window
    assert out["requested_session"] == "2026-10-02"
    assert out["served_session"] == "2026-10-01"
    assert out["stale"] is True
    assert "NOT today's" in out["note"] and "was due by 09:28 ET and is missing" in out["note"]
    s = RO.view(store, "summary", now=_at(2026, 10, 2, 11, 0))
    assert s["today"]["stale"] is True and s["today"]["requested_session"] == "2026-10-02"


def test_today_before_the_card_window_says_not_due_yet():
    store = MemoryStore()
    store.put("edge_cards", "2026-10-01", {"day": "2026-10-01", "rows": []})
    out = RO.view(store, "today", now=_at(2026, 10, 2, 8, 0))
    assert out["stale"] is True and "not due yet" in out["note"]


def test_today_matching_session_is_not_stale_and_weekend_maps_to_friday():
    store = MemoryStore()
    store.put("edge_cards", "2026-10-02", {"day": "2026-10-02", "forecasts": [], "rows": []})
    out = RO.view(store, "today", now=_at(2026, 10, 2, 10, 0))
    assert out["requested_session"] == out["served_session"] == "2026-10-02"
    assert out["stale"] is False and "note" not in out
    sat = RO.view(store, "today", now=_at(2026, 10, 3, 12, 0))   # Saturday
    assert sat["requested_session"] == "2026-10-02" and sat["stale"] is False


def test_today_explicit_day_is_the_requested_session():
    store = MemoryStore()
    store.put("edge_cards", "2026-10-01", {"day": "2026-10-01", "rows": []})
    out = RO.view(store, "today", "2026-10-01", now=_at(2026, 10, 2, 11, 0))
    assert out["requested_session"] == out["served_session"] == "2026-10-01"
    assert out["stale"] is False
    missing = RO.view(store, "today", "2026-09-30", now=_at(2026, 10, 2, 11, 0))
    assert missing["card"] is None and missing["served_session"] is None
    assert missing["requested_session"] == "2026-09-30"
