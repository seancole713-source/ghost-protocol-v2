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
    pred = {"open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "up_prob": 0.72}
    actual = {"open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5}
    sc = score_forecast_vs_actual(pred, actual)
    assert sc["overall_pct"] == 100.0
    assert sc["direction_ok"] is True
    assert sc["direction_source"] == "classifier_up_prob"


def test_score_direction_uses_up_prob_not_ohlc_band():
    """Classifier says UP (0.72) even when band geometry looks DOWN."""
    pred = {"open": 10.2, "high": 10.3, "low": 9.0, "close": 9.8, "up_prob": 0.72}
    actual = {"open": 10.0, "high": 11.0, "low": 9.5, "close": 10.8}
    sc = score_forecast_vs_actual(pred, actual)
    assert sc["direction_ok"] is True
    assert sc["direction_source"] == "classifier_up_prob"

    pred_down = dict(pred, up_prob=0.30)
    sc2 = score_forecast_vs_actual(pred_down, actual)
    assert sc2["direction_ok"] is False


def test_score_forecast_partial_error():
    pred = {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5}
    actual = {"open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0}
    sc = score_forecast_vs_actual(pred, actual)
    assert sc["peak_rate"] is not None
    assert sc["peak_rate"] < 100.0
    assert sc["open_rate"] == 100.0
    assert sc["low_rate"] == 100.0
    assert sc["close_rate"] is not None


def test_next_trading_date_skips_weekend():
    from core.daily_forecast_scorecard import next_trading_date_after

    assert next_trading_date_after("2026-06-05") == "2026-06-08"  # Fri -> Mon
    assert next_trading_date_after("2026-06-04") == "2026-06-05"  # Thu -> Fri


def test_scorecard_fetch_falls_back_to_longer_period(monkeypatch):
    from core import daily_forecast_scorecard as dfs

    calls = []

    def fake_fetch(symbol, asset_type, period="2y"):
        calls.append(period)
        if period == "3mo":
            return [{"ts": "2026-01-01T00:00:00Z", "close": 1.0}] * 10
        if period in ("2y", "1y"):
            return [{"ts": f"2025-05-{i:02d}T00:00:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5}
                    for i in range(1, 40)]
        return None

    monkeypatch.setattr("core.signal_engine._fetch_ohlcv", fake_fetch)
    monkeypatch.setenv("V3_SCORECARD_OHLCV_PERIOD", "3mo")
    rows, period = dfs._fetch_scorecard_rows("RDFN", "stock")
    assert rows is not None
    assert len(rows) >= 30
    assert period in ("2y", "1y")
    assert "3mo" in calls


def test_last_bar_age_days():
    from core.daily_forecast_scorecard import _last_bar_age_days

    rows = [{"ts": "2025-06-01T00:00:00Z", "close": 1.0}]
    age = _last_bar_age_days(rows)
    assert age is not None
    assert age > 30


def test_daily_bar_in_progress_until_3pm_ct(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from core.daily_forecast_scorecard import _daily_bar_in_progress

    rows = [{"ts": "2026-06-08T00:00:00Z", "close": 1.0}]

    class FakeDT:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 6, 8, 14, 30, tzinfo=ZoneInfo("America/Chicago"))

    monkeypatch.setattr("core.daily_forecast_scorecard.datetime", FakeDT)
    assert _daily_bar_in_progress(rows) is True

    class FakeDTAfter:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 6, 8, 15, 5, tzinfo=ZoneInfo("America/Chicago"))

    monkeypatch.setattr("core.daily_forecast_scorecard.datetime", FakeDTAfter)
    assert _daily_bar_in_progress(rows) is False


def test_live_now_quote_shape(monkeypatch):
    from core.daily_forecast_scorecard import live_now_quote

    monkeypatch.setattr(
        "core.prices.get_intraday_session",
        lambda s: {
            "symbol": "SPCE",
            "session": "rth",
            "session_label": "Market open",
            "market_date": "2026-06-08",
            "price": 4.39,
            "previous_close": 4.36,
            "change_pct": 0.688,
            "today_open": 4.55,
            "today_high": 4.56,
            "today_low": 4.13,
            "feed": "alpaca_sip",
        },
    )
    out = live_now_quote("SPCE")
    assert out["symbol"] == "SPCE"
    assert out["price"] == 4.39
    assert out["today_open"] == 4.55
    assert out["change_pct"] is not None


def test_forecast_includes_close():
    out = forecast_ohlc_from_prob(100.0, 0.72, "WOLF", "stock")
    assert "close" in out
    assert out["close"] > out["open"]


def test_parse_bar_date_uses_et_session():
    from core.daily_forecast_scorecard import _parse_bar_date

    # Alpaca daily bar for Mon Jun 8 RTH often stamped early UTC; still Mon ET.
    assert _parse_bar_date("2026-06-08T04:00:00Z") == "2026-06-08"
    assert _parse_bar_date("2026-06-09T00:00:00Z") == "2026-06-08"


def test_panel_session_dates_weekend():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from core.daily_forecast_scorecard import panel_session_dates

    sun = datetime(2026, 6, 7, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    d = panel_session_dates(sun)
    assert d["predict_date"] == "2026-06-08"
    assert d["market_date"] == "2026-06-05"
    assert d["live_date"] == "2026-06-05"
    assert d["live_label"] == "last session"


def test_build_prediction_panel_aligns_weekend(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from core.daily_forecast_scorecard import build_prediction_panel

    rows = [
        {"ts": "2026-06-04T04:00:00Z", "open": 4.0, "high": 4.1, "low": 3.9, "close": 4.05},
        {"ts": "2026-06-05T04:00:00Z", "open": 4.55, "high": 4.56, "low": 4.13, "close": 4.39},
    ]

    class FakeModel:
        def predict_proba(self, X):
            return [[0.4, 0.6]]

    monkeypatch.setattr(
        "core.daily_forecast_scorecard._up_prob_at_bar",
        lambda *a, **k: 0.65,
    )
    monkeypatch.setattr(
        "core.daily_forecast_scorecard.forecast_band_vol_pct",
        lambda *a, **k: {"vol_pct": 0.05, "source": "test"},
    )

    sun = datetime(2026, 6, 7, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    panel = build_prediction_panel(
        "SPCE",
        rows,
        [],
        FakeModel(),
        ["f1"],
        [],
        "stock",
        {"price": 4.39, "today_open": 4.55, "today_high": 4.56, "today_low": 4.13},
        now_et=sun,
    )
    assert panel["predict"]["target_date"] == "2026-06-08"
    assert panel["market"]["target_date"] == "2026-06-05"
    assert panel["market"]["actual"]["low"] == 4.13
    assert panel["live"]["panel_date"] == "2026-06-05"


def test_market_uses_rth_close_after_hours(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from core.daily_forecast_scorecard import build_prediction_panel

    rows = [
        {"ts": "2026-06-08T04:00:00Z", "open": 4.55, "high": 4.56, "low": 4.13, "close": 4.40},
    ]
    monkeypatch.setattr("core.daily_forecast_scorecard._up_prob_at_bar", lambda *a, **k: 0.65)
    monkeypatch.setattr(
        "core.daily_forecast_scorecard.forecast_band_vol_pct",
        lambda *a, **k: {"vol_pct": 0.05, "source": "test"},
    )
    mon_eh = datetime(2026, 6, 8, 17, 30, tzinfo=ZoneInfo("America/New_York"))
    live = {
        "price": 4.165,
        "today_open": 4.545,
        "today_high": 4.545,
        "today_low": 4.135,
        "rth_close": 4.40,
        "session": "afterhours",
    }
    panel = build_prediction_panel(
        "SPCE", rows, [], object(), ["f1"], [], "stock", live, now_et=mon_eh,
    )
    assert panel["market"]["actual"]["close"] == 4.40
    assert panel["live"]["price"] == 4.165
    assert panel["live"]["panel_label"] == "after hours"


def test_live_panel_backfills_ohlc_from_market_when_quote_price_only(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from core.daily_forecast_scorecard import build_prediction_panel

    rows = [
        {"ts": "2026-06-09T04:00:00Z", "open": 4.41, "high": 4.92, "low": 4.27, "close": 4.59},
        {"ts": "2026-06-10T04:00:00Z", "open": 4.41, "high": 4.92, "low": 4.27, "close": 4.68},
    ]
    monkeypatch.setattr("core.daily_forecast_scorecard._up_prob_at_bar", lambda *a, **k: 0.65)
    monkeypatch.setattr(
        "core.daily_forecast_scorecard.forecast_band_vol_pct",
        lambda *a, **k: {"vol_pct": 0.05, "source": "test"},
    )
    wed_eh = datetime(2026, 6, 10, 16, 2, tzinfo=ZoneInfo("America/New_York"))
    live = {"price": 4.68, "session": "afterhours", "rth_close": 4.68}
    panel = build_prediction_panel(
        "SPCE", rows, [], object(), ["f1"], [], "stock", live, now_et=wed_eh,
    )
    assert panel["live"]["today_open"] == 4.41
    assert panel["live"]["today_high"] == 4.92
    assert panel["live"]["today_low"] == 4.27
    assert panel["live"]["price"] == 4.68

