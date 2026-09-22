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
    lg.settle(f.forecast_id, Resolution("WIN", pnl_usd=48.0), now=ts(16, 25), record="simulated")
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
