"""Extended-session values must keep their own observation provenance."""
from __future__ import annotations

from datetime import datetime

from core import prices


def _patch_extended_session_inputs(monkeypatch, *, pre_market):
    monkeypatch.setattr(prices, "_alpaca_trade_quote", lambda _symbol: (10.0, 1_700_000_000))
    monkeypatch.setattr(prices, "_now_ct", lambda: datetime(2026, 3, 16, 8, 0))
    monkeypatch.setattr(prices, "is_us_rth", lambda _now: False)
    monkeypatch.setattr(prices, "is_us_premarket", lambda _now: True)
    monkeypatch.setattr(prices, "is_us_after_hours", lambda _now: False)

    class _FastInfo:
        previous_close = 9.0
        pre_market_price = pre_market
        post_market_price = None

    class _Ticker:
        fast_info = _FastInfo()

    import yfinance as yf
    from core.circuit_breaker import _yfinance_cb

    monkeypatch.setattr(_yfinance_cb, "allow", lambda: True)
    monkeypatch.setattr(_yfinance_cb, "record_success", lambda: None)
    monkeypatch.setattr(yf, "Ticker", lambda _symbol: _Ticker())


def test_yfinance_session_override_does_not_reuse_alpaca_timestamp(monkeypatch):
    _patch_extended_session_inputs(monkeypatch, pre_market=11.0)

    out = prices.get_extended_session("WOLF")

    assert out["session_price"] == 11.0
    assert out["price_source"] == "yfinance_fast_info"
    assert out["price_as_of_ts"] is None


def test_alpaca_session_price_keeps_alpaca_timestamp(monkeypatch):
    _patch_extended_session_inputs(monkeypatch, pre_market=None)

    out = prices.get_extended_session("WOLF")

    assert out["session_price"] == 10.0
    assert out["price_source"] == "alpaca_trade"
    assert out["price_as_of_ts"] == 1_700_000_000
