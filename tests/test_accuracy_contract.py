"""GHOST_ACCURACY_CONTRACT — unified 70%+ accuracy knobs."""
import os

import pytest


def test_contract_70_clamps_weak_env_overrides(monkeypatch):
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "70")
    monkeypatch.setenv("V3_MIN_HOLDOUT_ACC", "0.38")
    monkeypatch.setenv("V3_MIN_WF_ACC_MEAN", "0.40")
    monkeypatch.setenv("KILL_WINRATE_FLOOR", "0.40")
    from core.accuracy_contract import resolve_float

    assert resolve_float("V3_MIN_HOLDOUT_ACC", "min_holdout_acc") == 0.65
    assert resolve_float("V3_MIN_WF_ACC_MEAN", "min_wf_acc_mean") == 0.65
    assert resolve_float("KILL_WINRATE_FLOOR", "kill_winrate_floor") == 0.70


def test_legacy_contract_allows_weak_env(monkeypatch):
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "legacy")
    monkeypatch.setenv("V3_MIN_HOLDOUT_ACC", "0.38")
    from core.accuracy_contract import resolve_float

    assert resolve_float("V3_MIN_HOLDOUT_ACC", "min_holdout_acc") == 0.38


def test_research_bypass_disabled_under_70_contract(monkeypatch):
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "70")
    from core.accuracy_contract import research_bypasses_precision_gate

    assert research_bypasses_precision_gate() is False


def test_research_bypass_enabled_under_legacy(monkeypatch):
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "legacy")
    from core.accuracy_contract import research_bypasses_precision_gate

    assert research_bypasses_precision_gate() is True


def test_objective_mode_follows_contract(monkeypatch):
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "70")
    monkeypatch.setenv("OBJECTIVE_MODE", "aggressive")
    monkeypatch.setenv("OBJECTIVE_AUTO_MODE_ENABLED", "0")
    from core.prediction import _objective_effective_config

    cfg = _objective_effective_config()
    assert cfg["mode"] == "balanced"
    assert cfg["target_wr"] == 0.70
