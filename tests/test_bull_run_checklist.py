"""Tests for core/bull_run_checklist.py — pure, deterministic, no I/O."""
from core import bull_run_checklist as bc


def test_state_for_thresholds():
    assert bc._state_for(485.0, {"green": 470.0, "very_green": 480.0}) == "very_green"
    assert bc._state_for(475.0, {"green": 470.0, "very_green": 480.0}) == "green"
    assert bc._state_for(450.0, {"green": 470.0, "very_green": 480.0}) == "red"
    assert bc._state_for(None, {"green": 470.0}) == "unknown"


def test_missing_data_is_unknown_not_pass():
    """Missing evidence must never count toward the target."""
    out = bc.build_ymm_12_checklist({})
    assert out["confirmed"] == 0
    assert out["unknown"] == 12
    assert out["decision"] == "weak"


def test_full_green_is_strong():
    values = {
        "revenue_beat": 485.0,
        "eps_beat": 0.23,
        "transaction_growth": 42.0,
        "order_growth": 22.0,
        "shipper_growth": 16.0,
        "profitability": 1.0,
        "guidance": 3.0,
        "premarket_gap": 8.0,
        "relative_volume": 5.0,
        "breakout_950": 9.8,
        "breakout_1000": 10.2,
        "breakout_1100": 11.1,
    }
    out = bc.build_ymm_12_checklist(values)
    assert out["confirmed"] == 12
    assert out["decision"] == "strong"


def test_partial_is_moderate():
    values = {
        "revenue_beat": 475.0,
        "eps_beat": 0.21,
        "premarket_gap": 4.0,
        "relative_volume": 2.5,
        "breakout_950": 9.6,
        "breakout_1000": 10.1,
    }
    out = bc.build_ymm_12_checklist(values)
    assert out["confirmed"] == 6
    assert out["decision"] == "moderate"


def test_red_does_not_count():
    values = {
        "revenue_beat": 450.0,  # RED
        "eps_beat": 0.17,       # RED
    }
    out = bc.build_ymm_12_checklist(values)
    assert out["confirmed"] == 0
    # The red checks are still reported (not unknown).
    reds = [c for c in out["checks"] if c["state"] == "red"]
    assert len(reds) == 2


def test_decision_bands():
    assert bc._decision(9, 12)[0] == "strong"
    assert bc._decision(6, 12)[0] == "moderate"
    assert bc._decision(3, 12)[0] == "weak"


def test_ymm_preset_has_12_checks():
    out = bc.build_ymm_12_checklist({})
    assert out["total"] == 12
    assert out["target"] == 12.0
    assert out["symbol"] == "YMM"
    assert "disclaimer" in out
