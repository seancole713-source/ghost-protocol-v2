"""Tests for the transparent checklist that replaced the opaque up_prob score.

Every test here defends one of the three structural honesty rules:
unknown is never a pass, correlated boxes cannot stack, and vetoes only
ever subtract.
"""
from __future__ import annotations

from core import catalyst_checklist as cc


def test_no_evidence_scores_zero_not_full():
    """A stock Ghost knows nothing about must score 0%, never 100%.

    (Silently dropping unknown boxes from the denominator is exactly how a
    single known fact could otherwise read as full confidence.)
    """
    report = cc.evaluate_checklist("XYZ", cc.UP, {})
    assert report["score_pct"] == 0.0
    assert report["evidence_coverage_pct"] == 0.0
    assert report["direction_strength_pct"] is None
    assert report["boxes_unknown"] == report["boxes_total"]


def test_direction_flips_which_side_of_a_directional_box_passes():
    ev = dict(earnings_surprise_pct=6.0, guidance_direction=1, news_sentiment=0.6)
    up = cc.evaluate_checklist("X", cc.UP, ev)
    down = cc.evaluate_checklist("X", cc.DOWN, ev)
    assert up["score_pct"] > down["score_pct"]


def test_magnitude_boxes_read_the_same_both_directions():
    """Short interest fuels a squeeze whichever way the call points."""
    ev = dict(short_float_pct=31.0, days_to_cover=5.0)
    up = cc.evaluate_checklist("X", cc.UP, ev)
    down = cc.evaluate_checklist("X", cc.DOWN, ev)
    assert up["score_pct"] == down["score_pct"]


def test_correlated_boxes_in_one_group_cannot_dominate_the_score():
    """3 catalyst boxes maxed out is 1 of 5 groups -- capped at 20%."""
    ev = dict(earnings_surprise_pct=9, guidance_direction=1, news_sentiment=0.9)
    report = cc.evaluate_checklist("X", cc.UP, ev)
    assert report["score_pct"] <= 20.0 + 1e-9


def test_full_evidence_across_every_group_scores_full():
    ev = dict(
        earnings_surprise_pct=6.0, guidance_direction=1, news_sentiment=0.6,
        leadership_change_sentiment=0.3,
        revenue_growth_pct=12.0, margin_change_pct=1.4, net_income_growth_pct=30.0,
        short_float_pct=31.4, days_to_cover=4.1, borrow_fee_pct=18.0,
        relative_volume=6.2, premarket_gap_pct=5.5, trend_slope_pct=0.8,
        sector_move_pct=0.4, market_move_pct=0.2,
    )
    report = cc.evaluate_checklist("YMM", cc.UP, ev)
    assert report["score_pct"] == 100.0
    assert report["evidence_coverage_pct"] == 100.0
    assert report["direction_strength_pct"] == 100.0
    assert not report["blocked"]


def test_partial_coverage_separates_strength_from_coverage():
    """High conviction on what's known, but coverage stays honestly low.

    This is the split that would have caught the YMM DATA_UNAVAILABLE
    problem: a single bullish box must not read as a confident call.
    """
    report = cc.evaluate_checklist("X", cc.UP, dict(earnings_surprise_pct=9.0))
    assert report["direction_strength_pct"] == 100.0
    assert report["evidence_coverage_pct"] < 20.0
    assert report["score_pct"] < 20.0  # calibration keys off the discounted score


def test_veto_blocks_but_never_adds_confidence():
    ev = dict(short_float_pct=31.0, days_to_cover=5.0, move_from_base_pct=25.0)
    report = cc.evaluate_checklist("X", cc.UP, ev)
    assert report["blocked"] is True
    assert "already_ran" in report["blocked_by"]


def test_veto_does_not_trip_on_unknown_evidence():
    """Missing evidence must never be treated as a tripped veto."""
    report = cc.evaluate_checklist("X", cc.UP, {})
    assert report["blocked"] is False
    assert all(v["state"] == cc.UNKNOWN for v in report["vetoes"])


def test_boolean_box_kind_is_absent_not_false():
    """A NaN/garbage value must degrade to unknown, not read as failing evidence."""
    report = cc.evaluate_checklist("X", cc.UP, dict(earnings_surprise_pct=float("nan")))
    box = next(b for g in report["groups"] for b in g["boxes"] if b["key"] == "earnings_surprise")
    assert box["state"] == cc.UNKNOWN


def test_checklist_spec_matches_evaluated_groups():
    spec = cc.checklist_spec()
    report = cc.evaluate_checklist("X", cc.UP, {})
    assert {g["key"] for g in spec["groups"]} == {g["key"] for g in report["groups"]}
    assert spec["hold_bars"] == cc.HOLD_BARS == 3  # matches V3_LABEL_HOLD_BARS


def test_score_is_explicitly_not_labeled_a_probability():
    report = cc.evaluate_checklist("X", cc.UP, {})
    assert report["score_is_probability"] is False


def test_leadership_change_box_reads_from_edgar_signal_name():
    """Confirms the box exists and is wired to the collector's exact signal
    name -- a rename in either module without updating the other would leave
    this box permanently unknown without any test failing to say why."""
    report = cc.evaluate_checklist("X", cc.UP, dict(leadership_change_sentiment=0.4))
    box = next(b for g in report["groups"] for b in g["boxes"] if b["key"] == "leadership_change")
    assert box["state"] == cc.PASS

    report_bad = cc.evaluate_checklist("X", cc.UP, dict(leadership_change_sentiment=-0.4))
    box_bad = next(b for g in report_bad["groups"] for b in g["boxes"] if b["key"] == "leadership_change")
    assert box_bad["state"] == cc.FAIL
