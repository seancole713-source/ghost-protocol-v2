"""GHOST_ACCURACY_CONTRACT — unified 70%+ accuracy knobs."""



def test_contract_70_clamps_weak_env_overrides(monkeypatch):
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "70")
    monkeypatch.setenv("V3_MIN_HOLDOUT_ACC", "0.38")
    monkeypatch.setenv("V3_MIN_WF_ACC_MEAN", "0.40")
    monkeypatch.setenv("KILL_WINRATE_FLOOR", "0.40")
    from core.accuracy_contract import resolve_float

    assert resolve_float("V3_MIN_HOLDOUT_ACC", "min_holdout_acc") == 0.60
    assert resolve_float("V3_MIN_WF_ACC_MEAN", "min_wf_acc_mean") == 0.60
    assert resolve_float("KILL_WINRATE_FLOOR", "kill_winrate_floor") == 0.45  # P3 audit: kill = provably worse than coin-flip


def test_non_finite_env_cannot_lower_the_precision_target(monkeypatch):
    """GATE-01: V3_PRECISION_TARGET=nan used to resolve to the 0.50 lo bound."""
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "70")
    from core.accuracy_contract import resolve_float
    from core.precision_gate import precision_target

    for bad in ("nan", "NaN", "-nan", "inf", "-inf", "infinity"):
        monkeypatch.setenv("V3_PRECISION_TARGET", bad)
        assert resolve_float("V3_PRECISION_TARGET", "precision_target", lo=0.50, hi=0.95) == 0.70, bad
        assert precision_target() == 0.70, bad
        monkeypatch.setenv("KILL_WINRATE_FLOOR", bad)
        assert resolve_float("KILL_WINRATE_FLOOR", "kill_winrate_floor") == 0.45, bad


def test_non_finite_env_falls_back_to_default_on_legacy(monkeypatch):
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "legacy")
    monkeypatch.setenv("V3_MIN_HOLDOUT_ACC", "nan")
    from core.accuracy_contract import CONTRACTS, resolve_float

    assert resolve_float("V3_MIN_HOLDOUT_ACC", "min_holdout_acc", lo=0.0) == CONTRACTS["legacy"].min_holdout_acc


def test_legacy_contract_allows_weak_env(monkeypatch):
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "legacy")
    monkeypatch.setenv("V3_MIN_HOLDOUT_ACC", "0.38")
    from core.accuracy_contract import resolve_float

    assert resolve_float("V3_MIN_HOLDOUT_ACC", "min_holdout_acc") == 0.38


def test_research_bypass_enabled_under_70_contract(monkeypatch):
    """P3 audit: research bypass is now enabled under the 70 contract so
    models can fire research picks and accumulate precision-gate evidence."""
    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "70")
    from core.accuracy_contract import research_bypasses_precision_gate

    assert research_bypasses_precision_gate() is True


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


def test_objective_mode_contract_beats_auto_tuner(monkeypatch):
    """Audit U36: the auto-tuned runtime mode must not override contract 70."""
    import core.prediction as pred

    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "70")
    monkeypatch.setenv("OBJECTIVE_AUTO_MODE_ENABLED", "1")
    monkeypatch.setattr(pred, "_objective_runtime_mode", lambda *a, **k: "aggressive")
    cfg = pred._objective_effective_config()
    assert cfg["mode"] == "balanced"
    assert cfg["target_wr"] == 0.70
    assert cfg["min_samples"] == 12
    assert cfg["lookback_days"] == 150


def test_objective_mode_auto_tuner_still_applies_to_legacy(monkeypatch):
    import core.prediction as pred

    monkeypatch.setenv("GHOST_ACCURACY_CONTRACT", "legacy")
    monkeypatch.setenv("OBJECTIVE_AUTO_MODE_ENABLED", "1")
    monkeypatch.setattr(pred, "_objective_runtime_mode", lambda *a, **k: "precision")
    assert pred._objective_mode() == "precision"
