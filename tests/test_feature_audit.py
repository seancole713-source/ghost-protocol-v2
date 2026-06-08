"""Tests for Phase 3 feature audit + regime peer weighting."""
import numpy as np
import pytest

from core.feature_audit import (
    apply_inversions_to_features,
    apply_inversions_to_matrix,
    audit_gate_features,
    peer_regime_weight,
    point_biserial_corr,
    regime_profile,
    reliability_bins_monotonic,
    select_inverted_features,
)


def test_point_biserial_positive_when_feature_aligns_with_wins():
    x = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8])
    y = np.array([0, 0, 0, 0, 1, 1, 1, 1])
    assert point_biserial_corr(x, y) > 0.85


def test_audit_flags_inverted_momentum_on_gate_slice():
    rng = np.random.RandomState(0)
    n = 40
    cols = ["pct_b", "mom_4h", "rsi"]
    # mom_4h inversely related to wins; pct_b weakly positive
    mom = rng.uniform(-0.05, 0.05, n)
    pct = rng.uniform(0.2, 0.8, n)
    rsi = rng.uniform(30, 70, n)
    y = (mom < 0).astype(int)
    X = np.column_stack([pct, mom, rsi])
    audit = audit_gate_features(X, y, cols, min_n=10)
    by_name = {row["feature"]: row for row in audit}
    assert by_name["mom_4h"]["invert"] is True
    assert by_name["mom_4h"]["gate_corr"] < 0
    assert "mom_4h" in select_inverted_features(audit)


def test_apply_inversions_negates_selected_columns():
    feats = {"mom_4h": 0.12, "pct_b": 0.7, "rsi": 55.0}
    apply_inversions_to_features(feats, {"mom_4h"})
    assert feats["mom_4h"] == -0.12
    assert feats["pct_b"] == 0.7

    X = np.array([[0.1, 0.2], [0.3, 0.4]])
    out = apply_inversions_to_matrix(X, ["a", "b"], {"b"})
    assert out[0, 1] == -0.2
    assert out[0, 0] == 0.1


def test_regime_profile_and_peer_weight_downrank_divergent_peer():
    target_rows = [
        {"features": {"atr_pct": 0.02, "mom_4h": 0.01, "pct_b": 0.5}},
        {"features": {"atr_pct": 0.022, "mom_4h": 0.012, "pct_b": 0.52}},
    ]
    profile = regime_profile(target_rows)
    close_peer = peer_regime_weight(profile, {"atr_pct": 0.021, "mom_4h": 0.011, "pct_b": 0.51})
    far_peer = peer_regime_weight(profile, {"atr_pct": 0.08, "mom_4h": -0.05, "pct_b": 0.95})
    assert close_peer > far_peer


def test_reliability_bins_monotonic_detects_inversion():
    good = [
        {"n": 5, "mean_pred": 0.55, "observed_rate": 0.4, "bin_lo": 0.5, "bin_hi": 0.6},
        {"n": 5, "mean_pred": 0.75, "observed_rate": 0.6, "bin_lo": 0.7, "bin_hi": 0.8},
        {"n": 5, "mean_pred": 0.92, "observed_rate": 0.8, "bin_lo": 0.9, "bin_hi": 1.0},
    ]
    bad = [
        {"n": 5, "mean_pred": 0.55, "observed_rate": 0.5, "bin_lo": 0.5, "bin_hi": 0.6},
        {"n": 5, "mean_pred": 0.75, "observed_rate": 0.0, "bin_lo": 0.7, "bin_hi": 0.8},
        {"n": 5, "mean_pred": 0.92, "observed_rate": 0.5, "bin_lo": 0.9, "bin_hi": 1.0},
    ]
    assert reliability_bins_monotonic(good)["monotonic"] is True
    assert reliability_bins_monotonic(bad)["monotonic"] is False
