from fastapi.testclient import TestClient

import wolf_app
from core.super_ghost_lab import benchmark_candidates, candidate_manifest, evaluate_candidate, CANDIDATES


def _row(i, ret, *, confidence=0.55, coverage=21, action="WATCHLIST UP BIAS", grade="C"):
    return {
        "id": i,
        "symbol": "TEST",
        "direction": "UP",
        "action": action,
        "confidence": confidence,
        "checklist_coverage": coverage,
        "accuracy_grade": grade,
        "edge_score": None,
        "regime_risk_state": "neutral",
        "return_5d_pct": ret,
    }


def _candidate(name):
    return next(c for c in CANDIDATES if c.name == name)


def test_candidate_manifest_lists_shadow_policies():
    names = {c["name"] for c in candidate_manifest()}
    assert "production_champion" in names
    assert "strict_confidence" in names
    assert "regime_aligned" in names
    assert "edge_score_policy" in names


def test_evaluate_candidate_scores_if_followed_returns():
    rows = [_row(1, 5.0), _row(2, -2.0), _row(3, 3.0)]
    result = evaluate_candidate(rows, _candidate("production_champion"), horizon=5)
    assert result["rows_evaluated"] == 3
    assert result["actionable_count"] == 3
    assert result["wins"] == 2
    assert result["losses"] == 1
    assert result["win_rate"] == 0.6667
    assert result["net_return_pct"] == 6.0


def test_benchmark_refuses_recommendation_with_too_few_rows():
    rows = [_row(i, 4.0, confidence=0.8) for i in range(5)]
    out = benchmark_candidates(rows, horizon=5, min_rows=30)
    assert out["recommendation_status"] == "insufficient_rows"
    assert out["recommended_candidate"] is None


def test_benchmark_can_recommend_shadow_challenger_when_it_beats_champion():
    # Production acts on all 30 rows: 10 wins (+4%) and 20 losses (-2%) => weak.
    # strict_confidence acts only on 10 high-confidence wins => strong challenger.
    rows = []
    for i in range(20):
        rows.append(_row(i, -2.0, confidence=0.55, coverage=21, grade="C"))
    for i in range(20, 30):
        rows.append(_row(i, 4.0, confidence=0.80, coverage=21, grade="C"))
    out = benchmark_candidates(rows, horizon=5, min_rows=30)
    assert out["rows_evaluated"] == 30
    assert out["recommendation_status"] == "challenger_candidate"
    assert out["recommended_candidate"] == "strict_confidence"
    by_name = {r["candidate"]: r for r in out["results"]}
    assert by_name["production_champion"]["win_rate"] == 0.3333
    assert by_name["strict_confidence"]["win_rate"] == 1.0
    assert by_name["strict_confidence"]["actionable_count"] == 10


def test_lab_summary_endpoint(monkeypatch):
    def fake_summary(symbol=None, horizon=5):
        return {"ok": True, "available": True, "symbol": symbol or "ALL", "horizon_days": horizon, "recommendation_status": "keep_champion", "results": []}

    monkeypatch.setattr("core.super_ghost_lab.latest_lab_summary", fake_summary)
    client = TestClient(wolf_app.APP)
    r = client.get("/api/wolf/super-ghost/lab?symbol=WOLF&horizon=5")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["symbol"] == "WOLF"
    assert d["recommendation_status"] == "keep_champion"


def test_lab_run_endpoint_requires_auth():
    client = TestClient(wolf_app.APP)
    r = client.post("/api/wolf/super-ghost/lab/run", json={"symbol": "WOLF"})
    assert r.status_code in (401, 403)
