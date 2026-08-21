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


def test_cron_trigger_endpoints_are_gated():
    """snapshot-run and resolve must 403 without the cron secret."""
    c = _client()
    assert c.post("/api/ghost/checklist/snapshot-run").status_code == 403
    assert c.post("/api/ghost/checklist/resolve").status_code == 403


def test_collect_evidence_builds_fallback_market_ctx(monkeypatch):
    """Regression for the dead-veto finding: with no caller-supplied ctx,
    collect_evidence must build one itself so positioning/confirmation boxes
    and the already-ran/thin-liquidity vetoes can actually see the market."""
    import core.checklist_evidence as ce

    seen = {}

    def _fake_ctx(sym):
        seen["sym"] = sym
        return {
            "price": 10.0, "prior_close": 8.0,
            "session_volume": 5_000_000.0, "avg_daily_volume": 1_000_000.0,
            "peak_move_pct": 25.0, "short_float_pct": 30.0, "days_to_cover": 4.0,
        }

    monkeypatch.setattr(ce, "_default_market_ctx", _fake_ctx)
    # keep the network collectors quiet
    for name in ("_collect_earnings", "_collect_fundamentals", "_collect_news",
                 "_collect_leadership_change"):
        monkeypatch.setattr(ce, name, lambda sym: {})

    ev = ce.collect_evidence("DOMO")
    assert seen["sym"] == "DOMO"
    assert ev["relative_volume"] == 5.0
    assert ev["move_from_base_pct"] == 25.0  # the already-ran veto can now trip
    assert ev["short_float_pct"] == 30.0

    from core.catalyst_checklist import evaluate_checklist
    report = evaluate_checklist("DOMO", "UP", ev)
    assert report["blocked"] is True
    assert "already_ran" in report["blocked_by"]
