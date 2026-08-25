"""Unified timeline failure isolation and API truthfulness."""
from __future__ import annotations

from contextlib import AbstractContextManager

from fastapi.testclient import TestClient

import core.db as db
import core.squeeze_monitor as squeeze_monitor
import core.symbol_timeline as timeline
import wolf_app


class _Cursor:
    def __init__(self, rows=None, error=None):
        self.rows = list(rows or [])
        self.error = error

    def execute(self, sql, params=None):
        if self.error:
            raise self.error

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class _Ctx(AbstractContextManager):
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        return False


def _patch_connections(monkeypatch, cursors):
    queue = list(cursors)
    monkeypatch.setattr(db, "db_conn", lambda: _Ctx(_Conn(queue.pop(0))))
    monkeypatch.setattr(
        squeeze_monitor,
        "get_squeeze_status",
        lambda: {"watches": [], "candidates": [], "leaders": []},
    )


def test_timeline_isolates_one_failed_database_surface(monkeypatch):
    _patch_connections(monkeypatch, [
        _Cursor(error=RuntimeError("relation missing secret detail")),
        _Cursor(rows=[(2000, 12.0, "watch", 61.0)]),
        _Cursor(rows=[]),
        _Cursor(rows=[]),
    ])

    result = timeline.build_symbol_timeline(" arct ")

    assert result["ok"] is True
    assert result["status"] == "partial"
    assert result["symbol"] == "ARCT"
    assert result["event_count"] == 1
    assert result["events"][0]["surface"] == "observation"
    assert result["failed_sources"] == ["squeeze"]
    assert result["sources"]["squeeze"]["error"] == "database_query_failed"
    assert "secret" not in str(result)
    assert result["sources"]["observation"]["status"] == "available"
    assert result["sources"]["news"]["status"] == "available"
    assert result["sources"]["external"]["status"] == "available"


def test_timeline_external_discovery_is_advisory_only(monkeypatch):
    external_row = (
        3000, 3010, "yahoo_saved_screener", "yahoo", "day_gainers",
        "fresh", False, True, [], True, False, 1, 22.5, 18.0,
        2_000_000.0, 500_000.0, 18.0, "day_gainers:ARCT:3000",
    )
    _patch_connections(monkeypatch, [
        _Cursor(), _Cursor(), _Cursor(), _Cursor(rows=[external_row]),
    ])

    result = timeline.build_symbol_timeline("ARCT")

    assert result["status"] == "complete"
    assert result["event_count"] == 1
    event = result["events"][0]
    assert event["surface"] == "external_discovery"
    assert event["advisory_only"] is True
    assert event["decision_eligible"] is False
    assert event["in_official_watchlist"] is True


def test_timeline_empty_success_is_complete(monkeypatch):
    _patch_connections(monkeypatch, [_Cursor(), _Cursor(), _Cursor(), _Cursor()])
    result = timeline.build_symbol_timeline("WOLF")
    assert result["status"] == "complete"
    assert result["ok"] is True
    assert result["event_count"] == 0
    assert all(
        result["sources"][name]["status"] == "available"
        for name in ("squeeze", "observation", "news", "external", "current")
    )


def test_timeline_all_database_surfaces_unavailable(monkeypatch):
    _patch_connections(monkeypatch, [
        _Cursor(error=RuntimeError("one")),
        _Cursor(error=RuntimeError("two")),
        _Cursor(error=RuntimeError("three")),
        _Cursor(error=RuntimeError("four")),
    ])
    result = timeline.build_symbol_timeline("WOLF")
    assert result["status"] == "unavailable"
    assert result["ok"] is False
    assert result["error"] == "timeline_unavailable"


def test_timeline_rejects_invalid_symbol_before_database(monkeypatch):
    monkeypatch.setattr(db, "db_conn", lambda: (_ for _ in ()).throw(AssertionError("DB touched")))
    try:
        timeline.build_symbol_timeline("WOLF;DROP")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid symbol accepted")


def test_timeline_route_maps_partial_unavailable_and_invalid(monkeypatch):
    monkeypatch.setenv("GHOST_TEST_MODE", "1")
    partial = {
        "ok": True, "status": "partial", "symbol": "WOLF", "events": [],
        "event_count": 0, "current": {}, "sources": {}, "failed_sources": ["news"],
    }
    monkeypatch.setattr(timeline, "build_symbol_timeline", lambda symbol: partial)
    with TestClient(wolf_app.APP) as client:
        response = client.get("/api/symbol/timeline?symbol=WOLF")
    assert response.status_code == 200
    assert response.json()["status"] == "partial"

    unavailable = dict(partial, ok=False, status="unavailable")
    monkeypatch.setattr(timeline, "build_symbol_timeline", lambda symbol: unavailable)
    with TestClient(wolf_app.APP) as client:
        response = client.get("/api/symbol/timeline?symbol=WOLF")
    assert response.status_code == 503

    def invalid(symbol):
        raise ValueError("do not leak this detail")

    monkeypatch.setattr(timeline, "build_symbol_timeline", invalid)
    with TestClient(wolf_app.APP) as client:
        response = client.get("/api/symbol/timeline?symbol=bad%3Bsymbol")
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_symbol"
