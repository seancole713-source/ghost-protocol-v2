"""Tests for core/earnings_surprise.py — pure mapping + fetch (mocked)."""
from core import earnings_surprise as es


def test_earnings_surprise_to_trigger_uses_relative_surprise(monkeypatch):
    """A smaller-than-expected loss still scores as a positive surprise."""
    monkeypatch.setattr(
        es, "get_earnings_surprise",
        lambda s: {
            "available": True,
            "eps_actual": -0.10,
            "eps_expected": -0.50,
            "revenue_actual": None,
            "quarter": "2026Q2",
        },
    )
    out = es.earnings_surprise_to_trigger("WOLF")
    assert out["earnings_available"] is True
    assert out["earnings_surprise"] > 50.0
    assert out["eps_surprise_pct"] == 80.0


def test_earnings_surprise_to_trigger_unavailable(monkeypatch):
    monkeypatch.setattr(es, "get_earnings_surprise", lambda s: {"available": False})
    out = es.earnings_surprise_to_trigger("WOLF")
    assert out["earnings_available"] is False
    assert out["earnings_surprise"] == 0.0


def test_get_earnings_surprise_empty_symbol():
    assert es.get_earnings_surprise("") == {"available": False}


def test_report_ts_parses_iso_date_string():
    assert es._report_ts("2026-07-24") == 1784851200


def test_report_ts_returns_none_for_unparseable_label():
    assert es._report_ts("not-a-date") is None


def test_asof_ts_accepts_a_report_known_by_then(monkeypatch):
    es._cache.clear()
    monkeypatch.setattr(
        es, "_latest_quarter_earnings",
        lambda s: {
            "available": True, "eps_actual": 0.10, "eps_expected": 0.05,
            "eps_surprise_pct": 100.0, "revenue_actual": None,
            "revenue_expected": None, "quarter": "2026-07-24", "report_ts": 1784851200,
        },
    )
    out = es.get_earnings_surprise("WOLF", asof_ts=1784851200 + 3600)
    assert out["available"] is True
    assert out["eps_actual"] == 0.10


def test_asof_ts_rejects_a_report_from_after_the_decision_time(monkeypatch):
    es._cache.clear()
    """The report yfinance has on file was published after asof_ts -- it was
    not yet knowable at that decision time, so this must fail closed rather
    than launder a present-day read as historical knowledge."""
    monkeypatch.setattr(
        es, "_latest_quarter_earnings",
        lambda s: {
            "available": True, "eps_actual": 0.10, "eps_expected": 0.05,
            "eps_surprise_pct": 100.0, "revenue_actual": None,
            "revenue_expected": None, "quarter": "2026-07-24", "report_ts": 1784851200,
        },
    )
    out = es.get_earnings_surprise("WOLF", asof_ts=1784851200 - 3600)
    assert out["available"] is False
    assert out["reason"] == "report_ts_after_or_unknown_asof"


def test_asof_ts_rejects_a_report_with_no_parseable_timestamp(monkeypatch):
    es._cache.clear()
    monkeypatch.setattr(
        es, "_latest_quarter_earnings",
        lambda s: {
            "available": True, "eps_actual": 0.10, "eps_expected": 0.05,
            "eps_surprise_pct": 100.0, "revenue_actual": None,
            "revenue_expected": None, "quarter": "garbage", "report_ts": None,
        },
    )
    out = es.get_earnings_surprise("WOLF", asof_ts=1784851200)
    assert out["available"] is False
