"""Tests for turning checklist completeness into an honest win probability.

The one rule that matters: a young band prints no number at all, and a
proven band prints the Wilson floor, never the flattering raw rate.
"""
from __future__ import annotations

from core import checklist_calibration as cal


def test_band_for_buckets_by_ten_points():
    assert cal.band_for(86) == (80.0, 90.0)
    assert cal.band_for(0) == (0.0, 10.0)
    assert cal.band_for(100) == (90.0, 100.0)


def test_young_band_prints_no_confidence_number():
    samples = [{"score_pct": 86, "won": True}] * 3
    calibration = cal.build_calibration(samples)
    result = cal.confidence_for(86, calibration)
    assert result["confidence_pct"] is None
    assert result["proven"] is False
    assert "3" in result["explanation"]


def test_proven_band_prints_wilson_floor_not_raw_rate():
    samples = [{"score_pct": 86, "won": i < 24} for i in range(34)]  # 24/34 = 70.6% raw
    calibration = cal.build_calibration(samples)
    result = cal.confidence_for(86, calibration)
    assert result["proven"] is True
    raw_rate = 100.0 * 24 / 34
    assert result["confidence_pct"] < raw_rate
    assert result["confidence_pct"] > 0


def test_never_seen_band_explains_rather_than_guesses():
    calibration = cal.build_calibration([{"score_pct": 86, "won": True}] * 20)
    result = cal.confidence_for(15, calibration)
    assert result["confidence_pct"] is None
    assert "never resolved" in result["explanation"]


def test_samples_missing_score_or_outcome_are_skipped_not_guessed():
    samples = [
        {"score_pct": 50, "won": True},
        {"score_pct": None, "won": True},
        {"score_pct": 50, "won": None},
        {"won": True},
    ]
    calibration = cal.build_calibration(samples)
    assert calibration["skipped_samples"] == 3
    assert calibration["total_samples"] == 1


def test_malformed_truth_values_and_nonfinite_scores_are_rejected():
    samples = [
        {"score_pct": 50, "won": True},
        {"score_pct": 50, "won": 1},
        {"score_pct": 50, "won": "WIN"},
        {"score_pct": True, "won": True},
        {"score_pct": float("nan"), "won": True},
        {"score_pct": float("inf"), "won": False},
        {"score_pct": -0.1, "won": False},
        {"score_pct": 100.1, "won": True},
    ]
    calibration = cal.build_calibration(samples)
    assert calibration["total_samples"] == 1
    assert calibration["skipped_samples"] == 7


def test_boundaries_use_ten_point_bands_including_one_hundred():
    samples = [
        {"score_pct": 9.999, "won": True},
        {"score_pct": 10.0, "won": False},
        {"score_pct": 99.999, "won": True},
        {"score_pct": 100.0, "won": False},
    ]
    calibration = cal.build_calibration(samples)
    by_band = {row["band"]: row for row in calibration["bands"]}
    assert by_band["0-10%"]["n"] == 1
    assert by_band["10-20%"]["n"] == 1
    assert by_band["90-100%"]["n"] == 2


def test_calibration_preserves_exact_cohort_metadata():
    cohort = {
        "checklist_version": "v1",
        "hold_bars": 3,
        "outcome_contract": "contract-a",
        "direction": "DOWN",
        "symbol": "WOLF",
        "scope": "symbol",
    }
    calibration = cal.build_calibration([], cohort=cohort)
    assert calibration["cohort"] == cohort
    cohort["direction"] = "UP"
    assert calibration["cohort"]["direction"] == "DOWN"


def test_calibration_gap_only_reports_proven_bands():
    unproven = [{"score_pct": 20, "won": True}] * 2
    proven = [{"score_pct": 80, "won": i < 20} for i in range(20)]
    calibration = cal.build_calibration(unproven + proven)
    gap = cal.calibration_gap(calibration)
    bands_reported = {row["band"] for row in gap["rows"]}
    assert "20-30%" not in bands_reported
    assert "80-90%" in bands_reported
