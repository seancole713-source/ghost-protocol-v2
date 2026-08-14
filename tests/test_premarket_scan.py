"""Pre-market watchlist scans + market-scan cadence."""
import wolf_app
from core import prediction as pred


def _ct(weekday_hour_min):
    wd, h, m = weekday_hour_min

    class _D:
        def weekday(self):
            return wd

        hour = h
        minute = m

    return _D()


def test_watchlist_scan_enabled_premarket_default(monkeypatch):
    monkeypatch.setattr(pred, "_is_premarket", lambda: True)
    monkeypatch.delenv("GHOST_PREMARKET_SCAN", raising=False)
    assert pred._watchlist_scan_enabled() is True


def test_watchlist_scan_disabled_when_opt_out(monkeypatch):
    monkeypatch.setattr(pred, "_is_premarket", lambda: True)
    monkeypatch.setenv("GHOST_PREMARKET_SCAN", "0")
    assert pred._watchlist_scan_enabled() is False


def test_premarket_scan_gap_uses_market_interval(monkeypatch):
    monkeypatch.delenv("SCAN_INTERVAL_MARKET_MIN", raising=False)
    monkeypatch.delenv("SCAN_INTERVAL_OFFHOURS_MIN", raising=False)
    monkeypatch.delenv("GHOST_PREMARKET_SCAN", raising=False)
    monkeypatch.setattr("core.market_hours.is_us_premarket", lambda now=None: True)
    monkeypatch.setattr("core.market_hours.is_us_rth", lambda now=None: False)
    gap, is_market = wolf_app._market_scan_gap_s(None)
    assert is_market is True and gap == 30 * 60


def test_premarket_scan_gap_respects_opt_out(monkeypatch):
    monkeypatch.setenv("GHOST_PREMARKET_SCAN", "0")
    monkeypatch.setattr("core.market_hours.is_us_premarket", lambda now=None: True)
    monkeypatch.setattr("core.market_hours.is_us_rth", lambda now=None: False)
    _, is_market = wolf_app._market_scan_gap_s(None)
    assert is_market is False


def test_premarket_quality_is_brake_first_without_volume():
    from core.catalyst_scoring import score_premarket_quality

    constructive = score_premarket_quality({
        "session": "premarket",
        "previous_close": 10.0,
        "session_price": 10.4,
        "live_price": 10.38,
        "gap_pct": 4.0,
    })
    chase = score_premarket_quality({
        "session": "premarket",
        "previous_close": 10.0,
        "session_price": 11.4,
        "live_price": 11.05,
        "gap_pct": 14.0,
    })
    failed = score_premarket_quality({
        "session": "premarket",
        "previous_close": 10.0,
        "session_price": 10.5,
        "live_price": 9.95,
        "gap_pct": 5.0,
    })
    assert constructive["available"] is True
    assert constructive["score"] > 0
    assert constructive["confidence"] < 0.6
    assert chase["score"] < constructive["score"]
    assert failed["score"] < 0
