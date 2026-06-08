"""Tests for binding-gate resolution (no_v3_model vs v3_prob_low)."""
from core.prediction import enrich_near_miss, resolve_binding_skip


def test_enrich_near_miss_bootstrap_gap():
    nm = enrich_near_miss({
        "symbol": "BB",
        "up_prob": 0.6983,
        "min_win_proba": 0.55,
        "confidence": 0.6983,
        "bootstrap_min_conf": 0.75,
        "skip": "objective_bootstrap_conf",
    })
    assert nm["prob_gap"] == 0.1483
    assert nm["bootstrap_gap"] == -0.0517


def test_binding_prefers_near_miss_over_bulk_no_model():
    """17 untrained symbols must not mask WOLF prob_low when model ran."""
    skip_counts = {"no_v3_model": 17, "v3_prob_low": 1}
    near_miss = {
        "symbol": "WOLF",
        "up_prob": 0.4118,
        "min_win_proba": 0.55,
        "skip": "v3_prob_low",
    }
    assert resolve_binding_skip(skip_counts, near_miss=near_miss) == "v3_prob_low"


def test_binding_priority_without_near_miss():
    skip_counts = {"no_v3_model": 10, "v3_prob_low": 2}
    assert resolve_binding_skip(skip_counts) == "v3_prob_low"


def test_binding_dedup_wins():
    skip_counts = {"v3_prob_low": 1, "no_v3_model": 5}
    assert resolve_binding_skip(skip_counts, dedup_blocked=1) == "dedup_blocked"
