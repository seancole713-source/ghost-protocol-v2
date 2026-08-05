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
    for pcr, wins, losses in spec:
        out += [{"pcr": pcr, "outcome": "WIN"} for _ in range(wins)]
        out += [{"pcr": pcr, "outcome": "LOSS"} for _ in range(losses)]
    return out


def _directional_rows(direction, pcr, wins, losses, *, trade_date="2026-07-28"):
    common = {
        "direction": direction, "pcr": pcr, "trade_date": trade_date,
        "symbol": "STUB", "model_sha256": "a" * 64,
        "feature_schema": "feature-v1", "label_schema": "label-v1",
        "validation_schema": "validation-v1", "hold_bars": 5,
    }
    return ([{**common, "outcome": "WIN"} for _ in range(wins)]
            + [{**common, "outcome": "LOSS"} for _ in range(losses)])


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

    def test_directional_family_is_fixed_and_does_not_pool_directions(self):
        rows = _directional_rows("UP", 0.4, 90, 10)
        rows += _directional_rows("DOWN", 0.4, 10, 90)
        result = oe.summarize_directional_pcr_edge(rows)

        assert result["family_size"] == 10
        by_cell = {
            (cell["direction"], cell["pcr_bucket"]): cell
            for cell in result["cells"]
        }
        assert by_cell[("UP", "<0.5")]["win_rate"] == 0.9
        assert by_cell[("DOWN", "<0.5")]["win_rate"] == 0.1

    def test_directional_candidate_is_not_labeled_forward_proof(self):
        result = oe.summarize_directional_pcr_edge(
            _directional_rows("UP", 0.4, 190, 10)
        )

        assert result["status"] == "QUALIFIED_DISCOVERY_CANDIDATE"
        assert "forward" in result["note"]
        assert "proof" in result["note"]

    def test_mixed_model_generations_cannot_qualify(self):
        rows = _directional_rows("UP", 0.4, 190, 10)
        for row in rows[100:]:
            row["model_sha256"] = "b" * 64

        result = oe.summarize_directional_pcr_edge(rows)
        cell = next(
            item for item in result["cells"]
            if item["direction"] == "UP" and item["pcr_bucket"] == "<0.5"
        )

        assert cell["family_wilson_low"] >= 0.70
        assert cell["distinct_model_generations"] == 2
        assert cell["identity_homogeneous"] is False
        assert cell["qualified_discovery_candidate"] is False
        assert result["status"] == "NO_QUALIFIED_DIRECTIONAL_CELL"


class TestLiveWrappers:
    def test_loader_excludes_snapshots_recorded_after_evaluation(self, monkeypatch):
        executed = {}

        class _Cursor:
            def execute(self, sql, params=None):
                executed["sql"] = sql
                executed["params"] = params

            def fetchall(self):
                return []

        class _Connection:
            def cursor(self):
                return _Cursor()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr("core.db.db_conn", lambda: _Connection())

        assert oe.load_paired_rows() == []
        assert "ts <= s.eval_ts" in executed["sql"]
        assert "JOIN LATERAL" in executed["sql"]
        assert "EXTRACT(ISODOW" in executed["sql"]
        assert "s.model_sha256 IS NOT NULL" in executed["sql"]
        assert executed["params"][0] == oe.MAX_SNAPSHOT_AGE_S

    def test_edge_insufficient_when_thin(self, monkeypatch):
        monkeypatch.setattr(oe, "load_paired_rows",
                            lambda days=60, limit=50000: _rows([(0.4, 5, 5)]))
        monkeypatch.setattr(oe, "options_pcr_readiness",
                            lambda days=60: {"paired_distinct_days": 1,
                                             "paired_with_outcomes": 10})
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
