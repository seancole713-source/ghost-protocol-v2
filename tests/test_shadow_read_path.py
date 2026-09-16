"""Exercise the real DB reader, not just pure scoreboard aggregators."""
from contextlib import contextmanager

import core.shadow_outcomes as shadow


def test_reader_imports_connection_and_does_not_run_ddl(monkeypatch):
    calls = []

    class Cursor:
        def execute(self, sql, params):
            assert sql.lstrip().startswith("SELECT")
            calls.append((sql, params))

        def fetchall(self):
            return [
                ("AAA", 100, .2, "WIN", 2, "DOWN", .8, "sha", "label", "validation", 5),
                ("BBB", 200, .7, None, None, "UP", .7, "sha2", "label", "validation", 5),
            ]

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def connect():
        yield Connection()

    monkeypatch.setattr("core.db.db_conn", connect)
    monkeypatch.setattr(shadow.time, "time", lambda: 40_000_000)
    rows = shadow.load_shadow_rows(days=999)
    assert len(calls) == 1
    assert calls[0][1] == (40_000_000 - 365 * 86400,)
    assert rows[0]["up_prob"] == .8
    assert rows[1]["up_prob"] == .7
    assert rows[1]["outcome"] is None


def test_confidence_report_uses_real_reader(monkeypatch):
    from core.confidence_calibration import calibrated_confidence_report

    @contextmanager
    def connect():
        class Cursor:
            def execute(self, sql, params):
                assert sql.lstrip().startswith("SELECT")

            def fetchall(self):
                return [("AAA", 100, .7, "WIN", 2, "UP", .7, "sha", "l", "v", 5)]

        class Connection:
            def cursor(self):
                return Cursor()

        yield Connection()

    monkeypatch.setattr("core.db.db_conn", connect)
    report = calibrated_confidence_report()
    assert report["lanes"]["UP"]["total_samples"] == 1
