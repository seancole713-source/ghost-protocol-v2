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
    gap, is_market = wolf_app._market_scan_gap_s(_ct((0, 8, 0)))
    assert is_market is True and gap == 30 * 60


def test_premarket_scan_gap_respects_opt_out(monkeypatch):
    monkeypatch.setenv("GHOST_PREMARKET_SCAN", "0")
    _, is_market = wolf_app._market_scan_gap_s(_ct((0, 8, 0)))
    assert is_market is False
