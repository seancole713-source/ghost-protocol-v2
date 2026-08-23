"""tests/test_dev_env_pinning.py — Tier 3 hygiene: ML dependency pin guard.

The dev environment must match the pinned production requirements. A silent
sklearn/numpy/xgboost bump already bit once (sklearn >=1.6 removed cv="prefit",
silently uncalibrating the engine — forensic SE-3/ST-5). The conftest guard
fails collection on drift instead of producing subtly wrong results.
"""
from __future__ import annotations

import importlib.metadata as _md

import pytest


def test_ml_versions_match_requirements_pins():
    """The interpreter's installed ML versions equal the pinned requirements."""
    pinned = {
        "scikit-learn": "1.5.2",
        "numpy": "1.26.4",
        "xgboost": "2.1.1",
    }
    for dist, want in pinned.items():
        got = _md.version(dist)
        assert got == want, (
            f"{dist} is {got!r}, expected {want!r} — reinstall from "
            "requirements.txt or update the pin deliberately"
        )


def test_conftest_guard_raises_on_drift(monkeypatch):
    """The conftest guard raises RuntimeError when a version drifts."""
    import tests.conftest as _c

    def _fake_version(dist):
        if dist == "scikit-learn":
            return "9.9.9"
        return _md.version(dist)

    monkeypatch.setattr(_md, "version", _fake_version)
    with pytest.raises(RuntimeError, match="scikit-learn"):
        _c._assert_pinned_ml_versions()
