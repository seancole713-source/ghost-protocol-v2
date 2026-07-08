"""PR #151: momentum/trend brain — Ghost's second way of thinking (ride runs)."""
import core.momentum as mom
from core.super_ghost_shadow import momentum_shadow, run_shadow_models, SHADOW_MODELS


def _report(sym="ODD"):
    return {"symbol": sym, "engine": "test",
            "prediction": {"direction": "HOLD", "confidence": 0.5},
            "risk_plan": {"entry": 10.0, "target_price": 10.2, "stop_loss": 9.85},
            "checklist": []}


def test_commits_up_on_strong_run(monkeypatch):
    monkeypatch.setattr(mom, "compute_momentum", lambda s, **k: {
        "available": True, "score": 6, "ret_20d_pct": 76.0, "adx": 42.0,
        "signals": {"breakout": True, "uptrend_struct": True, "above_sma20": True,
                    "trending": True, "strong_return": True, "volume_confirm": True}})
    out = momentum_shadow(_report())
    assert out["model_id"] == "momentum_shadow_v1"
    assert out["direction"] == "UP"
    assert 0.52 <= out["confidence"] <= 0.70    # capped, never overconfident


def test_holds_on_weak_momentum(monkeypatch):
    monkeypatch.setattr(mom, "compute_momentum", lambda s, **k: {
        "available": True, "score": 2, "ret_20d_pct": 1.0, "adx": 12.0,
        "signals": {"breakout": False, "uptrend_struct": True, "above_sma20": True,
                    "trending": False, "strong_return": False, "volume_confirm": False}})
    assert momentum_shadow(_report())["direction"] == "HOLD"


def test_holds_when_unavailable(monkeypatch):
    monkeypatch.setattr(mom, "compute_momentum", lambda s, **k: {"available": False, "reason": "thin"})
    out = momentum_shadow(_report())
    assert out["direction"] == "HOLD" and out["confidence"] == 0.50


def test_fails_safe_on_exception(monkeypatch):
    def boom(s, **k): raise RuntimeError("feed down")
    monkeypatch.setattr(mom, "compute_momentum", boom)
    assert momentum_shadow(_report())["direction"] == "HOLD"


def test_momentum_score_signals():
    # pure signal math: a steady uptrend with volume must score high
    rows = []
    price = 10.0
    for i in range(80):
        price *= 1.01  # steady climb
        rows.append({"close": round(price, 3), "high": round(price * 1.01, 3),
                     "low": round(price * 0.99, 3), "volume": 2_000_000})
    import core.signal_engine as se
    orig = se._fetch_ohlcv
    se._fetch_ohlcv = lambda sym, at, period=None, interval="1d": rows
    try:
        m = mom.compute_momentum("TESTUP")
    finally:
        se._fetch_ohlcv = orig
        mom._CACHE.clear()
    assert m["available"] and m["score"] >= 4  # confirmed run
    assert m["signals"]["breakout"] and m["signals"]["uptrend_struct"]


def test_registered_with_momentum_v2(monkeypatch):
    monkeypatch.setattr(mom, "compute_momentum", lambda s, **k: {"available": False, "reason": "t"})
    import core.seasonality as seas, core.news_events as ne
    monkeypatch.setattr(seas, "seasonal_window_stats", lambda s, **k: {"available": False, "reason": "t"})
    monkeypatch.setattr(ne, "news_available", lambda **k: False)
    assert any(mm.model_id == "momentum_shadow_v1" for mm in SHADOW_MODELS)
    assert any(mm.model_id == "momentum_shadow_v2" for mm in SHADOW_MODELS)
    preds = run_shadow_models(_report())
    assert len(preds) == 12
    assert any(p["model_id"] == "momentum_shadow_v2" for p in preds)
