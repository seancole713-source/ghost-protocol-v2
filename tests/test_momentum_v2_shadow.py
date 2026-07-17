"""PR #153: momentum_shadow_v2 is a new, shadow-only trend-following brain."""
import core.momentum as mom
from core.super_ghost_shadow import momentum_shadow_v2, run_shadow_models, SHADOW_MODELS


def _report(sym="ODD"):
    return {"symbol": sym, "engine": "test",
            "prediction": {"direction": "HOLD", "confidence": 0.5},
            "risk_plan": {"entry": 10.0, "target_price": 10.2, "stop_loss": 9.85},
            "checklist": []}


def _v2(score=7, raw=7, setup="trend_continuation", penalties=None):
    return {
        "available": True, "version": "v2", "score": score, "raw_score": raw,
        "penalty_score": len([p for p in (penalties or {}).values() if p]),
        "setup": setup,
        "signals": {"multi_tf_uptrend": True, "above_sma20": True,
                    "breakout_or_near_high": True, "higher_low_pullback": True,
                    "relative_strength": True, "trend_strength": True,
                    "strong_20d_return": True, "volume_confirm": False},
        "penalties": penalties or {"overextended": False, "trendless_chop": False},
        "ret_20d_pct": 18.0, "relative_strength_20d": 0.06, "adx": 27.0,
    }


def test_momentum_v2_commits_only_on_clean_continuation(monkeypatch):
    monkeypatch.setattr(mom, "compute_momentum_v2", lambda s, **k: _v2(score=7))
    out = momentum_shadow_v2(_report())
    assert out["model_id"] == "momentum_shadow_v2"
    assert out["direction"] == "UP"
    assert 0.54 <= out["confidence"] <= 0.69
    assert "Shadow-only" in out["reason"]


def test_momentum_v2_holds_on_overextended_run(monkeypatch):
    monkeypatch.setattr(mom, "compute_momentum_v2", lambda s, **k: _v2(
        score=5, raw=7, setup="extended_wait_for_pullback", penalties={"overextended": True}))
    out = momentum_shadow_v2(_report())
    assert out["direction"] == "HOLD"
    assert "watchlist only" in out["reason"] or "No v2" in out["reason"]


def test_momentum_v2_registered_without_mutating_v1(monkeypatch):
    monkeypatch.setattr(mom, "compute_momentum_v2", lambda s, **k: {"available": False, "version": "v2", "reason": "test"})
    import core.seasonality as seas
    import core.news_events as ne
    monkeypatch.setattr(seas, "seasonal_window_stats", lambda s, **k: {"available": False, "reason": "t"})
    monkeypatch.setattr(ne, "news_available", lambda **k: False)
    ids = [m.model_id for m in SHADOW_MODELS]
    assert "momentum_shadow_v1" in ids
    assert "momentum_shadow_v2" in ids
    preds = run_shadow_models(_report())
    assert len(preds) == 12
    assert any(p["model_id"] == "momentum_shadow_v2" for p in preds)
