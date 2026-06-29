from fastapi.testclient import TestClient

import wolf_app
from core.super_ghost_range_calibration import (
    apply_range_calibration_to_report,
    derive_calibration_from_precision_profile,
)


def test_range_calibration_widens_target_after_target_too_low_profile():
    profile = {
        "symbol": "TEST",
        "horizon_days": 5,
        "direction": "UP",
        "sample_count": 8,
        "avg_precision_score": 58.0,
        "direction_win_rate": 0.75,
        "avg_abs_target_error_pct": 20.0,
        "avg_abs_stop_error_pct": 3.0,
        "primary_mistake_type": "target_too_low",
    }
    cal = derive_calibration_from_precision_profile(profile)
    assert cal["available"] is True
    assert cal["calibration_status"] == "widen_target"
    assert cal["target_move_multiplier"] > 1.0
    assert cal["stop_distance_multiplier"] == 1.0
    assert cal["range_width_pct"] > 0


def test_range_calibration_tightens_target_after_target_too_high_profile():
    profile = {
        "symbol": "TEST",
        "horizon_days": 5,
        "direction": "UP",
        "sample_count": 10,
        "avg_precision_score": 62.0,
        "direction_win_rate": 0.70,
        "avg_abs_target_error_pct": 30.0,
        "avg_abs_stop_error_pct": 4.0,
        "primary_mistake_type": "target_too_high",
    }
    cal = derive_calibration_from_precision_profile(profile)
    assert cal["calibration_status"] == "tighten_target"
    assert 0.80 <= cal["target_move_multiplier"] < 1.0


def test_apply_range_calibration_publishes_raw_and_calibrated_ranges():
    report = {
        "ok": True,
        "symbol": "TEST",
        "prediction": {"direction": "UP", "confidence": 0.72},
        "risk_plan": {"entry": 10.0, "target_price": 12.0, "stop_loss": 9.0, "risk_reward_ratio": 2.0},
    }
    profile = {
        "available": True,
        "symbol": "TEST",
        "horizon_days": 5,
        "direction": "UP",
        "sample_count": 8,
        "avg_precision_score": 58.0,
        "direction_win_rate": 0.75,
        "target_move_multiplier": 1.20,
        "stop_distance_multiplier": 0.90,
        "range_width_pct": 0.05,
        "calibration_status": "widen_target",
        "primary_mistake_type": "target_too_low",
        "primary_reason": "Targets too conservative",
    }
    out = apply_range_calibration_to_report(report, profile)
    risk = out["risk_plan"]
    assert out["range_calibration"]["applied"] is True
    assert risk["target_price_raw"] == 12.0
    assert risk["stop_loss_raw"] == 9.0
    assert risk["target_price_calibrated"] == 12.4
    assert risk["stop_loss_calibrated"] == 9.1
    assert risk["target_price"] == 12.4
    assert risk["stop_loss"] == 9.1
    assert len(risk["expected_high_range"]) == 2
    assert len(risk["expected_low_range"]) == 2
    assert risk["range_calibrated"] is True


def test_apply_range_calibration_cold_start_keeps_raw_risk_plan():
    report = {
        "ok": True,
        "symbol": "TEST",
        "prediction": {"direction": "UP"},
        "risk_plan": {"entry": 10.0, "target_price": 12.0, "stop_loss": 9.0, "risk_reward_ratio": 2.0},
    }
    out = apply_range_calibration_to_report(report, {"available": False, "sample_count": 2})
    assert out["range_calibration"]["applied"] is False
    assert out["risk_plan"]["target_price"] == 12.0
    assert "target_price_calibrated" not in out["risk_plan"]


def test_super_ghost_report_includes_range_calibration_cold_start():
    from core.super_ghost import build_super_ghost

    report = build_super_ghost("WOLF", snapshot={"symbol": "WOLF"})
    assert "range_calibration" in report
    assert report["range_calibration"]["applied"] is False


def test_super_ghost_applies_range_calibration_when_profile_available(monkeypatch):
    from core.super_ghost import build_super_ghost
    from tests.test_super_ghost import _sample_snapshot

    def fake_profile(symbol, direction, horizon=5):
        return {
            "available": True,
            "symbol": symbol,
            "direction": direction,
            "horizon_days": horizon,
            "sample_count": 8,
            "avg_precision_score": 61.0,
            "direction_win_rate": 0.75,
            "target_move_multiplier": 1.10,
            "stop_distance_multiplier": 1.0,
            "range_width_pct": 0.04,
            "calibration_status": "widen_target",
            "primary_reason": "Targets too conservative",
            "primary_mistake_type": "target_too_low",
        }

    monkeypatch.setattr("core.super_ghost_range_calibration.get_range_calibration_profile", fake_profile)
    report = build_super_ghost("WOLF", snapshot=_sample_snapshot())
    assert report["range_calibration"]["applied"] is True
    assert report["risk_plan"]["range_calibrated"] is True
    assert report["risk_plan"]["target_price_calibrated"] > report["risk_plan"]["target_price_raw"]


def test_range_calibration_endpoint(monkeypatch):
    def fake_summary(symbol=None, horizon=5, limit=20):
        return {"ok": True, "symbol": symbol or "ALL", "horizon_days": horizon, "profiles": [{"symbol": "WOLF", "calibration_status": "stable"}]}

    monkeypatch.setattr("core.super_ghost_range_calibration.range_calibration_summary", fake_summary)
    r = TestClient(wolf_app.APP).get("/api/wolf/super-ghost/range-calibration?symbol=WOLF&horizon=5")
    assert r.status_code == 200
    assert r.json()["profiles"][0]["calibration_status"] == "stable"


def test_range_calibration_rebuild_requires_auth():
    r = TestClient(wolf_app.APP).post("/api/wolf/super-ghost/range-calibration/rebuild", json={"symbol": "WOLF"})
    assert r.status_code in (401, 403)
