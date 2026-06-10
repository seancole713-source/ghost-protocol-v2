"""roadmap #3a — market-hours scan cadence + task registration.

Separate file to avoid colliding with other PRs' test appends.
"""
import wolf_app


def test_scan_gap_market_hours_vs_offhours(monkeypatch):
    monkeypatch.delenv("SCAN_INTERVAL_MARKET_MIN", raising=False)
    monkeypatch.delenv("SCAN_INTERVAL_OFFHOURS_MIN", raising=False)
    monkeypatch.delenv("GHOST_PREMARKET_SCAN", raising=False)

    # RTH
    monkeypatch.setattr("core.market_hours.is_us_rth", lambda now=None: True)
    monkeypatch.setattr("core.market_hours.is_us_premarket", lambda now=None: False)
    gap, is_market = wolf_app._market_scan_gap_s(None)
    assert is_market is True and gap == 30 * 60

    # Off hours
    monkeypatch.setattr("core.market_hours.is_us_rth", lambda now=None: False)
    monkeypatch.setattr("core.market_hours.is_us_premarket", lambda now=None: False)
    gap, is_market = wolf_app._market_scan_gap_s(None)
    assert is_market is False and gap == 60 * 60

    # Pre-market enabled
    monkeypatch.setattr("core.market_hours.is_us_premarket", lambda now=None: True)
    gap, is_market = wolf_app._market_scan_gap_s(None)
    assert is_market is True and gap == 30 * 60

    monkeypatch.setenv("GHOST_PREMARKET_SCAN", "0")
    _, is_market = wolf_app._market_scan_gap_s(None)
    assert is_market is False


def test_scan_gap_boundaries(monkeypatch):
    monkeypatch.delenv("GHOST_PREMARKET_SCAN", raising=False)
    monkeypatch.setattr("core.market_hours.is_us_premarket", lambda now=None: False)

    try:
        from zoneinfo import ZoneInfo
        import datetime as dt

        t_open = dt.datetime(2026, 6, 10, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        t_before_close = dt.datetime(2026, 6, 10, 15, 59, tzinfo=ZoneInfo("America/New_York"))
        t_at_close = dt.datetime(2026, 6, 10, 16, 0, tzinfo=ZoneInfo("America/New_York"))
    except Exception:
        import datetime as dt

        t_open = dt.datetime(2026, 6, 10, 9, 30)
        t_before_close = dt.datetime(2026, 6, 10, 15, 59)
        t_at_close = dt.datetime(2026, 6, 10, 16, 0)

    assert wolf_app._market_scan_gap_s(t_open)[1] is True
    assert wolf_app._market_scan_gap_s(t_before_close)[1] is True
    assert wolf_app._market_scan_gap_s(t_at_close)[1] is False


def test_scan_gap_env_tunable(monkeypatch):
    monkeypatch.setenv("SCAN_INTERVAL_MARKET_MIN", "15")
    monkeypatch.setenv("SCAN_INTERVAL_OFFHOURS_MIN", "120")
    monkeypatch.setattr("core.market_hours.is_us_rth", lambda now=None: True)
    monkeypatch.setattr("core.market_hours.is_us_premarket", lambda now=None: False)
    assert wolf_app._market_scan_gap_s(None)[0] == 15 * 60
    monkeypatch.setattr("core.market_hours.is_us_rth", lambda now=None: False)
    assert wolf_app._market_scan_gap_s(None)[0] == 120 * 60


def test_market_scan_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("MARKET_SCAN_ENABLED", "0")
    called = {"n": 0}
    monkeypatch.setattr("core.prediction.run_prediction_cycle", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    wolf_app._market_scan_job()
    assert called["n"] == 0   # disabled => never runs the cycle
