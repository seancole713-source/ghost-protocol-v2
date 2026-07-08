"""PR #133: calendar-seasonality stats + the seasonal shadow brain.

The brain must commit only on consistent multi-year edges, cap confidence at
0.65 (n<=4 windows is thin evidence), and fail safe to HOLD on any data
shortfall.
"""
import datetime

import core.seasonality as seas
from core.seasonality import seasonal_window_stats, _compute
from core.super_ghost_shadow import seasonal_shadow, run_shadow_models, SHADOW_MODELS


def _mk_rows(daily_pct_by_date):
    """Build OHLCV-ish rows from {date: daily_return_pct}, starting at 100."""
    rows, price = [], 100.0
    for d in sorted(daily_pct_by_date):
        price *= 1 + daily_pct_by_date[d] / 100.0
        rows.append({"date": d.isoformat(), "close": round(price, 4)})
    return rows


def _flat_years_with_july_pop(pop_pct=2.0):
    """~5y of flat closes except +pop_pct days inside the July 8-11 window.

    The window entry is the first trading day AFTER the anchor (July 5), so the
    pop must land strictly after July 7 to be captured in the forward return
    for every weekday layout.
    """
    out = {}
    d = datetime.date(2021, 6, 1)
    while d <= datetime.date(2026, 7, 2):
        if d.weekday() < 5:
            pop = d.month == 7 and 8 <= d.day <= 11
            out[d] = pop_pct if pop else 0.0
        d += datetime.timedelta(days=1)
    return out


def test_compute_finds_consistent_up_lean(monkeypatch):
    rows = _mk_rows(_flat_years_with_july_pop(3.0))
    monkeypatch.setattr(seas, "_fetch_daily_5y", lambda s: rows)
    stats = _compute("TEST", datetime.date(2026, 7, 5), 5)
    assert stats["available"] is True
    assert stats["n_years"] >= 3
    assert stats["lean"] == "UP"
    assert stats["consistency"] == 1.0
    assert stats["excess_pct"] >= 2.5


def test_compute_no_lean_on_flat_history(monkeypatch):
    rows = _mk_rows({d: 0.0 for d in _flat_years_with_july_pop(0.0)})
    monkeypatch.setattr(seas, "_fetch_daily_5y", lambda s: rows)
    stats = _compute("TEST", datetime.date(2026, 7, 5), 5)
    assert stats["available"] is True
    assert stats["lean"] == "NONE"


def test_compute_unavailable_on_thin_history(monkeypatch):
    rows = _mk_rows({datetime.date(2026, 1, 1) + datetime.timedelta(days=i): 0.0
                     for i in range(100)})
    monkeypatch.setattr(seas, "_fetch_daily_5y", lambda s: rows)
    stats = _compute("TEST", datetime.date(2026, 7, 5), 5)
    assert stats["available"] is False


def _report():
    return {"symbol": "TEST", "engine": "test",
            "prediction": {"direction": "UP", "confidence": 0.8},
            "risk_plan": {"entry": 100.0, "target_price": 102.0, "stop_loss": 98.5},
            "checklist": []}


def test_brain_commits_capped_on_strong_edge(monkeypatch):
    monkeypatch.setattr(seas, "seasonal_window_stats", lambda s, **k: {
        "available": True, "n_years": 4, "lean": "UP", "consistency": 1.0,
        "excess_pct": 11.7, "avg_window_pct": 11.7, "baseline_pct": 0.0})
    out = seasonal_shadow(_report())
    assert out["model_id"] == "seasonal_shadow_v1"
    assert out["direction"] == "UP"
    assert 0.52 <= out["confidence"] <= 0.65


def test_brain_holds_on_inconsistent_edge(monkeypatch):
    monkeypatch.setattr(seas, "seasonal_window_stats", lambda s, **k: {
        "available": True, "n_years": 4, "lean": "UP", "consistency": 0.5,
        "excess_pct": 8.0, "avg_window_pct": 8.0, "baseline_pct": 0.0})
    out = seasonal_shadow(_report())
    assert out["direction"] == "HOLD"


def test_brain_fails_safe_when_data_layer_raises(monkeypatch):
    def boom(s, **k):
        raise RuntimeError("feed down")
    monkeypatch.setattr(seas, "seasonal_window_stats", boom)
    out = seasonal_shadow(_report())
    assert out["direction"] == "HOLD"
    assert out["confidence"] == 0.50


def test_registered_as_ninth_brain(monkeypatch):
    monkeypatch.setattr(seas, "seasonal_window_stats",
                        lambda s, **k: {"available": False, "reason": "test"})
    import core.news_events as ne
    monkeypatch.setattr(ne, "news_available", lambda **k: False)
    assert any(m.model_id == "seasonal_shadow_v1" for m in SHADOW_MODELS)
    preds = run_shadow_models(_report())
    assert len(preds) == 11  # + momentum (PR #151)
    assert any(p["model_id"] == "seasonal_shadow_v1" for p in preds)


def test_cache_returns_same_object(monkeypatch):
    rows = _mk_rows(_flat_years_with_july_pop(3.0))
    calls = {"n": 0}
    def fetch(s):
        calls["n"] += 1
        return rows
    monkeypatch.setattr(seas, "_fetch_daily_5y", fetch)
    seas._CACHE.clear()
    a = seasonal_window_stats("CACHETEST", datetime.date(2026, 7, 5))
    b = seasonal_window_stats("CACHETEST", datetime.date(2026, 7, 5))
    assert calls["n"] == 1 and a == b
