"""tests/test_explosion_benchmark.py — preregistered explosion-event detection."""
from __future__ import annotations

import core.explosion_benchmark as eb


def _bars(closes):
    return [{"ts": 1000 + i * 86400, "close": c} for i, c in enumerate(closes)]


def test_detect_20pct_1d_event():
    events = eb.detect_explosion_events(_bars([10.0, 12.5]), symbol="ARCT")
    tiers = {e["tier"] for e in events}
    assert "+20%_1d" in tiers


def test_detect_100pct_20d_event():
    # +100% over 20 days.
    closes = [10.0] + [10.0 + i * 0.6 for i in range(1, 21)]  # ends at 22.0
    events = eb.detect_explosion_events(_bars(closes), symbol="ARCT")
    tiers = {e["tier"] for e in events}
    assert "+100%_20d" in tiers


def test_no_event_on_flat_series():
    events = eb.detect_explosion_events(_bars([10.0, 10.1, 10.2]), symbol="ARCT")
    assert events == []


def test_event_carries_start_and_peak():
    events = eb.detect_explosion_events(_bars([10.0, 12.5]), symbol="ARCT")
    e = next(e for e in events if e["tier"] == "+20%_1d")
    assert e["start_price"] == 10.0
    assert e["peak_price"] == 12.5
    assert e["move_pct"] == 25.0


def test_benchmark_summary_empty_db(monkeypatch):
    class _Cur:
        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            return []

    out = eb.benchmark_summary(cur=_Cur())
    assert out["ok"] is True
    assert out["total_events"] == 0
    assert out["overall_recall_pct"] == 0.0
