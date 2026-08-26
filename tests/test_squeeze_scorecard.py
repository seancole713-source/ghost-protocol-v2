"""Squeeze scorecard — setup/trigger/confirm + heuristic probability targets."""

from core.squeeze_scorecard import (
    build_scorecard_row,
    compute_stop,
    probability_targets,
    score_confirmation,
    score_setup,
    score_trigger,
    scorecard_legend,
    squeeze_score,
)


def test_score_setup_high_short():
    s = score_setup({"short_float_pct": 25.0, "days_to_cover": 4.0, "squeeze_risk": "extreme"})
    assert s >= 70


def test_score_trigger_move():
    assert score_trigger(8.0, 3.0) >= 50
    assert score_trigger(1.0, 0.5) < 20


def test_score_confirmation_rvol_and_vwap():
    assert score_confirmation(3.0, True) > score_confirmation(3.0, False)
    assert score_confirmation(0.5, None) < score_confirmation(2.5, True)


def test_squeeze_score_weighted():
    total = squeeze_score(80, 60, 70)
    assert 68 <= total <= 76


def test_probability_targets_bounded():
    probs = probability_targets(
        squeeze_score_val=75,
        rvol=3.0,
        peak_move_pct=10.0,
        above_vwap=True,
        kind="squeeze_active",
    )
    for key in (
        "p_continue_3pct_60m",
        "p_vwap_hold",
        "p_close_above_prior_high",
        "p_exhaustion_soon",
    ):
        assert 5 <= probs[key] <= 92


def test_compute_stop_uses_vwap():
    stop = compute_stop(10.0, vwap=9.5, prior_close=9.0)
    assert stop <= 9.5


def test_build_scorecard_row_fields():
    metrics = {
        "price": 4.52,
        "prior_close": 4.20,
        "session_high": 4.92,
        "peak_move_pct": 17.1,
        "current_move_pct": 7.6,
        "vwap": 4.45,
    }
    row = build_scorecard_row(
        "SPCE",
        metrics,
        3.2,
        {"short_float_pct": 18.0, "days_to_cover": 3.5, "squeeze_risk": "high"},
        kind="squeeze_active",
    )
    assert row["symbol"] == "SPCE"
    assert row["buy"] == 4.52
    assert row["sell"] >= 4.52
    assert row["stop"] < row["buy"]
    assert row["squeeze_score"] > 0
    assert row["probabilities"]["p_continue_3pct_60m"] > 0
    assert row["probability_model"] in ("squeeze_ml_v2", "heuristic_v1")
    assert row["probability_status"] in ("unvalidated_proxy", "research_only")
    assert row["probabilities_decision_eligible"] is False
    assert row["short_interest_status"] == "available"
    assert row["short_interest_fields_observed"] == [
        "short_float_pct", "days_to_cover",
    ]
    assert row["above_vwap"] is True


def test_missing_short_interest_is_unknown_and_scores_zero():
    metrics = {
        "price": 10.0,
        "prior_close": 9.5,
        "session_high": 10.5,
        "peak_move_pct": 5.0,
        "current_move_pct": 4.0,
        "vwap": 9.9,
    }

    row = build_scorecard_row("MISS", metrics, 2.0, {})

    assert row["setup_score"] == 0
    assert row["short_interest_status"] == "unknown"
    assert row["short_interest_fields_observed"] == []
    assert row["short_float_pct"] is None
    assert row["days_to_cover"] is None


def test_observed_zero_short_interest_is_available_not_missing():
    metrics = {
        "price": 10.0,
        "prior_close": 9.5,
        "session_high": 10.5,
        "peak_move_pct": 5.0,
        "current_move_pct": 4.0,
        "vwap": 9.9,
    }

    row = build_scorecard_row(
        "ZERO", metrics, 2.0,
        {"short_float_pct": 0.0, "days_to_cover": 0.0},
    )

    assert row["setup_score"] == 0
    assert row["short_interest_status"] == "available"
    assert row["short_interest_fields_observed"] == [
        "short_float_pct", "days_to_cover",
    ]


def test_partial_short_interest_lists_only_observed_fields():
    metrics = {
        "price": 10.0,
        "prior_close": 9.5,
        "session_high": 10.5,
        "peak_move_pct": 5.0,
        "current_move_pct": 4.0,
        "vwap": None,
    }

    row = build_scorecard_row(
        "PART", metrics, 2.0,
        {"short_float_pct": None, "days_to_cover": 2.5},
    )

    assert row["short_interest_status"] == "available"
    assert row["short_interest_fields_observed"] == ["days_to_cover"]
    assert row["short_float_pct"] is None


def test_scorecard_legend_has_ct_session():
    leg = scorecard_legend()
    assert leg["timezone"] == "America/Chicago"
    assert "cash" in leg["session"]
    assert leg["probability_status"] in ("unvalidated_proxy", "research_only")
    assert leg["probabilities_decision_eligible"] is False
    assert "not calibrated" in leg["probabilities_note"]
    assert "alert confidence" in leg["probabilities_note"]
