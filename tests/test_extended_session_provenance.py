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


# ------------------------------------------------ one basis or none (2026-09-22) --
#
# Caught live on 2026-09-22 premarket. WOLF, SHOP and BB had no Alpaca print yet
# that morning, so the "live" price was Monday's 16:00 ET closing trade -- 17.4h
# old -- labelled "premarket" and priced against the previous close. WOLF showed
# a +16.9% premarket gap that was Monday's completed move. The same bug PR #194
# fixed for the discovery screener, in the main quote path.

def _patch_session(monkeypatch, *, trade_ts, pre_market=None, prev_close=23.74, price=27.75):
    from zoneinfo import ZoneInfo
    ct = ZoneInfo("America/Chicago")
    monkeypatch.setattr(prices, "_alpaca_trade_quote", lambda _s: (price, trade_ts))
    monkeypatch.setattr(prices, "_reject_phantom", lambda _s, p: (p, None))
    # Tue 2026-09-22 08:25 CT == 09:25 ET, premarket.
    monkeypatch.setattr(prices, "_now_ct", lambda: datetime(2026, 9, 22, 8, 25, tzinfo=ct))
    monkeypatch.setattr(prices, "is_us_rth", lambda _now: False)
    monkeypatch.setattr(prices, "is_us_premarket", lambda _now: True)
    monkeypatch.setattr(prices, "is_us_after_hours", lambda _now: False)

    class _FastInfo:
        previous_close = prev_close
        pre_market_price = pre_market
        post_market_price = None

    class _Ticker:
        fast_info = _FastInfo()

    import yfinance as yf
    from core.circuit_breaker import _yfinance_cb
    monkeypatch.setattr(_yfinance_cb, "allow", lambda: True)
    monkeypatch.setattr(_yfinance_cb, "record_success", lambda: None)
    monkeypatch.setattr(yf, "Ticker", lambda _s: _Ticker())


# Mon 2026-09-21 16:00 ET, the exact print WOLF was served from.
_MONDAY_CLOSE_TRADE = 1790020754
# Tue 2026-09-22 09:10 ET, a genuine premarket print.
_TUESDAY_PREMARKET_TRADE = 1790082600


def test_yesterdays_last_trade_is_not_a_premarket_gap(monkeypatch):
    _patch_session(monkeypatch, trade_ts=_MONDAY_CLOSE_TRADE)

    out = prices.get_extended_session("WOLF")

    assert out["session"] == "premarket"
    assert out["session_price_basis"] == "prior_session_trade"
    # The number is still reported -- it is simply not today's gap.
    assert out["session_price"] == 27.75
    assert out["gap_pct"] is None
    assert out["gap_abs"] is None
    assert out["session_price_age_s"] > 17 * 3600


def test_a_genuine_premarket_print_keeps_its_gap(monkeypatch):
    _patch_session(monkeypatch, trade_ts=_TUESDAY_PREMARKET_TRADE, price=24.93)

    out = prices.get_extended_session("WOLF")

    assert out["session_price_basis"] == "current_session"
    assert out["gap_pct"] == round((24.93 - 23.74) / 23.74 * 100, 3)


def test_session_start_is_4am_eastern_in_premarket(monkeypatch):
    _patch_session(monkeypatch, trade_ts=_TUESDAY_PREMARKET_TRADE)
    out = prices.get_extended_session("WOLF")
    # 2026-09-22 04:00 America/New_York
    assert out["session_start_ts"] == 1790064000


def test_an_untimestamped_price_is_labelled_unverified(monkeypatch):
    """yfinance fast_info carries no market time. Keep the gap (existing
    behaviour) but say plainly that its session is unverified."""
    _patch_session(monkeypatch, trade_ts=_MONDAY_CLOSE_TRADE, pre_market=24.50)

    out = prices.get_extended_session("WOLF")

    assert out["price_source"] == "yfinance_fast_info"
    assert out["session_price_basis"] == "unverified_time"
    assert out["gap_pct"] is not None
