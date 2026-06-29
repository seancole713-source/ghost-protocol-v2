from fastapi.testclient import TestClient

import wolf_app
from core.super_ghost_learning import (
    apply_learning_to_report,
    classify_lesson,
    profile_from_lessons,
)


def test_learning_classifies_target_too_low_when_realized_exceeds_target():
    """The user's concrete case: Ghost said target $5, price went to $7.

    Direction was right, but magnitude was wrong. The learning brain should not
    just mark this as a generic win; it should learn that the target model was
    too conservative.
    """
    row = {
        "id": 101,
        "symbol": "TEST",
        "direction": "UP",
        "reference_price": 4.50,
        "target_price": 5.00,
        "stop_loss": 4.20,
        "price_5d": 7.00,
        "return_5d_pct": 55.56,
        "correct_5d": True,
        "confidence": 0.72,
        "accuracy_grade": "B",
        "regime_label": "risk_on",
        "regime_risk_state": "risk_on",
    }
    lesson = classify_lesson(row, horizon=5)
    assert lesson.mistake_type == "target_too_low"
    assert lesson.target_error_pct == 40.0
    assert "too conservative" in lesson.lesson
    assert lesson.predicted_target == 5.0
    assert lesson.realized_price == 7.0


def test_learning_profile_widens_target_after_repeated_target_too_low_lessons():
    lessons = [
        classify_lesson({
            "id": i,
            "symbol": "TEST",
            "direction": "UP",
            "reference_price": 4.5,
            "target_price": 5.0,
            "stop_loss": 4.0,
            "price_5d": 7.0,
            "return_5d_pct": 55.56,
            "correct_5d": True,
        }, horizon=5)
        for i in range(1, 5)
    ]
    profile = profile_from_lessons(lessons)
    assert profile["available"] is True
    assert profile["sample_count"] == 4
    assert profile["primary_mistake_type"] == "target_too_low"
    assert profile["target_move_multiplier"] > 1.0
    assert "widen" in profile["primary_lesson"].lower()


def test_apply_learning_adjusts_target_move_and_confidence_bounded():
    report = {
        "ok": True,
        "symbol": "TEST",
        "prediction": {
            "direction": "UP",
            "confidence": 0.70,
            "conviction_score": 50.0,
            "accuracy_grade": "B",
            "action": "WATCHLIST UP BIAS",
        },
        "risk_plan": {
            "entry": 4.50,
            "target_price": 5.00,
            "stop_loss": 4.20,
            "risk_reward_ratio": 1.67,
        },
    }
    profile = {
        "available": True,
        "sample_count": 4,
        "direction_win_rate": 0.75,
        "target_move_multiplier": 1.30,
        "confidence_delta": 0.05,
        "conviction_multiplier": 1.10,
        "learning_status": "supportive",
        "primary_lesson": "Targets too conservative",
        "primary_mistake_type": "target_too_low",
    }
    out = apply_learning_to_report(report, profile)
    assert out["prediction"]["confidence"] == 0.75
    assert out["prediction"]["conviction_score"] == 55.0
    assert out["risk_plan"]["target_price_original"] == 5.0
    # Move from 4.5->5.0 is 0.5; widened by 1.3 => 4.5 + 0.65 = 5.15
    assert out["risk_plan"]["target_price"] == 5.15
    assert out["learning_adjustment"]["available"] is True
    assert out["learning_adjustment"]["new_target_price"] == 5.15


def test_apply_learning_dampens_bad_direction_profile():
    report = {
        "ok": True,
        "symbol": "TEST",
        "prediction": {
            "direction": "UP",
            "confidence": 0.72,
            "conviction_score": 60.0,
            "accuracy_grade": "B+",
            "action": "HIGH-CONVICTION UP PREDICTION",
        },
        "risk_plan": {"entry": 10.0, "target_price": 12.0, "stop_loss": 9.0},
    }
    profile = {
        "available": True,
        "sample_count": 6,
        "direction_win_rate": 0.25,
        "target_move_multiplier": 1.0,
        "confidence_delta": -0.08,
        "conviction_multiplier": 0.85,
        "learning_status": "dampen",
        "primary_lesson": "Direction has been wrong too often",
        "primary_mistake_type": "wrong_direction",
    }
    out = apply_learning_to_report(report, profile)
    assert out["prediction"]["confidence"] == 0.64
    assert out["prediction"]["conviction_score"] == 51.0
    assert out["prediction"]["action"] == "NO EDGE — LEARNING BLOCK"
    assert out["prediction"]["accuracy_grade"] == "C"


def test_super_ghost_report_contains_learning_adjustment_cold_start():
    from core.super_ghost import build_super_ghost

    report = build_super_ghost("WOLF", snapshot={"symbol": "WOLF"})
    assert "learning_adjustment" in report
    assert report["learning_adjustment"]["available"] is False


def test_learning_summary_endpoint(monkeypatch):
    def fake_summary(symbol=None, horizon=5, limit=20):
        return {
            "ok": True,
            "symbol": symbol or "ALL",
            "horizon_days": horizon,
            "profiles": [{"symbol": "WOLF", "sample_count": 3, "primary_lesson": "Targets too low"}],
            "recent_lessons": [],
        }

    monkeypatch.setattr("core.super_ghost_learning.learning_summary", fake_summary)
    client = TestClient(wolf_app.APP)
    r = client.get("/api/wolf/super-ghost/learning?symbol=WOLF&horizon=5")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["symbol"] == "WOLF"
    assert d["profiles"][0]["primary_lesson"] == "Targets too low"


def test_learning_post_endpoint_requires_auth():
    client = TestClient(wolf_app.APP)
    r = client.post("/api/wolf/super-ghost/learn", json={"symbol": "WOLF"})
    assert r.status_code in (401, 403)
