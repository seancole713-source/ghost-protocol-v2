from fastapi.testclient import TestClient

import wolf_app
from core.super_ghost_memory import (
    DEFAULT_FEATURE_SET_ID,
    DEFAULT_MODEL_ID,
    classify_feature_outcome,
    extract_feature_rows,
)


def _sample_report():
    return {
        "ok": True,
        "symbol": "WOLF",
        "engine": "super_ghost_checklist_v1",
        "ts": 1782750000,
        "prediction": {"direction": "UP", "confidence": 0.72, "action": "WATCHLIST UP BIAS", "edge_score": 14.0, "conviction_score": 62.0},
        "coverage": {"available": 3, "total": 25},
        "risk_plan": {"entry": 10.0, "target_price": 12.0, "stop_loss": 9.0},
        "checklist": [
            {"id": 1, "key": "eps", "category": "company", "source": "sec", "available": True, "status": "bullish", "score": 1.2, "confidence": 0.8, "weight": 1.1, "value": {"eps_yoy": 20}},
            {"id": 18, "key": "vix", "category": "market", "source": "market", "available": True, "status": "bearish", "score": -0.8, "confidence": 0.7, "weight": 0.8, "value": {"vix": 31}},
            {"id": 5, "key": "insider_trading", "category": "company", "source": "insider", "available": False, "status": "unknown", "score": None, "confidence": 0, "weight": 0.8, "value": None},
        ],
    }


def test_extract_feature_rows_from_super_ghost_checklist():
    rows = extract_feature_rows(_sample_report())
    assert len(rows) == 3
    by_name = {r["feature_name"]: r for r in rows}
    assert by_name["eps"]["model_id"] == DEFAULT_MODEL_ID
    assert by_name["eps"]["feature_set_id"] == DEFAULT_FEATURE_SET_ID
    assert by_name["eps"]["directional_effect"] == "bullish"
    assert by_name["eps"]["feature_importance"] > 0
    assert by_name["vix"]["directional_effect"] == "bearish"
    assert by_name["insider_trading"]["directional_effect"] == "missing"


def test_feature_outcome_classifies_help_hurt_underweighted_missing():
    align, effect, lesson = classify_feature_outcome(feature_score=1.0, feature_available=True, direction="UP", prediction_correct=True)
    assert align == "supports_prediction"
    assert effect == "helped"
    assert "positive evidence" in lesson

    align, effect, lesson = classify_feature_outcome(feature_score=1.0, feature_available=True, direction="UP", prediction_correct=False)
    assert align == "supports_prediction"
    assert effect == "hurt"
    assert "reduce trust" in lesson

    align, effect, lesson = classify_feature_outcome(feature_score=-1.0, feature_available=True, direction="UP", prediction_correct=False)
    assert align == "opposes_prediction"
    assert effect == "underweighted"
    assert "more weight" in lesson

    align, effect, lesson = classify_feature_outcome(feature_score=None, feature_available=False, direction="UP", prediction_correct=True)
    assert align == "missing"
    assert effect == "missing"


def test_models_endpoint(monkeypatch):
    def fake_models():
        return {"ok": True, "models": [{"model_id": DEFAULT_MODEL_ID, "status": "production"}]}

    monkeypatch.setattr("core.super_ghost_memory.list_models", fake_models)
    client = TestClient(wolf_app.APP)
    r = client.get("/api/wolf/super-ghost/models")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["models"][0]["model_id"] == DEFAULT_MODEL_ID


def test_features_endpoint(monkeypatch):
    def fake_features(symbol=None, limit=100):
        return {"ok": True, "symbol": symbol, "features": [{"feature_name": "eps", "score": 1.2}]}

    monkeypatch.setattr("core.super_ghost_memory.recent_features", fake_features)
    client = TestClient(wolf_app.APP)
    r = client.get("/api/wolf/super-ghost/features?symbol=WOLF")
    assert r.status_code == 200
    assert r.json()["features"][0]["feature_name"] == "eps"


def test_feature_profile_endpoint(monkeypatch):
    def fake_profile(symbol=None, horizon=5, limit=50):
        return {"ok": True, "symbol": symbol, "horizon_days": horizon, "profiles": [{"feature_name": "vix", "reliability": 0.7}]}

    monkeypatch.setattr("core.super_ghost_memory.feature_profile", fake_profile)
    client = TestClient(wolf_app.APP)
    r = client.get("/api/wolf/super-ghost/feature-profile?symbol=WOLF&horizon=5")
    assert r.status_code == 200
    assert r.json()["profiles"][0]["feature_name"] == "vix"


def test_features_score_requires_auth():
    client = TestClient(wolf_app.APP)
    r = client.post("/api/wolf/super-ghost/features/score", json={"symbol": "WOLF"})
    assert r.status_code in (401, 403)
