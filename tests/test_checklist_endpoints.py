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
    monkeypatch.setattr(ce, "collect_evidence", lambda sym, **kwargs: {})

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
    monkeypatch.setattr(ce, "collect_evidence", lambda sym, **kwargs: {})

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


def test_authenticated_snapshot_run_is_retired(monkeypatch):
    import wolf_app

    monkeypatch.setattr(wolf_app, "_cron_ok", lambda supplied, strict=True: True)
    r = _client().post("/api/ghost/checklist/snapshot-run", headers={"x-cron-secret": "valid"})
    assert r.status_code == 410
    assert r.json()["reason"] == "checklists_are_frozen_at_prediction_issuance"


def test_live_endpoint_marks_calibration_database_failure_unavailable(monkeypatch):
    import core.checklist_evidence as ce
    import core.checklist_ledger as ledger

    monkeypatch.setattr(ce, "collect_evidence", lambda sym, **kwargs: {})
    monkeypatch.setattr(
        ledger,
        "resolved_samples_for_calibration",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    body = _client().get("/api/ghost/checklist/WOLF?direction=UP").json()
    assert body["confidence"]["calibration_status"] == "unavailable"
    assert body["confidence"]["confidence_pct"] is None
    assert "temporarily unavailable" in body["confidence"]["explanation"]


def test_immutable_prediction_endpoint_uses_prior_exact_cohort_only(monkeypatch):
    import core.checklist_ledger as ledger

    snapshot = {
        "checklist_version": "v1",
        "hold_bars": 3,
        "outcome_contract": "contract-a",
        "direction": "DOWN",
        "issued_at": 1_766_000_123,
        "score_pct": 80.0,
        "evidence": {"relative_volume": 3.0},
        "report": {"score_pct": 80.0, "groups": []},
    }
    captured = {}
    monkeypatch.setattr(ledger, "snapshot_for_prediction", lambda prediction_id: snapshot)

    def fake_samples(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(ledger, "resolved_samples_for_calibration", fake_samples)
    r = _client().get("/api/ghost/checklist/prediction/42")
    assert r.status_code == 200
    body = r.json()
    assert body["snapshot_semantics"] == "immutable_at_prediction_issuance"
    assert body["prediction_id"] == 42
    assert body["evidence"] == snapshot["evidence"]
    assert captured == {
        "checklist_version": "v1",
        "hold_bars": 3,
        "outcome_contract": "contract-a",
        "direction": "DOWN",
        "min_issued_before": 1_766_000_123,
    }


def test_collect_evidence_builds_fallback_market_ctx(monkeypatch):
    """Fallback data without a genuine provider timestamp remains UNKNOWN."""
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

    ev = ce.collect_evidence("DOMO", asof_ts=1_766_000_100)
    assert seen["sym"] == "DOMO"
    assert "relative_volume" not in ev
    assert ev[ce.RECORDS_KEY]["relative_volume"][0]["confidence_status"] == "UNVERIFIED"
    assert "move_from_base_pct" not in ev
    assert "short_float_pct" not in ev

    from core.catalyst_checklist import evaluate_checklist
    report = evaluate_checklist("DOMO", "UP", ev)
    assert report["blocked"] is False
    assert all(v["state"] == "unknown" for v in report["vetoes"])
