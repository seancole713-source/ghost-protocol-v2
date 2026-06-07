"""Tests for daily forecast scorecard helpers."""
from core.daily_forecast_scorecard import (
    forecast_ohlc_from_prob,
    score_forecast_vs_actual,
)


def test_forecast_ohlc_bullish():
    out = forecast_ohlc_from_prob(100.0, 0.72, "WOLF", "stock")
    assert out["bias"] == "UP"
    assert out["open"] > 100.0
    assert out["high"] > out["open"]
    assert out["low"] < 100.0


def test_forecast_ohlc_bearish():
    out = forecast_ohlc_from_prob(50.0, 0.35, "TSLA", "stock")
    assert out["bias"] == "DOWN"
    assert out["high"] >= out["open"]
    assert out["low"] < out["open"]


def test_score_forecast_perfect_match():
    pred = {"open": 10.0, "high": 11.0, "low": 9.5}
    actual = {"open": 10.0, "high": 11.0, "low": 9.5}
    sc = score_forecast_vs_actual(pred, actual)
    assert sc["overall_pct"] == 100.0
    assert sc["direction_ok"] is True


def test_score_forecast_partial_error():
    pred = {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5}
    actual = {"open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0}
    sc = score_forecast_vs_actual(pred, actual)
    assert sc["peak_rate"] is not None
    assert sc["peak_rate"] < 100.0
    assert sc["open_rate"] == 100.0
    assert sc["close_rate"] is not None


def test_next_trading_date_skips_weekend():
    from core.daily_forecast_scorecard import next_trading_date_after

    assert next_trading_date_after("2026-06-05") == "2026-06-08"  # Fri -> Mon
    assert next_trading_date_after("2026-06-04") == "2026-06-05"  # Thu -> Fri


def test_forecast_includes_close():
    out = forecast_ohlc_from_prob(100.0, 0.72, "WOLF", "stock")
    assert "close" in out
    assert out["close"] > out["open"]
