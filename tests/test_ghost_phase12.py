"""Phase 1+2 modules — contract, calibration, ML v2, regime, sentiment, drift, options."""

import os
from unittest.mock import MagicMock, patch

import pytest

from core.ghost_contract import CONTRACT_VERSION, ghost_contract
from core.news_sentiment import score_articles, score_headline
from core.regime_calibration import (
    effective_min_win_proba,
    regime_calibration_enabled,
    regime_calibration_meta,
    sma5_gate_trend_up_bypass,
)
from core.regime_classifier import classify_from_indicators, unified_regime
from core.squeeze_ml_v2 import blend_probabilities, model_info, predict_continue_3pct


def test_ghost_contract_post_falsification():
    c = ghost_contract()
    assert c["version"] == CONTRACT_VERSION
    assert c["north_star_retired"] is True
    assert c["falsification"]["status"] == "abandoned"
    assert c["lanes"]["v3_picks"]["accuracy_claim"] is None


def test_regime_calibration_lowers_floor_in_uptrend(monkeypatch):
    monkeypatch.setenv("GHOST_REGIME_CALIBRATION", "1")
    base = 0.55
    eff = effective_min_win_proba("Trend-up", base=base)
    assert eff < base
    meta = regime_calibration_meta("Trend-up", base=base)
    assert meta["effective_min_win_proba"] == eff
    assert meta["adjustment"] < 0


def test_regime_calibration_disabled_returns_base(monkeypatch):
    monkeypatch.setenv("GHOST_REGIME_CALIBRATION", "0")
    assert effective_min_win_proba("Trend-up", base=0.55) == 0.55


def test_sma5_bypass_env(monkeypatch):
    monkeypatch.setenv("REGIME_GATE_SMA5_TREND_UP_BYPASS", "1")
    assert sma5_gate_trend_up_bypass() is True
    monkeypatch.setenv("REGIME_GATE_SMA5_TREND_UP_BYPASS", "0")
    assert sma5_gate_trend_up_bypass() is False


def test_squeeze_ml_v2_blend(monkeypatch):
    monkeypatch.setenv("SQUEEZE_ML_V2", "1")
    heu = {
        "p_continue_3pct_60m": 40.0,
        "p_vwap_hold": 50.0,
        "p_close_above_prior_high": 45.0,
        "p_exhaustion_soon": 30.0,
    }
    out = blend_probabilities(
        heu,
        setup_score=75,
        trigger_score=60,
        confirm_score=70,
        rvol=3.0,
        peak_move_pct=10.0,
        above_vwap=True,
        short_risk="high",
    )
    assert out["p_continue_3pct_60m_ml"] > 0
    assert out["p_continue_3pct_60m_heuristic"] == 40.0
    assert model_info()["model"] == "squeeze_ml_v2"


def test_squeeze_ml_v2_predict_bounded(monkeypatch):
    monkeypatch.setenv("SQUEEZE_ML_V2", "1")
    p = predict_continue_3pct(
        setup_score=80,
        trigger_score=70,
        confirm_score=75,
        rvol=3.5,
        peak_move_pct=12.0,
        above_vwap=True,
        short_risk="extreme",
    )
    assert 0 < p <= 100


def test_regime_classifier_engine_labels():
    assert classify_from_indicators(
        above_ema200=1, adx_trending=1, ema_trend_bullish=1
    ) == "Trend-up"
    assert classify_from_indicators(
        above_ema200=0, adx_trending=1, ema_trend_bullish=0
    ) == "Trend-down"
    u = unified_regime(price=10.0, sma_5d=9.5, volume_ratio=1.2)
    assert u["primary_label"]


def test_news_sentiment_lexicon():
    assert score_headline("WOLF beats earnings, strong rally upgrade") > 0
    assert score_headline("WOLF misses, downgrade, weak decline") < 0
    out = score_articles(
        [{"title": "WOLF surges on contract win"}, {"title": "WOLF slips on delay"}],
        symbol="WOLF",
    )
    assert out["count"] == 2
    assert out["model"] == "lexicon_v1"


def test_feature_drift_disabled(monkeypatch):
    monkeypatch.setenv("GHOST_FEATURE_DRIFT", "0")
    from core.feature_drift import compute_drift

    out = compute_drift("WOLF")
    assert out["enabled"] is False


def test_feature_drift_with_mock_db(monkeypatch):
    monkeypatch.setenv("GHOST_FEATURE_DRIFT", "1")
    from core.feature_drift import compute_drift

    payloads = [{"rsi": 50 + (i % 3)} for i in range(40)]
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = [(p,) for p in payloads]
    conn.cursor.return_value = cur
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=conn)
    cm.__exit__ = MagicMock(return_value=False)
    with patch("core.db.db_conn", return_value=cm):
        out = compute_drift("WOLF", window=14)
    assert out["ok"] is True
    assert out["status"] in ("stable", "alert", "insufficient_samples")


def test_options_flow_mock_yfinance():
    import pandas as pd
    from core.options_flow import probe_options_flow

    calls = pd.DataFrame({"volume": [800, 200]})
    puts = pd.DataFrame({"volume": [400, 100]})
    mock_chain = MagicMock()
    mock_chain.calls = calls
    mock_chain.puts = puts
    mock_ticker = MagicMock()
    mock_ticker.options = ("2026-06-20",)
    mock_ticker.option_chain = MagicMock(return_value=mock_chain)
    yf = MagicMock()
    yf.Ticker = MagicMock(return_value=mock_ticker)
    with patch.dict("sys.modules", {"yfinance": yf}):
        out = probe_options_flow("WOLF")
    assert out["ok"] is True
    assert out["available"] is True
    assert out["put_call_volume_ratio"] == pytest.approx(0.5, rel=0.01)


def test_ghost_api_contract_route():
    from fastapi.testclient import TestClient
    import wolf_app

    client = TestClient(wolf_app.APP)
    resp = client.get("/api/ghost/contract")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["north_star_retired"] is True


def test_ghost_api_blueprint_route():
    from fastapi.testclient import TestClient
    import wolf_app

    client = TestClient(wolf_app.APP)
    with patch("core.options_flow.probe_options_flow", return_value={"ok": True, "available": False}):
        with patch("core.feature_drift.compute_drift", return_value={"ok": True, "status": "stable", "alerts": []}):
            resp = client.get("/api/ghost/blueprint")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "phase1" in body
