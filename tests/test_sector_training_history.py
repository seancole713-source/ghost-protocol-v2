"""Sector context must span the same history as target training bars."""
from datetime import date, timedelta

import pytest

from core import signal_engine as engine


def _bars(count, step):
    return [
        {"ts": (date(2024, 1, 1) + timedelta(days=i)).isoformat(),
         "open": 100 + step * i, "high": 101 + step * i,
         "low": 99 + step * i, "close": 100 + step * i, "volume": 1000}
        for i in range(count)
    ]


@pytest.mark.parametrize("period", ["2y", "5y"])
def test_backtest_requests_matching_sector_history(monkeypatch, period):
    monkeypatch.setenv("V3_OHLCV_PERIOD", period)
    monkeypatch.setenv("V3_SECTOR_FEATURE", "on")
    monkeypatch.setenv("V3_MACRO_FEATURES", "off")
    monkeypatch.setenv("V3_FUNDAMENTAL_FEATURES", "off")
    monkeypatch.setenv("V3_LABEL_TYPE", "tp_sl")
    target, sector = _bars(450, 0.2), _bars(450, 0.05)
    requested = []

    def fetch_sector(period="1y"):
        requested.append(period)
        return sector if period in ("2y", "5y") else sector[-250:]

    monkeypatch.setattr(engine, "_fetch_ohlcv", lambda *a, **kw: target)
    monkeypatch.setattr(engine, "_fetch_sector_series", fetch_sector)
    monkeypatch.setattr(engine, "_calculate_features", lambda rows: {})
    monkeypatch.setattr("core.tp_sl_resolve.simulate_tp_sl_label_detail",
                        lambda *a, **kw: ("WIN", 1800000000))
    up, down = engine.backtest_symbol("AAA", "stock")

    assert requested == [period]
    assert up and down
    first = engine._effective_backtest_window(len(target))
    aligned = engine._align_sector_closes(target, sector)
    expected = engine._sector_rel_at(target, aligned, first, engine._v3_sector_lookback())
    assert expected != 0
    assert up[0]["features"]["sector_rel_strength"] == pytest.approx(expected)
    assert down[0]["features"]["sector_rel_strength"] == pytest.approx(expected)


def test_disabled_sector_does_not_fetch_context(monkeypatch):
    monkeypatch.setenv("V3_SECTOR_FEATURE", "off")
    monkeypatch.setenv("V3_MACRO_FEATURES", "off")
    monkeypatch.setenv("V3_FUNDAMENTAL_FEATURES", "off")
    monkeypatch.setenv("V3_LABEL_TYPE", "tp_sl")
    monkeypatch.setattr(engine, "_fetch_ohlcv", lambda *a, **kw: _bars(150, 0.2))
    monkeypatch.setattr(engine, "_calculate_features", lambda rows: {})
    monkeypatch.setattr("core.tp_sl_resolve.simulate_tp_sl_label_detail",
                        lambda *a, **kw: ("WIN", 1800000000))

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled sector feature fetched context")

    monkeypatch.setattr(engine, "_fetch_sector_series", forbidden)
    up, _ = engine.backtest_symbol("AAA", "stock")
    assert up and "sector_rel_strength" not in up[0]["features"]
