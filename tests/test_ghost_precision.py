from fastapi.testclient import TestClient

import wolf_app
from core.ghost_precision import score_trade_precision
from core.super_ghost_precision import profile_from_precision_events, score_ledger_row


def test_trade_precision_separates_direction_win_from_low_precision():
    """A target hit can be a directional WIN while price precision is weak."""
    out = score_trade_precision(
        direction="UP",
        entry=11.41,
        target=11.87,
        stop=10.27,
        live_open=11.35,
        live_low=11.35,
        live_high=12.61,
        live_close=12.30,
    )
    assert out["target_stop_result"] == "WIN"
    assert out["direction_result"] == "WIN"
    assert out["precision_score"] < 75
    assert out["mistake_type"] in {"target_too_low", "stop_too_wide", "direction_right_low_precision"}
    assert out["errors_pct"]["high"] > 0


def test_trade_precision_neutral_when_target_and_stop_not_hit():
    out = score_trade_precision(
        direction="UP",
        entry=35.50,
        target=36.92,
        stop=33.25,
        live_open=36.05,
        live_low=35.51,
        live_high=36.51,
        live_close=36.10,
    )
    assert out["target_stop_result"] == "NEUTRAL"
    assert out["mistake_type"] in {"target_too_high", "no_follow_through", "stop_too_wide"}
    assert out["precision_score"] is not None


def test_super_ghost_precision_scores_resolved_ledger_row():
    row = {
        "id": 7,
        "symbol": "TEST",
        "created_at": 1,
        "direction": "UP",
        "reference_price": 4.50,
        "target_price": 5.00,
        "stop_loss": 4.20,
        "max_favorable_pct": 55.56,  # actual high ~= 7.00
        "max_adverse_pct": -2.0,
        "price_5d": 7.00,
        "return_5d_pct": 55.56,
        "correct_5d": True,
    }
    out = score_ledger_row(row, horizon=5)
    assert out["direction_result"] == "WIN"
    assert out["mistake_type"] == "target_too_low"
    assert abs(out["errors_pct"]["target"] - 40.0) < 0.01
    assert out["precision_score"] < 60


def test_precision_profile_requires_direction_and_precision():
    events = [
        {"direction_result": "WIN", "direction_correct": True, "precision_score": 82, "overall_score": 86, "mistake_type": "precise_direction_win", "target_error_pct": 1.0, "stop_error_pct": 1.5},
        {"direction_result": "WIN", "direction_correct": True, "precision_score": 78, "overall_score": 83, "mistake_type": "precise_direction_win", "target_error_pct": 2.0, "stop_error_pct": 1.0},
        {"direction_result": "WIN", "direction_correct": True, "precision_score": 76, "overall_score": 82, "mistake_type": "precise_direction_win", "target_error_pct": 2.5, "stop_error_pct": 1.0},
        {"direction_result": "WIN", "direction_correct": True, "precision_score": 74, "overall_score": 80, "mistake_type": "precise_direction_win", "target_error_pct": 3.0, "stop_error_pct": 2.0},
        {"direction_result": "LOSS", "direction_correct": False, "precision_score": 40, "overall_score": 30, "mistake_type": "wrong_direction", "target_error_pct": -8.0, "stop_error_pct": -3.0},
    ]
    prof = profile_from_precision_events(events)
    assert prof["available"] is True
    assert prof["sample_count"] == 5
    assert prof["direction_win_rate"] == 0.8
    assert prof["avg_precision_score"] == 70.0
    assert prof["precision_status"] in {"learning", "precision_supportive"}


def test_precision_summary_endpoint(monkeypatch):
    def fake_summary(symbol=None, horizon=5, limit=20):
        return {
            "ok": True,
            "symbol": symbol or "ALL",
            "horizon_days": horizon,
            "primary_profile": {"symbol": "WOLF", "sample_count": 5, "avg_precision_score": 71.2},
            "recent_events": [{"ledger_id": 1, "precision_score": 71.2}],
        }

    monkeypatch.setattr("core.super_ghost_precision.precision_summary", fake_summary)
    r = TestClient(wolf_app.APP).get("/api/wolf/super-ghost/precision?symbol=WOLF&horizon=5")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["primary_profile"]["avg_precision_score"] == 71.2


def test_precision_score_endpoint_requires_auth():
    r = TestClient(wolf_app.APP).post("/api/wolf/super-ghost/precision/score", json={"symbol": "WOLF"})
    assert r.status_code in (401, 403)
