from fastapi.testclient import TestClient

import wolf_app
from core.super_ghost_promotion import review_promotion, normalize_metrics


def _metrics(**kw):
    base = {
        "candidate": "strict_confidence",
        "rows_evaluated": 80,
        "actionable_count": 25,
        "wins": 18,
        "losses": 7,
        "win_rate": 0.72,
        "false_positive_rate": 0.28,
        "avg_signed_return_pct": 1.2,
        "profit_factor": 1.8,
        "max_drawdown_pct": 4.0,
        "score": 1.0,
    }
    base.update(kw)
    return base


def test_promotion_refuses_too_few_rows():
    out = review_promotion(_metrics(rows_evaluated=12, actionable_count=10), _metrics(candidate="production_champion"))
    assert out["decision"] == "INSUFFICIENT_EVIDENCE"
    assert out["approved_for_promotion"] is False


def test_promotion_keeps_shadowing_when_actionable_too_low():
    out = review_promotion(_metrics(rows_evaluated=80, actionable_count=4), _metrics(candidate="production_champion"))
    assert out["decision"] == "KEEP_SHADOWING"
    assert "actionable" in out["reason"]


def test_promotion_keeps_champion_when_improvement_too_small():
    candidate = _metrics(win_rate=0.64, avg_signed_return_pct=0.7)
    champion = _metrics(candidate="production_champion", win_rate=0.62, avg_signed_return_pct=0.6, max_drawdown_pct=4.0)
    out = review_promotion(candidate, champion)
    assert out["decision"] == "KEEP_CHAMPION"
    assert out["approved_for_promotion"] is False


def test_promotion_can_approve_candidate_when_all_gates_clear():
    candidate = _metrics(win_rate=0.72, avg_signed_return_pct=1.4, profit_factor=2.0, false_positive_rate=0.28, max_drawdown_pct=4.0)
    champion = _metrics(candidate="production_champion", win_rate=0.60, avg_signed_return_pct=0.8, profit_factor=1.3, false_positive_rate=0.40, max_drawdown_pct=5.0)
    out = review_promotion(candidate, champion)
    assert out["decision"] == "PROMOTE_CANDIDATE"
    assert out["approved_for_promotion"] is True
    assert out["requires_more_shadowing"] is False


def test_promotion_retires_poor_candidate():
    out = review_promotion(_metrics(win_rate=0.30, false_positive_rate=0.70, profit_factor=0.5, avg_signed_return_pct=-1.0), _metrics(candidate="production_champion"))
    assert out["decision"] == "RETIRE_CANDIDATE"


def test_normalize_metrics_handles_shadow_profile_names():
    raw = {"model_id": "technical_shadow_v1", "sample_count": 60, "actionable_count": 20, "wins": 12, "losses": 8, "avg_signed_return_pct": 0.9}
    n = normalize_metrics(raw)
    assert n["id"] == "technical_shadow_v1"
    assert n["win_rate"] == 0.6
    assert n["false_positive_rate"] == 0.4


def test_promotion_get_endpoint(monkeypatch):
    def fake_reviews(symbol=None, limit=20):
        return {"ok": True, "reviews": [{"candidate_id": "strict_confidence", "decision": "KEEP_SHADOWING"}]}

    monkeypatch.setattr("core.super_ghost_promotion.latest_promotion_reviews", fake_reviews)
    client = TestClient(wolf_app.APP)
    r = client.get("/api/wolf/super-ghost/promotion?symbol=WOLF")
    assert r.status_code == 200
    assert r.json()["reviews"][0]["decision"] == "KEEP_SHADOWING"


def test_promotion_review_requires_auth():
    client = TestClient(wolf_app.APP)
    r = client.post("/api/wolf/super-ghost/promotion/review", json={"symbol": "WOLF"})
    assert r.status_code in (401, 403)
