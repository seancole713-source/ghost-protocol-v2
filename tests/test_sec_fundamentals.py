"""Tests for core/sec_fundamentals.py's asof_ts point-in-time filter."""
from __future__ import annotations

from core import sec_fundamentals as sf


def _full_out(*, eps_filed_ts=1_700_000_000, revenue_filed_ts=1_700_050_000):
    return {
        "available": True, "symbol": "WOLF", "source": "sec_xbrl",
        "actual_eps": 0.20, "eps_actual": 0.20, "eps_year_ago": 0.10,
        "eps_period": "2026 Q2", "eps_basis": "yoy_trend", "eps_tag": "EarningsPerShareDiluted",
        "eps_filed_ts": eps_filed_ts,
        "revenue": 480_000_000, "revenue_year_ago": 420_000_000, "revenue_yoy": 0.1429,
        "revenue_period": "2026 Q2", "revenue_tag": "Revenues",
        "revenue_filed_ts": revenue_filed_ts,
        "cik": "0000895419", "checked_at": 1_800_000_000,
    }


def test_filed_ts_parses_iso_date_string():
    assert sf._filed_ts("2026-01-10") == 1768003200


def test_filed_ts_returns_none_for_unparseable_value():
    assert sf._filed_ts(None) is None
    assert sf._filed_ts("garbage") is None


def test_apply_asof_filter_is_a_noop_on_unavailable_results():
    result = {"available": False, "reason": "no_cik_mapping"}
    assert sf._apply_asof_filter(result, 1_800_000_000) == result


def test_apply_asof_filter_keeps_facts_filed_by_then():
    out = sf._apply_asof_filter(_full_out(), 1_800_000_000)
    assert out["available"] is True
    assert out["eps_actual"] == 0.20
    assert out["revenue"] == 480_000_000


def test_apply_asof_filter_drops_only_the_later_filed_fact():
    """EPS and revenue come from independent SEC concepts -- one being filed
    after asof_ts must not blank the other."""
    out = sf._apply_asof_filter(_full_out(eps_filed_ts=1_700_000_000, revenue_filed_ts=1_900_000_000), 1_800_000_000)
    assert out["available"] is True
    assert "eps_actual" in out
    assert "revenue" not in out
    assert "revenue_year_ago" not in out


def test_apply_asof_filter_reports_unavailable_when_nothing_survives():
    out = sf._apply_asof_filter(_full_out(eps_filed_ts=1_900_000_000, revenue_filed_ts=1_900_000_000), 1_800_000_000)
    assert out["available"] is False
    assert out["reason"] == "no_facts_known_as_of"


def test_apply_asof_filter_drops_a_fact_with_no_filed_date_at_all():
    """Missing provenance must fail closed, never be treated as 'always known'."""
    out = sf._apply_asof_filter(_full_out(eps_filed_ts=None), 1_800_000_000)
    assert "eps_actual" not in out
    assert "revenue" in out


def test_get_fundamentals_asof_ts_end_to_end(monkeypatch):
    sf._cache.clear()
    monkeypatch.setattr(
        sf, "_fetch_fundamentals_now",
        lambda sym: _full_out(eps_filed_ts=1_700_000_000, revenue_filed_ts=1_900_000_000),
    )
    live = sf.get_fundamentals("WOLF")
    assert live["available"] is True
    assert "revenue" in live  # no asof_ts -> unfiltered live read

    historical = sf.get_fundamentals("WOLF", asof_ts=1_800_000_000)
    assert historical["available"] is True
    assert "eps_actual" in historical
    assert "revenue" not in historical


def test_get_fundamentals_empty_symbol():
    assert sf.get_fundamentals("") == {"available": False, "symbol": "", "source": "sec_xbrl"}
