"""roadmap #3a — market-hours scan cadence + task registration.

Separate file to avoid colliding with other PRs' test appends.
"""
import datetime
import wolf_app


def _ct(weekday_hour_min):
    """Build a CT-naive datetime stand-in with the given weekday/hour/minute.
    _market_scan_gap_s only reads .weekday()/.hour/.minute."""
    wd, h, m = weekday_hour_min

    class _D:
        def weekday(self): return wd
        hour = h
        minute = m
    return _D()


def test_scan_gap_market_hours_vs_offhours(monkeypatch):
    monkeypatch.delenv("SCAN_INTERVAL_MARKET_MIN", raising=False)
    monkeypatch.delenv("SCAN_INTERVAL_OFFHOURS_MIN", raising=False)
    # Tuesday 10:00 CT — market open
    gap, is_market = wolf_app._market_scan_gap_s(_ct((1, 10, 0)))
    assert is_market is True and gap == 30 * 60
    # Tuesday 20:00 CT — after hours
    gap, is_market = wolf_app._market_scan_gap_s(_ct((1, 20, 0)))
    assert is_market is False and gap == 60 * 60
    # Saturday 10:00 CT — weekend
    gap, is_market = wolf_app._market_scan_gap_s(_ct((5, 10, 0)))
    assert is_market is False and gap == 60 * 60
    # Monday 08:00 CT — pre-market (before 8:30)
    _, is_market = wolf_app._market_scan_gap_s(_ct((0, 8, 0)))
    assert is_market is False


def test_scan_gap_boundaries(monkeypatch):
    # 08:30 inclusive open, 15:00 exclusive close
    assert wolf_app._market_scan_gap_s(_ct((2, 8, 30)))[1] is True
    assert wolf_app._market_scan_gap_s(_ct((2, 14, 59)))[1] is True
    assert wolf_app._market_scan_gap_s(_ct((2, 15, 0)))[1] is False


def test_scan_gap_env_tunable(monkeypatch):
    monkeypatch.setenv("SCAN_INTERVAL_MARKET_MIN", "15")
    monkeypatch.setenv("SCAN_INTERVAL_OFFHOURS_MIN", "120")
    assert wolf_app._market_scan_gap_s(_ct((1, 10, 0)))[0] == 15 * 60
    assert wolf_app._market_scan_gap_s(_ct((1, 23, 0)))[0] == 120 * 60


def test_market_scan_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("MARKET_SCAN_ENABLED", "0")
    called = {"n": 0}
    monkeypatch.setattr("core.prediction.run_prediction_cycle", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    wolf_app._market_scan_job()
    assert called["n"] == 0   # disabled => never runs the cycle
