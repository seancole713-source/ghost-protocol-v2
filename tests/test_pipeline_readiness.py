"""Audit I02: pipeline readiness is reported separately from liveness."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core import pipeline_readiness as PR
from edge.intraday import TICK_TABLE
from edge.ledger import MemoryStore

ET = ZoneInfo("America/New_York")
FRI = "2026-10-02"


def at(hh, mm, d=2):
    return int(datetime(2026, 10, d, hh, mm, tzinfo=ET).timestamp())


def _wf_ok():
    return {"ok": True, "issues": [], "workers": {"online": 1, "expected": 1}, "stale_leases": 0}


def _scan(ts, symbols=100, fetch_ok=95):
    return lambda: {"status": "complete", "ts": ts, "symbols": symbols, "fetch_ok": fetch_ok}


def build(now, store, *, wf=_wf_ok, scan=None):
    return PR.build_readiness(now, store=store, workflow=wf, scan_report=scan or _scan(now - 60))


def test_missing_morning_card_after_0930_is_not_ready():
    """The 2026-10-02 case: no card for today while liveness read healthy."""
    store = MemoryStore()
    store.put("edge_cards", "2026-10-01", {"day": "2026-10-01"})
    store.put(TICK_TABLE, FRI, {"last_tick_at": at(10, 55), "last_ok_at": at(10, 55), "status": "watched"})
    out = build(at(11, 0), store)
    card = out["components"]["edge_morning_card"]
    assert card["status"] == "missing" and FRI in card["note"]
    assert out["ready"] is False and out["status"] == "not_ready"
    assert out["failing"] == ["edge_morning_card"]
    assert out["session"]["date"] == FRI and out["session"]["trading"] is True


def test_card_pending_before_0930_and_ok_when_present():
    store = MemoryStore()
    assert build(at(9, 10), store)["components"]["edge_morning_card"]["status"] == "pending"
    store.put("edge_cards", FRI, {"day": FRI, "issued_at": at(9, 6), "forecasts": ["AAA"]})
    c = build(at(9, 40), store)["components"]["edge_morning_card"]
    assert c["status"] == "ok" and c["forecasts"] == 1
    store.put("edge_cards", FRI, {"day": FRI, "issued_at": at(9, 25), "degraded": True})
    assert build(at(9, 40), store)["components"]["edge_morning_card"]["status"] == "degraded"


def test_weekend_nothing_is_due():
    out = build(at(12, 0, d=3), MemoryStore(), scan=_scan(at(16, 0)))
    assert out["components"]["edge_morning_card"]["status"] == "not_due"
    assert out["components"]["intraday_ticks"]["status"] == "not_due"
    assert out["components"]["scan_coverage"]["status"] == "ok"   # stale only matters in RTH
    assert out["ready"] is True


@pytest.mark.parametrize("now,last_ok,expected", [
    (at(11, 0), at(10, 55), "ok"),
    (at(11, 0), at(10, 30), "stale"),
    (at(14, 50), at(14, 25), "ok"),        # the radar stops ticking at 14:30
    (at(9, 50), None, "pending"),
    (at(15, 30), at(14, 25), "not_due"),
])
def test_intraday_tick_freshness(now, last_ok, expected):
    store = MemoryStore()
    store.put("edge_cards", FRI, {"day": FRI})
    if last_ok is not None:
        store.put(TICK_TABLE, FRI, {"last_tick_at": last_ok, "last_ok_at": last_ok, "status": "watched"})
    assert build(now, store)["components"]["intraday_ticks"]["status"] == expected


def test_intraday_missing_and_rate_limited_ticks_are_named():
    store = MemoryStore()
    store.put("edge_cards", FRI, {"day": FRI})
    assert build(at(11, 0), store)["components"]["intraday_ticks"]["status"] == "missing"
    store.put(TICK_TABLE, FRI, {"last_tick_at": at(10, 55), "last_ok_at": at(10, 0), "status": "error",
                                "errors": 4, "error": "RateLimited: alpaca rate limited (429) on /v2/stocks/bars",
                                "source_errors": {"alpaca": {"kind": "rate_limited", "path": "/v2/stocks/bars"}}})
    c = build(at(11, 0), store)["components"]["intraday_ticks"]
    assert c["status"] == "stale" and c["source_errors"]["alpaca"]["kind"] == "rate_limited"


def test_workflow_and_scan_coverage_degraded_states():
    store = MemoryStore()
    store.put("edge_cards", FRI, {"day": FRI})
    store.put(TICK_TABLE, FRI, {"last_ok_at": at(10, 58), "last_tick_at": at(10, 58)})
    out = build(at(11, 0), store,
                wf=lambda: {"ok": False, "issues": ["all_workers_offline"], "workers": {"online": 0}},
                scan=_scan(at(10, 59), symbols=105, fetch_ok=6))
    assert out["components"]["agent_workflow"]["status"] == "degraded"
    assert out["components"]["agent_workflow"]["issues"] == ["all_workers_offline"]
    sc = out["components"]["scan_coverage"]
    assert sc["status"] == "degraded" and sc["usable_symbols"] == 6 and sc["scanned_symbols"] == 105
    assert set(out["failing"]) == {"agent_workflow", "scan_coverage"}
    stale = build(at(11, 0), store, scan=_scan(at(10, 0)))["components"]["scan_coverage"]
    assert stale["status"] == "stale"


def test_a_failing_component_is_unknown_never_raises():
    def boom():
        raise RuntimeError("db down")
    out = build(at(11, 0), MemoryStore(), wf=boom)
    assert out["components"]["agent_workflow"]["status"] == "unknown"
    assert "db down" in out["components"]["agent_workflow"]["error"]


def test_public_slim_view_hides_detail():
    store = MemoryStore()
    out = build(at(11, 0), store, wf=lambda: {"ok": False, "issues": ["x"], "workers": {"online": 0}})
    s = PR.slim(out)
    assert s["ready"] is False and s["components"]["agent_workflow"] == {"status": "degraded"}
    assert "workers" not in str(s)


def test_liveness_never_reads_readiness(monkeypatch):
    """/health and /api/health keep their own score; readiness only rides along in the
    admin full payload and its own endpoint."""
    import wolf_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("GHOST_TEST_MODE", "1")
    monkeypatch.setattr(wolf_app, "_health_db_ping", lambda: True)
    monkeypatch.setattr(wolf_app, "health", lambda: {"status": "healthy", "score": 100})
    wolf_app._HEALTH_FULL_CACHE.update({"t": 0.0, "v": None})
    not_ready = {"ready": False, "status": "not_ready", "failing": ["edge_morning_card"],
                 "components": {"edge_morning_card": {"status": "missing", "note": "n"}},
                 "session": {"date": FRI, "trading": True, "early_close": False}}
    monkeypatch.setattr(PR, "build_readiness", lambda *a, **k: not_ready)
    wolf_app._READINESS_CACHE.update({"t": 0.0, "v": None})
    with TestClient(wolf_app.APP) as client:
        h = client.get("/health")
        assert h.status_code == 200 and h.json()["status"] == "healthy" and h.json()["score"] == 100
        assert "readiness" not in h.json()
        r = client.get("/api/readiness")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is False and body["failing"] == ["edge_morning_card"]
        assert body["components"]["edge_morning_card"]["status"] == "missing"
    wolf_app._READINESS_CACHE.update({"t": 0.0, "v": None})
