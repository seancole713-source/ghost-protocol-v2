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


def test_calibration_gap_only_reports_proven_bands():
    unproven = [{"score_pct": 20, "won": True}] * 2
    proven = [{"score_pct": 80, "won": i < 20} for i in range(20)]
    calibration = cal.build_calibration(unproven + proven)
    gap = cal.calibration_gap(calibration)
    bands_reported = {row["band"] for row in gap["rows"]}
    assert "20-30%" not in bands_reported
    assert "80-90%" in bands_reported
