from fastapi.testclient import TestClient

import wolf_app
from core.super_ghost_shadow import (
    run_shadow_models,
    shadow_manifest,
    technical_shadow,
    ensemble_shadow,
)


def _item(key, score, category="price_action_performance", available=True):
    return {"key": key, "score": score, "category": category, "available": available, "weight": 1.0, "confidence": 0.8, "evidence": key}


def _report():
    return {
        "ok": True,
        "symbol": "WOLF",
        "engine": "super_ghost_checklist_v1",
        "ts": 1782750000,
        "prediction": {"direction": "UP", "confidence": 0.72, "action": "WATCHLIST UP BIAS"},
        "market_regime": {"label": "risk_on", "risk_state": "risk_on"},
        "risk_plan": {"entry": 10.0, "target_price": 12.0, "stop_loss": 9.0},
        "learning_adjustment": {"available": False, "status": "cold_start"},
        "checklist": [
            _item("perf_30d", 1.2), _item("relative_strength", 1.0), _item("moving_averages", 1.1),
            _item("rvol", 0.8), _item("news_catalysts", 0.9, "company_fundamentals_news"),
            _item("eps", 1.2, "company_fundamentals_news"), _item("revenue_growth", 1.0, "company_fundamentals_news"),
            _item("spx", 0.8, "market_context_indicators"), _item("sector", 0.9, "market_context_indicators"),
            _item("vix", 0.7, "market_context_indicators"),
        ],
    }


def test_shadow_manifest_lists_specialist_models():
    ids = {m["model_id"] for m in shadow_manifest()}
    assert "technical_shadow_v1" in ids
    assert "news_shadow_v1" in ids
    assert "fundamental_shadow_v1" in ids
    assert "macro_shadow_v1" in ids
    assert "regime_shadow_v1" in ids
    assert "learning_adjusted_shadow_v1" in ids
    assert "ensemble_shadow_v1" in ids


def test_technical_shadow_reads_price_features():
    pred = technical_shadow(_report())
    assert pred["model_id"] == "technical_shadow_v1"
    assert pred["direction"] == "UP"
    assert pred["confidence"] > 0.5
    assert pred["reference_price"] == 10.0
    assert "Technical edge" in pred["reason"]


def test_run_shadow_models_returns_all_specialists():
    preds = run_shadow_models(_report())
    assert len(preds) == 7
    ids = {p["model_id"] for p in preds}
    assert "ensemble_shadow_v1" in ids
    assert all("direction" in p and "confidence" in p for p in preds)


def test_ensemble_shadow_votes_committee():
    pred = ensemble_shadow(_report())
    assert pred["model_id"] == "ensemble_shadow_v1"
    assert pred["direction"] in {"UP", "HOLD", "DOWN"}
    assert "Specialist vote" in pred["reason"]


def test_shadow_summary_endpoint(monkeypatch):
    def fake_summary(symbol=None, limit=50):
        return {"ok": True, "symbol": symbol or "ALL", "manifest": [{"model_id": "technical_shadow_v1"}], "rows": []}

    monkeypatch.setattr("core.super_ghost_shadow.shadow_summary", fake_summary)
    client = TestClient(wolf_app.APP)
    r = client.get("/api/wolf/super-ghost/shadow?symbol=WOLF")
    assert r.status_code == 200
    assert r.json()["manifest"][0]["model_id"] == "technical_shadow_v1"


def test_shadow_models_endpoint(monkeypatch):
    def fake_profiles():
        return {"ok": True, "profiles": [{"model_id": "technical_shadow_v1", "sample_count": 3}], "manifest": []}

    monkeypatch.setattr("core.super_ghost_shadow.shadow_model_profiles", fake_profiles)
    client = TestClient(wolf_app.APP)
    r = client.get("/api/wolf/super-ghost/shadow/models")
    assert r.status_code == 200
    assert r.json()["profiles"][0]["model_id"] == "technical_shadow_v1"


def test_shadow_post_routes_require_auth():
    client = TestClient(wolf_app.APP)
    r1 = client.post("/api/wolf/super-ghost/shadow/run", json={"symbol": "WOLF"})
    r2 = client.post("/api/wolf/super-ghost/shadow/resolve", json={"symbol": "WOLF"})
    assert r1.status_code in (401, 403)
    assert r2.status_code in (401, 403)
