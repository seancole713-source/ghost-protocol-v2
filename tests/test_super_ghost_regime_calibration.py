from fastapi.testclient import TestClient

import wolf_app
from core.super_ghost_regime_calibration import (
    apply_regime_calibration_to_report,
    derive_regime_calibration,
    regime_bucket,
    setup_bucket,
)


def _event(i, *, precision=70, result="WIN", mistake="target_too_low", target_error=12.0):
    return {
        "ledger_id": i,
        "symbol": "TEST",
        "horizon_days": 5,
        "direction": "UP",
        "direction_result": result,
        "direction_correct": result == "WIN",
        "precision_score": precision,
        "overall_score": precision,
        "mistake_type": mistake,
        "target_error_pct": target_error,
        "stop_error_pct": 2.0,
    }


def test_regime_bucket_classifies_risk_off_high_volatility():
    row = {"regime_risk_state": "risk_off", "market_regime_json": {"label": "risk_off", "risk_state": "risk_off", "high_volatility": True}}
    assert regime_bucket(row) == "risk_off_high_volatility"


def test_setup_bucket_classifies_news_and_squeeze():
    news = {"checklist_json": [{"key": "news_catalysts", "available": True, "score": 1.2}]}
    squeeze = {"checklist_json": [{"key": "rvol", "available": True, "score": 1.4}]}
    assert setup_bucket(news) == "news_catalyst"
    assert setup_bucket(squeeze) == "squeeze_momentum"


def test_derive_regime_calibration_widens_high_vol_band():
    events = [_event(i, precision=58, target_error=20.0) for i in range(6)]
    cal = derive_regime_calibration(events, symbol="TEST", horizon=5, direction="UP", regime="risk_off_high_volatility", setup="squeeze_momentum")
    assert cal["available"] is True
    assert cal["regime_bucket"] == "risk_off_high_volatility"
    assert cal["setup_bucket"] == "squeeze_momentum"
    assert cal["target_move_multiplier"] > 1.0
    # High-vol + squeeze slice should not publish a fake-tight band.
    assert cal["range_width_pct"] > 0.04


def test_apply_regime_calibration_overrides_broad_range_from_raw_values():
    report = {
        "ok": True,
        "symbol": "TEST",
        "prediction": {"direction": "UP"},
        "risk_plan": {
            "entry": 10.0,
            "target_price_raw": 12.0,
            "stop_loss_raw": 9.0,
            "target_price": 12.2,
            "stop_loss": 9.0,
            "risk_reward_ratio": 2.2,
        },
    }
    profile = {
        "available": True,
        "sample_count": 7,
        "regime_bucket": "risk_on",
        "setup_bucket": "news_catalyst",
        "target_move_multiplier": 1.15,
        "stop_distance_multiplier": 0.90,
        "range_width_pct": 0.04,
        "calibration_status": "widen_target",
        "primary_reason": "News risk-on setups exceeded target.",
    }
    out = apply_regime_calibration_to_report(report, profile)
    assert out["regime_calibration"]["applied"] is True
    risk = out["risk_plan"]
    assert risk["target_price_raw"] == 12.0
    assert risk["stop_loss_raw"] == 9.0
    assert risk["target_price_regime_calibrated"] == 12.3
    assert risk["stop_loss_regime_calibrated"] == 9.1
    assert risk["regime_calibrated"] is True
    assert len(risk["expected_high_range"]) == 2


def test_apply_regime_calibration_cold_start_keeps_existing_plan():
    report = {"prediction": {"direction": "UP"}, "risk_plan": {"entry": 10.0, "target_price": 12.0, "stop_loss": 9.0}}
    out = apply_regime_calibration_to_report(report, {"available": False, "sample_count": 2, "regime_bucket": "risk_on", "setup_bucket": "general"})
    assert out["regime_calibration"]["applied"] is False
    assert out["risk_plan"]["target_price"] == 12.0


def test_super_ghost_report_includes_regime_calibration_cold_start():
    from core.super_ghost import build_super_ghost

    report = build_super_ghost("WOLF", snapshot={"symbol": "WOLF"})
    assert "regime_calibration" in report
    assert report["regime_calibration"]["applied"] is False


def test_super_ghost_applies_regime_calibration_when_profile_available(monkeypatch):
    from core.super_ghost import build_super_ghost
    from tests.test_super_ghost import _sample_snapshot

    def fake_profile(symbol, direction, report_or_regime=None, horizon=5):
        return {
            "available": True,
            "symbol": symbol,
            "direction": direction,
            "horizon_days": horizon,
            "sample_count": 6,
            "regime_bucket": "calm_risk_on",
            "setup_bucket": "news_catalyst",
            "target_move_multiplier": 1.10,
            "stop_distance_multiplier": 1.0,
            "range_width_pct": 0.035,
            "calibration_status": "widen_target",
            "primary_reason": "Calm risk-on news setups exceeded target.",
        }

    monkeypatch.setattr("core.super_ghost_regime_calibration.get_regime_calibration_profile", fake_profile)
    report = build_super_ghost("WOLF", snapshot=_sample_snapshot())
    assert report["regime_calibration"]["applied"] is True
    assert report["risk_plan"]["regime_calibrated"] is True
    assert report["risk_plan"]["target_price_regime_calibrated"] >= report["risk_plan"]["target_price_raw"]


def test_regime_calibration_endpoint(monkeypatch):
    def fake_summary(symbol=None, horizon=5, limit=20):
        return {"ok": True, "symbol": symbol or "ALL", "profiles": [{"regime_bucket": "risk_on", "setup_bucket": "news_catalyst"}]}

    monkeypatch.setattr("core.super_ghost_regime_calibration.regime_calibration_summary", fake_summary)
    r = TestClient(wolf_app.APP).get("/api/wolf/super-ghost/regime-calibration?symbol=WOLF&horizon=5")
    assert r.status_code == 200
    assert r.json()["profiles"][0]["setup_bucket"] == "news_catalyst"


def test_regime_calibration_rebuild_requires_auth():
    r = TestClient(wolf_app.APP).post("/api/wolf/super-ghost/regime-calibration/rebuild", json={"symbol": "WOLF"})
    assert r.status_code in (401, 403)
