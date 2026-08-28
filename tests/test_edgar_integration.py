"""Tests for core/edgar_integration.py's asof_ts point-in-time filter."""
from __future__ import annotations

from core import edgar_integration as edgar


def _result(*, filing_dates_and_items):
    filings = []
    material_events = []
    for date, item, category in filing_dates_and_items:
        filings.append({
            "filing_date": date, "accession_number": "x", "document": "x.htm",
            "items": [{"item": item, "category": category}], "url": None,
        })
        material_events.append({"date": date, "category": category, "item": item})
    filings.sort(key=lambda f: f["filing_date"], reverse=True)
    material_events.sort(key=lambda e: e["date"], reverse=True)
    return {
        "available": True, "symbol": "WOLF", "cik": "0000895419",
        "filings_count": len(filings), "filings": filings,
        "material_events": material_events,
        "latest_filing_date": filings[0]["filing_date"] if filings else None,
        "has_earnings": any(e["category"] == "earnings_results" for e in material_events),
        "has_delisting_risk": any(e["category"] == "delisting_notice" for e in material_events),
        "has_officer_change": any(e["category"] == "officer_departure_election" for e in material_events),
        "checked_at": 1_800_000_000,
    }


def test_apply_asof_filter_is_a_noop_on_unavailable_results():
    result = {"available": False, "reason": "no_cik_mapping"}
    assert edgar._apply_asof_filter(result, 1_800_000_000) == result


def test_apply_asof_filter_drops_filings_after_the_cutoff():
    result = _result(filing_dates_and_items=[
        ("2026-01-10", "5.02", "officer_departure_election"),
        ("2026-03-01", "2.02", "earnings_results"),
    ])
    # 2026-02-01 UTC: only the January filing was knowable by then.
    filtered = edgar._apply_asof_filter(result, 1769904000)
    assert filtered["filings_count"] == 1
    assert filtered["filings"][0]["filing_date"] == "2026-01-10"
    assert filtered["latest_filing_date"] == "2026-01-10"
    assert filtered["has_officer_change"] is True
    assert filtered["has_earnings"] is False


def test_apply_asof_filter_with_nothing_yet_filed_reports_no_events():
    result = _result(filing_dates_and_items=[("2026-03-01", "2.02", "earnings_results")])
    filtered = edgar._apply_asof_filter(result, 1_700_000_000)  # well before 2026
    assert filtered["filings_count"] == 0
    assert filtered["latest_filing_date"] is None
    assert filtered["has_earnings"] is False


def test_fetch_recent_8k_asof_ts_filters_a_cached_result(monkeypatch):
    result = _result(filing_dates_and_items=[
        ("2026-01-10", "5.02", "officer_departure_election"),
        ("2026-03-01", "2.02", "earnings_results"),
    ])
    edgar._edgar_cache["WOLF"] = (edgar.time.time(), result)

    live = edgar.fetch_recent_8k("WOLF")
    assert live["filings_count"] == 2  # no asof_ts -> unfiltered live cache read

    historical = edgar.fetch_recent_8k("WOLF", asof_ts=1769904000)
    assert historical["filings_count"] == 1
    assert historical["filings"][0]["filing_date"] == "2026-01-10"
