"""tests/test_options_edge.py — PCR-edge test harness (built ahead of data).

Pure bucket/Wilson logic, verified with synthetic paired rows so the harness
is trustworthy the moment real forward data is sufficient. The harness must be
honest in every regime: no data, flat (no edge), discriminating, and proven.
"""
from __future__ import annotations

import core.options_edge as oe


def _rows(spec):
    """spec: list of (pcr, n_win, n_loss) -> paired-row dicts."""
    out = []
    for pcr, w, l in spec:
        out += [{"pcr": pcr, "outcome": "WIN"} for _ in range(w)]
        out += [{"pcr": pcr, "outcome": "LOSS"} for _ in range(l)]
    return out


class TestBucketing:
    def test_bucket_boundaries(self):
        assert oe._bucket_for(0.4) == "<0.5"
        assert oe._bucket_for(0.5) == "0.5-0.7"
        assert oe._bucket_for(0.99) == "0.7-1.0"
        assert oe._bucket_for(1.0) == "1.0-1.5"
        assert oe._bucket_for(3.0) == ">=1.5"
        assert oe._bucket_for(-1) is None


class TestSummarize:
    def test_no_data(self):
        r = oe.summarize_pcr_edge([])
        assert r["verdict"] == "NO_DATA" and r["total_paired"] == 0

    def test_flat_no_edge(self):
        # Same ~50% win rate in every bucket → PCR carries no signal.
        r = oe.summarize_pcr_edge(_rows([(0.4, 25, 25), (0.8, 25, 25), (2.0, 25, 25)]))
        assert r["verdict"] == "FLAT_NO_EDGE"
        assert r["discriminates"] is False
        assert r["proven_70_buckets"] == []

    def test_discriminates_unproven(self):
        # Real spread (bullish flow wins more) but samples too thin to prove 70.
        r = oe.summarize_pcr_edge(_rows([(0.4, 9, 3), (2.0, 3, 9)]))
        assert r["win_rate_spread"] >= 0.10
        assert r["verdict"] == "DISCRIMINATES_UNPROVEN"

    def test_proven_70(self):
        # A bucket with a large, lopsided sample clears the family-corrected 70.
        r = oe.summarize_pcr_edge(_rows([(0.4, 180, 20), (2.0, 40, 160)]))
        assert "<0.5" in r["proven_70_buckets"]
        assert r["verdict"] == "PROVEN_70"

    def test_expired_counts_as_non_win(self):
        rows = ([{"pcr": 0.4, "outcome": "WIN"}] * 10
                + [{"pcr": 0.4, "outcome": "EXPIRED"}] * 10)
        r = oe.summarize_pcr_edge(rows)
        b = next(x for x in r["buckets"] if x["pcr_bucket"] == "<0.5")
        assert b["n"] == 20 and b["wins"] == 10  # expired in denominator, not a win


class TestLiveWrappers:
    def test_edge_insufficient_when_thin(self, monkeypatch):
        monkeypatch.setattr(oe, "load_paired_rows",
                            lambda days=60, limit=50000: _rows([(0.4, 5, 5)]))
        monkeypatch.setattr(oe, "options_pcr_readiness",
                            lambda days=60: {"distinct_days": 1, "paired_with_outcomes": 10})
        out = oe.options_pcr_edge()
        assert out["sufficient_data"] is False   # provisional until data accrues
        assert out["result"]["total_paired"] == 10

    def test_edge_read_failure_is_honest(self, monkeypatch):
        monkeypatch.setattr(oe, "load_paired_rows", lambda days=60, limit=50000: None)
        assert oe.options_pcr_edge()["status"] == "READ_FAILED"


class TestRoutes:
    def test_routes_registered(self):
        from api.routes_ghost_system import router
        paths = [r.path for r in router.routes]
        assert "/api/ghost/options/readiness" in paths
        assert "/api/ghost/options/edge-test" in paths
