from fastapi.testclient import TestClient

import wolf_app
from core.super_ghost_feature_store import build_feature_snapshot, parse_source_ts


def _report(ts=1_800_000_000):
    return {
        "ok": True,
        "symbol": "WOLF",
        "engine": "super_ghost_checklist_v1",
        "ts": ts,
        "prediction": {"direction": "UP", "confidence": 0.7},
        "coverage": {"available": 21, "total": 25},
        "risk_plan": {"entry": 10, "target_price": 12, "stop_loss": 9},
        "market_regime": {"label": "risk_on", "checked_at": ts - 20},
        "top_drivers": {"bullish": []},
        "learning_adjustment": {"available": False},
        "checklist": [
            {"id": 1, "key": "eps", "category": "company", "source": "sec", "available": True, "score": 1.2, "value": {"filed_at": ts - 100}},
            {"id": 2, "key": "news_catalysts", "category": "news", "source": "news", "available": True, "score": 0.8, "value": {"published_at": ts - 10}},
        ],
    }


def test_parse_source_ts_handles_seconds_millis_and_iso():
    assert parse_source_ts(1_800_000_000) == 1_800_000_000
    assert parse_source_ts(1_800_000_000_000) == 1_800_000_000
    assert parse_source_ts("2026-06-29T12:00:00+00:00") is not None
    assert parse_source_ts("bad") is None


def test_build_feature_snapshot_clean_when_sources_not_after_prediction():
    snap = build_feature_snapshot(_report())
    assert snap["ok"] is True
    assert snap["symbol"] == "WOLF"
    assert snap["source_time_ok"] is True
    assert snap["leak_count"] == 0
    assert snap["feature_asof_ts"] <= snap["prediction_ts"]
    assert snap["snapshot"]["checklist"][0]["key"] == "eps"


def test_build_feature_snapshot_detects_future_leakage():
    report = _report()
    report["checklist"][1]["value"]["published_at"] = report["ts"] + 3600
    snap = build_feature_snapshot(report)
    assert snap["source_time_ok"] is False
    assert snap["leak_count"] >= 1
    assert any("published_at" in src["path"] for src in snap["future_sources"])


def test_feature_store_endpoints(monkeypatch):
    def fake_latest(symbol=None, limit=50):
        return {"ok": True, "symbol": symbol or "ALL", "snapshots": [{"id": 1, "source_time_ok": True}]}

    def fake_audit(symbol=None, limit=200):
        return {"ok": True, "symbol": symbol or "ALL", "checked": 1, "leak_count": 0, "status": "clean", "leaks": []}

    monkeypatch.setattr("core.super_ghost_feature_store.latest_snapshots", fake_latest)
    monkeypatch.setattr("core.super_ghost_feature_store.leakage_audit", fake_audit)
    client = TestClient(wolf_app.APP)
    r = client.get("/api/wolf/super-ghost/feature-store?symbol=WOLF")
    assert r.status_code == 200
    assert r.json()["snapshots"][0]["source_time_ok"] is True
    r = client.get("/api/wolf/super-ghost/feature-store/audit?symbol=WOLF")
    assert r.status_code == 200
    assert r.json()["status"] == "clean"


def test_feature_store_snapshot_requires_auth():
    client = TestClient(wolf_app.APP)
    r = client.post("/api/wolf/super-ghost/feature-store/snapshot", json={"symbol": "WOLF"})
    assert r.status_code in (401, 403)
