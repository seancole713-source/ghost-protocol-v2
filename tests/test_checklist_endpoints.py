"""Endpoint-level tests: the API must never fabricate confidence or evidence."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client():
    from api.routes_ghost_system import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_spec_endpoint_lists_all_five_groups():
    r = _client().get("/api/ghost/checklist/spec")
    assert r.status_code == 200
    body = r.json()
    assert len(body["groups"]) == 5
    assert body["hold_bars"] == 3


def test_symbol_endpoint_rejects_bad_direction():
    r = _client().get("/api/ghost/checklist/AAPL?direction=SIDEWAYS")
    assert r.status_code == 400


def test_symbol_endpoint_never_fabricates_confidence_with_no_data(monkeypatch):
    """With no live sources reachable, every box is unknown and score is 0 --
    never a plausible-looking placeholder number."""
    import core.checklist_evidence as ce
    monkeypatch.setattr(ce, "collect_evidence", lambda sym, market_ctx=None: {})

    r = _client().get("/api/ghost/checklist/ZZZZ?direction=UP")
    assert r.status_code == 200
    body = r.json()
    assert body["score_pct"] == 0.0
    assert body["confidence"]["confidence_pct"] is None
    assert body["evidence_coverage_pct"] == 0.0


def test_leadership_change_absent_from_evidence_reads_unknown_not_neutral(monkeypatch):
    """No 8-K item 5.02 filing must leave the box unknown, never a false pass
    or fail from an assumed neutral 0.0."""
    import core.checklist_evidence as ce
    monkeypatch.setattr(ce, "collect_evidence", lambda sym, market_ctx=None: {})

    r = _client().get("/api/ghost/checklist/ZZZZ?direction=UP")
    body = r.json()
    box = next(
        b for g in body["groups"] for b in g["boxes"] if b["key"] == "leadership_change"
    )
    assert box["state"] == "unknown"


def test_symbol_endpoint_requires_a_symbol():
    r = _client().get("/api/ghost/checklist/ ")
    assert r.status_code in (400, 404)


def test_global_record_route_is_registered_before_symbol_wildcard():
    """Regression: /api/ghost/checklist/record must resolve to the global
    record endpoint, not be swallowed by /{symbol} matching 'record' as a
    ticker. Route order in the source file is what fixes this -- this test
    asserts the resulting behavior, not the line order itself."""
    from api.routes_ghost_system import router

    record_routes = [r for r in router.routes if getattr(r, "path", "") == "/api/ghost/checklist/record"]
    symbol_routes = [r for r in router.routes if getattr(r, "path", "") == "/api/ghost/checklist/{symbol}"]
    assert record_routes and symbol_routes
    record_index = router.routes.index(record_routes[0])
    symbol_index = router.routes.index(symbol_routes[0])
    assert record_index < symbol_index


def test_global_record_endpoint_reachable(monkeypatch):
    import core.checklist_ledger as ckl
    monkeypatch.setattr(ckl, "recent_resolved_across_symbols", lambda limit=30: [])

    r = _client().get("/api/ghost/checklist/record")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "snapshots": []}
