"""PR #132: contrarian (inverse-Ghost) shadow brain.

Inverts every committed production call; holds when production holds; mirrors
the risk geometry around entry so the inverted trade is coherent.
"""
from core.super_ghost_shadow import (
    SHADOW_MODELS,
    contrarian_shadow,
    run_shadow_models,
    shadow_manifest,
    _correct,
)


def _report(direction="UP", confidence=0.8, entry=100.0, target=102.0, stop=98.5):
    return {
        "symbol": "WOLF",
        "engine": "test",
        "prediction": {"direction": direction, "confidence": confidence},
        "risk_plan": {"entry": entry, "target_price": target, "stop_loss": stop},
        "checklist": [],
    }


def test_contrarian_inverts_up_to_down():
    out = contrarian_shadow(_report("UP"))
    assert out["model_id"] == "contrarian_shadow_v1"
    assert out["direction"] == "DOWN"


def test_contrarian_inverts_down_to_up():
    assert contrarian_shadow(_report("DOWN"))["direction"] == "UP"


def test_contrarian_holds_when_production_holds():
    out = contrarian_shadow(_report("HOLD"))
    assert out["direction"] == "HOLD"
    assert out["confidence"] == 0.50


def test_contrarian_mirrors_risk_geometry():
    out = contrarian_shadow(_report("UP", entry=100.0, target=102.0, stop=98.5))
    # Long targeted +2 with -1.5 stop → inverted short targets -2 with +1.5 stop.
    assert out["target_price"] == 98.0
    assert out["stop_loss"] == 101.5


def test_contrarian_registered_and_runs():
    assert any(m.model_id == "contrarian_shadow_v1" for m in SHADOW_MODELS)
    assert any(m["model_id"] == "contrarian_shadow_v1" for m in shadow_manifest())
    preds = run_shadow_models(_report("UP"))
    byid = {p["model_id"]: p for p in preds}
    assert byid["contrarian_shadow_v1"]["direction"] == "DOWN"


def test_contrarian_scoring_is_complement_of_production():
    # 5-day sign resolution: when production UP is wrong (ret<0), contrarian
    # DOWN is right — the profile directly measures the anti-signal hypothesis.
    ret = -1.7
    assert _correct("UP", ret) is False
    assert _correct("DOWN", ret) is True
