from fastapi.testclient import TestClient

import wolf_app
from core.super_ghost_top_picks import evaluate_top_pick_gate


def test_top_pick_gate_locks_when_cold_start(monkeypatch):
    monkeypatch.setattr("core.super_ghost_ledger.get_accuracy", lambda symbol=None, horizon=5: {"ok": True, "overall": {"n": 0, "win_rate": None}})
    monkeypatch.setattr("core.super_ghost_ledger.get_if_followed", lambda symbol=None, horizon=5: {"ok": True, "followed_calls": 0})
    monkeypatch.setattr("core.super_ghost_precision.precision_summary", lambda symbol=None, horizon=5, limit=20: {"ok": True, "primary_profile": None})
    monkeypatch.setattr("core.super_ghost_range_calibration.range_calibration_summary", lambda symbol=None, horizon=5, limit=20: {"ok": True, "primary_profile": None})
    monkeypatch.setattr("core.super_ghost_regime_calibration.regime_calibration_summary", lambda symbol=None, horizon=5, limit=20: {"ok": True, "primary_profile": None})
    monkeypatch.setattr("core.prediction.evaluate_kill_conditions", lambda include_pause=True: {"ok": True, "any_triggered": False, "engine_pause": {"paused": False}})
    out = evaluate_top_pick_gate("WOLF")
    assert out["eligible"] is False
    assert out["decision"] == "LOCKED"
    assert any(c["key"] == "completed_predictions" and not c["passed"] for c in out["checks"])


def test_top_pick_gate_unlocks_only_with_full_evidence(monkeypatch):
    monkeypatch.setattr("core.super_ghost_ledger.get_accuracy", lambda symbol=None, horizon=5: {"ok": True, "overall": {"n": 8, "win_rate": 0.75}})
    monkeypatch.setattr("core.super_ghost_ledger.get_if_followed", lambda symbol=None, horizon=5: {"ok": True, "followed_calls": 8, "profit_factor": 1.4, "net_return_pct": 12.0})
    monkeypatch.setattr("core.super_ghost_precision.precision_summary", lambda symbol=None, horizon=5, limit=20: {"ok": True, "primary_profile": {"sample_count": 8, "avg_precision_score": 70.0}})
    monkeypatch.setattr("core.super_ghost_range_calibration.range_calibration_summary", lambda symbol=None, horizon=5, limit=20: {"ok": True, "primary_profile": {"sample_count": 8, "available": True}})
    monkeypatch.setattr("core.super_ghost_regime_calibration.regime_calibration_summary", lambda symbol=None, horizon=5, limit=20: {"ok": True, "primary_profile": None})
    monkeypatch.setattr("core.prediction.evaluate_kill_conditions", lambda include_pause=True: {"ok": True, "any_triggered": False, "engine_pause": {"paused": False}})
    out = evaluate_top_pick_gate("WOLF")
    assert out["eligible"] is True
    assert out["decision"] == "ELIGIBLE_FOR_TOP_PICKS"
    assert all(c["passed"] for c in out["checks"])


def test_top_pick_gate_blocks_on_kill_condition(monkeypatch):
    monkeypatch.setattr("core.super_ghost_ledger.get_accuracy", lambda symbol=None, horizon=5: {"ok": True, "overall": {"n": 8, "win_rate": 0.75}})
    monkeypatch.setattr("core.super_ghost_ledger.get_if_followed", lambda symbol=None, horizon=5: {"ok": True, "followed_calls": 8, "profit_factor": 1.4, "net_return_pct": 12.0})
    monkeypatch.setattr("core.super_ghost_precision.precision_summary", lambda symbol=None, horizon=5, limit=20: {"ok": True, "primary_profile": {"sample_count": 8, "avg_precision_score": 70.0}})
    monkeypatch.setattr("core.super_ghost_range_calibration.range_calibration_summary", lambda symbol=None, horizon=5, limit=20: {"ok": True, "primary_profile": {"sample_count": 8, "available": True}})
    monkeypatch.setattr("core.super_ghost_regime_calibration.regime_calibration_summary", lambda symbol=None, horizon=5, limit=20: {"ok": True, "primary_profile": None})
    monkeypatch.setattr("core.prediction.evaluate_kill_conditions", lambda include_pause=True: {"ok": True, "any_triggered": True, "engine_pause": {"paused": False}})
    out = evaluate_top_pick_gate("WOLF")
    assert out["eligible"] is False
    assert any(c["key"] == "kill_conditions_clear" and not c["passed"] for c in out["checks"])


def test_top_pick_gate_endpoint(monkeypatch):
    monkeypatch.setattr("core.super_ghost_top_picks.evaluate_top_pick_gate", lambda symbol, horizon=5: {"ok": True, "symbol": symbol, "eligible": False, "checks": []})
    r = TestClient(wolf_app.APP).get("/api/wolf/super-ghost/top-pick-gate?symbol=WOLF&horizon=5")
    assert r.status_code == 200
    assert r.json()["symbol"] == "WOLF"
