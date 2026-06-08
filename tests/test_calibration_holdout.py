"""Phase 2 — holdout calibration metrics (Brier + reliability bins)."""
import numpy as np
import pytest

from core.signal_engine import (
    _evaluate_calibration_holdout,
    _maybe_calibrate,
    _reliability_bins,
)


def _fit_tiny_model():
    pytest.importorskip("sklearn")
    from sklearn.linear_model import LogisticRegression

    rng = np.random.RandomState(0)
    X = rng.rand(40, 3)
    y = (X[:, 0] + rng.rand(40) * 0.3 > 0.6).astype(int)
    y[0], y[1] = 0, 1
    return LogisticRegression().fit(X, y), X, y


def test_reliability_bins_cover_samples():
    y = np.array([1, 1, 0, 0, 1, 0, 1, 0])
    p = np.array([0.9, 0.8, 0.2, 0.1, 0.7, 0.3, 0.6, 0.4])
    bins = _reliability_bins(y, p, n_bins=2)
    assert bins
    assert sum(b["n"] for b in bins) == len(y)


def test_evaluate_calibration_holdout_computes_brier(monkeypatch):
    monkeypatch.setenv("V3_CALIBRATION", "on")
    model, X, y = _fit_tiny_model()
    Xc, yc = X[:20], y[:20]
    Xg, yg = X[20:], y[20:]
    calibrated, info = _maybe_calibrate(model, Xc, yc)
    assert info.get("calibrated") is True

    out = _evaluate_calibration_holdout(calibrated, Xg, yg)
    assert out["gate_n"] == len(yg)
    assert out["gate_brier"] is not None
    assert out["gate_brier"] < 0.24
    assert out["reliability_bins"]
