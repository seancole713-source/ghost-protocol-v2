"""Tests for squeeze daily log (core.squeeze_outcomes) — pure helpers, no live DB."""

from core.squeeze_outcomes import (
    _parse_bar_date,
    _resolve_row,
    record_squeeze_prediction,
    squeeze_daily_log,
    squeeze_log_enabled,
)


def test_squeeze_log_enabled_default_on(monkeypatch):
    monkeypatch.delenv("SQUEEZE_DAILY_LOG", raising=False)
    assert squeeze_log_enabled() is True


def test_squeeze_log_disabled(monkeypatch):
    monkeypatch.setenv("SQUEEZE_DAILY_LOG", "0")
    assert squeeze_log_enabled() is False
    assert record_squeeze_prediction({"symbol": "HOOD", "buy": 1, "sell": 2}) is None
    out = squeeze_daily_log()
    assert out["enabled"] is False


def test_parse_bar_date_iso_to_et_session():
    assert _parse_bar_date("2026-06-10T20:00:00Z") == "2026-06-10"


def test_resolve_row_win_when_high_reaches_sell():
    meta = _resolve_row(10.0, 12.0, 8.0, {"open": 10.0, "high": 12.5, "low": 9.8, "close": 11.2})
    assert meta["outcome"] == "WIN"
    assert meta["hit_target"] is True
    assert meta["hit_stop"] is False
    assert meta["session_close"] == 11.2
    assert meta["close_pnl_pct"] == 12.0
    assert meta["target_gap_pct"] == round((11.2 - 12.0) / 12.0 * 100, 3)


def test_resolve_row_loss_when_low_hits_stop():
    meta = _resolve_row(10.0, 12.0, 9.5, {"open": 10.0, "high": 10.4, "low": 9.4, "close": 9.7})
    assert meta["outcome"] == "LOSS"
    assert meta["hit_stop"] is True
    assert meta["hit_target"] is False


def test_resolve_row_mixed_when_both_hit():
    meta = _resolve_row(10.0, 11.0, 9.0, {"open": 10.0, "high": 11.5, "low": 8.8, "close": 10.2})
    assert meta["outcome"] == "MIXED"
    assert meta["hit_target"] is True
    assert meta["hit_stop"] is True


def test_resolve_row_neutral_when_neither_hit():
    meta = _resolve_row(10.0, 12.0, 8.0, {"open": 10.0, "high": 10.2, "low": 9.2, "close": 10.1})
    assert meta["outcome"] == "NEUTRAL"
    assert meta["hit_3pct"] is False


def test_resolve_row_hit_3pct_flag():
    meta = _resolve_row(10.0, 15.0, 8.0, {"open": 10.0, "high": 10.31, "low": 9.9, "close": 10.2})
    assert meta["hit_3pct"] is True
    assert meta["outcome"] == "NEUTRAL"
