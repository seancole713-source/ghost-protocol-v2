from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.daily_bar_contract import bar_session_date, completed_daily_bars, prior_daily_bars


def ct(month, day, hour, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=ZoneInfo("America/Chicago"))


@pytest.mark.parametrize("stamp", ["2026-09-15", "2026-09-15T00:00:00Z", "2026-09-15T04:00:00Z"])
def test_provider_daily_labels_do_not_shift_to_prior_ct_day(stamp):
    assert bar_session_date(stamp) == date(2026, 9, 15)


def test_prior_close_uses_yesterday_before_and_after_todays_bar_arrives():
    rows = [{"t": "2026-09-14", "c": 90, "v": 100}, {"t": "2026-09-15", "c": 100, "v": 200}]
    assert prior_daily_bars(rows, date(2026, 9, 16)) == rows
    assert prior_daily_bars(rows + [{"t": "2026-09-16", "c": 105, "v": 1}], date(2026, 9, 16)) == rows
    assert prior_daily_bars(rows[:1], date(2026, 9, 16)) == []


@pytest.mark.parametrize("hour,minute", [(8, 0), (8, 30), (12, 0), (15, 4)])
def test_model_excludes_partial_daily_bar(hour, minute):
    rows = [{"ts": "2026-09-15", "close": 100}, {"ts": "2026-09-16", "close": 999}]
    assert completed_daily_bars(rows, now=ct(9, 16, hour, minute)) == rows[:1]


def test_model_requires_today_after_issuance_delay():
    rows = [{"ts": "2026-09-15"}, {"ts": "2026-09-16"}]
    assert completed_daily_bars(rows, now=ct(9, 16, 15, 5)) == rows
    assert completed_daily_bars(rows[:1], now=ct(9, 16, 15, 5)) == []


def test_holiday_weekend_and_early_close():
    friday = [{"ts": "2026-09-04"}]
    assert completed_daily_bars(friday, now=ct(9, 7, 16)) == friday
    assert completed_daily_bars(friday, now=ct(9, 8, 8)) == friday
    wednesday = [{"ts": "2026-11-25"}]
    half_day = wednesday + [{"ts": "2026-11-27"}]
    assert completed_daily_bars(half_day, now=ct(11, 27, 12, 4)) == wednesday
    assert completed_daily_bars(half_day, now=ct(11, 27, 12, 5)) == half_day
    assert completed_daily_bars(half_day, now=ct(11, 28, 16)) == half_day


def test_live_score_never_overlays_extended_quote_or_partial_bar(monkeypatch):
    import core.signal_engine as engine

    rows = [{"ts": (date(2026, 9, 15) - timedelta(days=i)).isoformat(), "close": 100}
            for i in reversed(range(35))]
    rows.append({"ts": "2026-09-16", "close": 999})
    monkeypatch.setattr("core.market_hours._now_ct", lambda: ct(9, 16, 8))
    monkeypatch.setattr(engine, "load_model", lambda *a: (object(), [], {}))
    monkeypatch.setattr(engine, "_fetch_ohlcv", lambda *a, **kw: rows)
    monkeypatch.setattr("core.prediction._is_premarket", lambda: True)
    monkeypatch.setattr("core.prediction._premarket_scan_enabled", lambda: True)
    monkeypatch.setattr("core.prices.get_extended_session", lambda s: {"session_price": 999})
    observed = []

    def capture(bars):
        observed.extend(bars)
        raise RuntimeError("stop after feature-input inspection")

    monkeypatch.setattr(engine, "_calculate_features", capture)
    with pytest.raises(RuntimeError, match="feature-input inspection"):
        engine.predict_live_ex("AAA", "stock")
    assert observed[-1]["close"] == 100
    assert observed[-1]["ts"] == "2026-09-15"
    assert rows[-1]["close"] == 999  # supplier data was not mutated


def test_radar_premarket_gap_and_volume_use_completed_baseline(monkeypatch):
    import core.squeeze_monitor as radar

    daily = [{"t": "2026-09-14T04:00:00Z", "c": 90, "v": 100},
             {"t": "2026-09-15T04:00:00Z", "c": 100, "v": 300}]
    bars = {"AAA": {"daily": daily, "intraday": [
        {"t": "2026-09-16T12:00:00Z", "c": 105, "h": 106, "l": 104, "v": 20}]}}
    monkeypatch.setattr(radar, "_batch_bars", bars)
    before = radar._metrics_from_batch_bars("AAA")
    daily.append({"t": "2026-09-16T04:00:00Z", "c": 105, "v": 1})
    after = radar._metrics_from_batch_bars("AAA")
    assert before == after
    assert before["current_move_pct"] == 5
    assert before["avg_daily_volume"] == 200
    assert before["reference_session_date"] == "2026-09-15"


def test_performance_log_preserves_feature_clock():
    from core.performance_log import _trim_scores

    out = _trim_scores({
        "feature_timeframe": "completed_daily_bar",
        "feature_bar_ts": "2026-09-15T04:00:00Z",
        "irrelevant_large_payload": {"raw": "discard"},
    })
    assert out == {
        "feature_timeframe": "completed_daily_bar",
        "feature_bar_ts": "2026-09-15T04:00:00Z",
    }
