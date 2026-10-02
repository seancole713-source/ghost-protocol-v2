"""Tests for core/squeeze_hunter_ledger.py — point-in-time audit trail."""
from datetime import datetime, timezone

from core import squeeze_hunter_ledger as hl


def _noon_utc(day_offset: int) -> int:
    """A noon-UTC timestamp `day_offset` days after 2026-08-01 (noon UTC).

    Noon UTC = 06:00 CT, safely inside a single exchange session day, so each
    +1 day lands on a distinct session date regardless of provider convention.
    """
    base = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    return int((base.timestamp()) + day_offset * 86400)


def _series(n: int) -> list:
    """n daily bars, one per session day, starting the day after day 0."""
    return [
        {"ts": _noon_utc(i + 1), "open": 100, "high": 100 + (i + 1),
         "low": 90, "close": 100 + (i + 1)}
        for i in range(n)
    ]


class _FakeCursor:
    """Minimal cursor that records executed SQL and returns canned rows."""

    def __init__(self, fetchone_result=None, fetchall_result=None):
        self._fetchone = fetchone_result
        self._fetchall = fetchall_result or []
        self.executed = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall


def test_log_hunter_evaluation_inserts(monkeypatch):
    cur = _FakeCursor(fetchone_result=(1,))
    rid = hl.log_hunter_evaluation(
        symbol="HTZ",
        report={"fuel_score": 70.0, "trigger_score": 80.0, "confirmation_score": 60.0,
                "squeeze_pressure_score": 72.0, "pressure_band": "high", "stage": "squeeze",
                "explosion_score": 88.0, "factors": {}, "projection": {},
                "planning_levels": {"status": "available", "entry_price": 100.0,
                                    "target_price": 102.0, "stop_price": 98.7}},
        short_ctx={"short_float_pct": 34.0},
        trigger_ctx={"catalyst_score": 90.0},
        confirm_ctx={"breakout_pct": 5.0},
        reference_price=100.0,
        reference_price_ts=1000,
        session_date="1970-01-01",
        issued_ts=1000,
        cur=cur,
    )
    assert rid == 1
    assert any("INSERT INTO ghost_squeeze_hunter_evaluations" in s for s in cur.executed)
    insert_sql = next(s for s in cur.executed if "INSERT INTO ghost_squeeze_hunter_evaluations" in s)
    assert "planning_levels" in insert_sql


def test_log_hunter_evaluation_skips_missing_reference(monkeypatch):
    """P0: a sample with no reference price must NOT be persisted."""
    cur = _FakeCursor(fetchone_result=(1,))
    rid = hl.log_hunter_evaluation(
        symbol="HTZ",
        report={"fuel_score": 70.0},
        reference_price=None,
        issued_ts=1000,
        cur=cur,
    )
    assert rid is None
    assert not any("INSERT INTO ghost_squeeze_hunter_evaluations" in s for s in cur.executed)


def test_log_hunter_evaluation_idempotent(monkeypatch):
    # ON CONFLICT DO NOTHING returns no row → None (idempotent, no duplicate).
    cur = _FakeCursor(fetchone_result=None)
    rid = hl.log_hunter_evaluation(
        symbol="HTZ",
        report={"fuel_score": 70.0},
        reference_price=100.0,
        reference_price_ts=1000,
        session_date="1970-01-01",
        issued_ts=1000,
        cur=cur,
    )
    assert rid is None


def test_persist_hunter_evaluation_distinguishes_database_failure(monkeypatch):
    class Boom:
        def execute(self, sql, params=None):
            raise RuntimeError("db down")

    out = hl.persist_hunter_evaluation(
        symbol="HTZ",
        report={"fuel_score": 1.0},
        reference_price=100.0,
        reference_price_ts=1000,
        session_date="1970-01-01",
        issued_ts=1000,
        cur=Boom(),
    )
    assert out == {"status": "database_unavailable", "evaluation_id": None}


def test_persist_hunter_evaluation_rejects_missing_timestamp():
    out = hl.persist_hunter_evaluation(
        symbol="HTZ", report={}, reference_price=100.0,
        reference_price_ts=None, session_date="1970-01-01",
    )
    assert out["status"] == "invalid_reference"


def test_log_hunter_evaluation_never_raises(monkeypatch):
    # A cursor that raises on execute must not propagate.
    class Boom:
        def execute(self, sql, params=None):
            raise RuntimeError("db down")

    rid = hl.log_hunter_evaluation(symbol="HTZ", report={"fuel_score": 1.0},
                                   reference_price=100.0, reference_price_ts=1000,
                                   session_date="1970-01-01", cur=Boom())
    assert rid is None


def test_resolve_hunter_evaluation_inserts(monkeypatch):
    cur = _FakeCursor()
    cur.rowcount = 1
    inserted = hl.resolve_hunter_evaluation(
        evaluation_id=1,
        return_14d_pct=25.0,
        hit_plus_20=True,
        hit_minus_20=False,
        cur=cur,
    )
    assert inserted is True
    assert any("INSERT INTO ghost_squeeze_hunter_resolutions" in s for s in cur.executed)


def test_resolve_hunter_evaluation_idempotent(monkeypatch):
    cur = _FakeCursor()
    cur.rowcount = 0  # ON CONFLICT DO NOTHING → no row inserted
    inserted = hl.resolve_hunter_evaluation(evaluation_id=1, cur=cur)
    assert inserted is False


def test_scoring_version_is_stable():
    assert hl.HUNTER_SCORING_VERSION == "1"
    assert hl.HUNTER_HORIZONS == (1, 5, 14)


def test_ensure_hunter_tables_runs_migrations():
    """P0: ensure_hunter_tables must ALTER existing tables, not just CREATE."""
    cur = _FakeCursor()
    hl.ensure_hunter_tables(cur)
    sqls = " ".join(cur.executed)
    assert "ALTER TABLE ghost_squeeze_hunter_evaluations ADD COLUMN IF NOT EXISTS reference_price" in sqls
    assert "ALTER TABLE ghost_squeeze_hunter_evaluations ADD COLUMN IF NOT EXISTS planning_levels JSONB" in sqls
    assert "ALTER TABLE ghost_squeeze_hunter_resolutions ADD COLUMN IF NOT EXISTS hit_plus_50" in sqls
    assert "ALTER TABLE ghost_squeeze_hunter_resolutions ADD COLUMN IF NOT EXISTS hit_plus_100" in sqls
    assert "ALTER TABLE ghost_squeeze_hunter_resolutions ADD COLUMN IF NOT EXISTS max_favorable_pct" in sqls


def test_purge_invalid_hunter_samples():
    """P0: purge deletes invalid (no-reference) evaluations + their resolutions."""
    cur = _FakeCursor()
    cur.rowcount = 104
    n = hl.purge_invalid_hunter_samples(cur)
    assert n == 104
    sqls = " ".join(cur.executed)
    assert "DELETE FROM ghost_squeeze_hunter_resolutions" in sqls
    assert "DELETE FROM ghost_squeeze_hunter_evaluations" in sqls
    assert "reference_price IS NULL OR reference_price <= 0" in sqls
    assert "reference_price_ts IS NULL OR session_date IS NULL" in sqls


def test_enforce_hunter_constraints_sets_session_date_not_null():
    """P0: session_date must be enforced NOT NULL after invalid rows are purged."""
    cur = _FakeCursor()
    hl.enforce_hunter_constraints(cur)
    sqls = " ".join(cur.executed)
    assert (
        "ALTER TABLE ghost_squeeze_hunter_evaluations "
        "ALTER COLUMN session_date SET NOT NULL" in sqls
    )


def test_resolve_one_computes_returns():
    """Pure resolution: 1/5/14-day returns + hit thresholds + excursions."""
    series = _series(20)
    # ref = 100; day 1 close = 101 (+1%), day 5 close = 105 (+5%), day 14 close = 114 (+14%).
    out = hl._resolve_one(1, "HTZ", _noon_utc(0), 100.0, series, now=_noon_utc(20))
    assert out["return_1d_pct"] == 1.0
    assert out["return_5d_pct"] == 5.0
    assert out["return_14d_pct"] == 14.0
    # No bar reached +20% (max high in 14-bar window = 100+14 = 114, < 120).
    assert out["hit_plus_20"] is False
    assert out["hit_plus_50"] is False
    assert out["hit_plus_100"] is False
    assert out["hit_minus_20"] is False
    assert out["max_favorable_pct"] == 14.0
    assert out["max_adverse_pct"] == -10.0


def test_resolve_one_hit_plus_20():
    series = [
        {"ts": _noon_utc(i + 1), "open": 100, "high": 100 + i * 10, "low": 90, "close": 100}
        for i in range(20)
    ]
    out = hl._resolve_one(1, "HTZ", _noon_utc(0), 100.0, series, now=_noon_utc(20))
    assert out["hit_plus_20"] is True  # high reaches 100+190 = 290 > 120
    assert out["hit_plus_50"] is True
    assert out["hit_plus_100"] is True


def test_resolve_one_waits_for_full_window():
    """P1: a partial window must NOT produce a resolution (no premature labels)."""
    series = _series(3)  # only 3 forward bars
    out = hl._resolve_one(1, "HTZ", _noon_utc(0), 100.0, series, now=_noon_utc(3))
    assert out is None


def test_resolve_one_no_reference():
    assert hl._resolve_one(1, "HTZ", _noon_utc(0), None, [], now=_noon_utc(20)) is None


def test_bars_after_uses_session_dates():
    """P1: day-zero/day-one alignment must be provider-independent."""
    # A bar timestamped 00:00 UTC the next day is still the SAME session date
    # as an eval at 22:00 UTC the prior day (both map to the same CT date).
    eval_ts = int(datetime(2026, 8, 1, 22, 0, 0, tzinfo=timezone.utc).timestamp())
    bar_ts = int(datetime(2026, 8, 2, 0, 0, 0, tzinfo=timezone.utc).timestamp())
    series = [{"ts": bar_ts, "open": 1, "high": 1, "low": 1, "close": 1}]
    out = hl._bars_after(series, eval_ts)
    # 22:00 UTC Aug 1 = 17:00 CT Aug 1; 00:00 UTC Aug 2 = 19:00 CT Aug 1 → same
    # session date, so this bar is NOT a forward bar.
    assert out == []


def test_issue_hunter_samples_skips_weekend(monkeypatch):
    """No samples issued on a weekend."""
    # 2026-08-01 is a Saturday.
    sat = int(datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp())
    out = hl.issue_hunter_samples(symbols=["HTZ"], now_ts=sat)
    assert out["attempted"] == 0
    assert out["session_date"] is None


def test_issue_hunter_samples_issues_on_trading_day(monkeypatch):
    """One evaluation per symbol per session date, with honest issued_ts."""
    calls = []
    monkeypatch.setattr(hl, "_existing_session_symbols", lambda session_date: set())
    monkeypatch.setattr(
        "core.squeeze_hunter.fetch_explosion_report",
        lambda sym, persist=False, issued_ts=None: calls.append((sym, persist, issued_ts)) or {
            "evaluation_id": 1,
            "reference_price": 100.0,
            "persistence": {"status": "inserted", "evaluation_id": 1},
        },
    )
    # 2026-08-03 is a Monday (trading day). 20:05 UTC = 15:05 CT (post-close,
    # inside the frozen sampling window).
    mon = int(datetime(2026, 8, 3, 20, 5, 0, tzinfo=timezone.utc).timestamp())
    out = hl.issue_hunter_samples(symbols=["HTZ", "WOLF"], now_ts=mon)
    assert out["attempted"] == 2
    assert out["inserted"] == 2
    assert out["session_date"] == "2026-08-03"
    # Both calls used persist=True and no frozen batch timestamp: each symbol's
    # issued_ts is its own decision time, taken after its evidence fetch.
    assert all(c[1] is True for c in calls)
    assert all(c[2] is None for c in calls)


def test_issue_hunter_samples_skips_outside_window(monkeypatch):
    """P1: no samples issued outside the frozen 15:05-16:00 CT window."""
    calls = []
    monkeypatch.setattr(
        "core.squeeze_hunter.fetch_explosion_report",
        lambda sym, persist=False, issued_ts=None: calls.append((sym, persist, issued_ts)),
    )
    # 2026-08-03 noon UTC = 06:00 CT (premarket) — outside the window.
    mon = int(datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc).timestamp())
    out = hl.issue_hunter_samples(symbols=["HTZ"], now_ts=mon)
    assert out["attempted"] == 0
    assert out["inserted"] == 0
    assert "sampling window" in out["note"]


def test_issue_hunter_samples_counts_invalid_reference(monkeypatch):
    """P2: a missing reference price is counted as invalid_reference, not inserted."""
    monkeypatch.setattr(hl, "_existing_session_symbols", lambda session_date: set())
    monkeypatch.setattr(
        "core.squeeze_hunter.fetch_explosion_report",
        lambda sym, persist=False, issued_ts=None: {
            "evaluation_id": None,
            "reference_price": None,
            "persistence": {"status": "invalid_reference", "evaluation_id": None},
        },
    )
    mon = int(datetime(2026, 8, 3, 20, 5, 0, tzinfo=timezone.utc).timestamp())
    out = hl.issue_hunter_samples(symbols=["HTZ"], now_ts=mon)
    assert out["attempted"] == 1
    assert out["inserted"] == 0
    assert out["invalid_reference"] == 1


def test_issue_hunter_samples_skips_already_persisted_before_fetch(monkeypatch):
    calls = []
    monkeypatch.setattr(hl, "_existing_session_symbols", lambda session_date: {"HTZ"})
    monkeypatch.setattr(
        "core.squeeze_hunter.fetch_explosion_report",
        lambda sym, persist=False, issued_ts=None: calls.append(sym),
    )
    mon = int(datetime(2026, 8, 3, 20, 5, 0, tzinfo=timezone.utc).timestamp())

    out = hl.issue_hunter_samples(symbols=["HTZ"], now_ts=mon)

    assert out["requested"] == 1
    assert out["attempted"] == 0
    assert out["duplicate"] == 1
    assert calls == []


# ── Hunter issuance timeline: evidence must never postdate issued_ts ───────

def test_persist_rejects_reference_observed_after_issuance():
    """Prod Sept 2: reference ts +6..+47s after recorded issued_ts."""
    cur = _FakeCursor(fetchone_result=(1,))
    out = hl.persist_hunter_evaluation(
        symbol="MU", report={}, reference_price=100.0,
        reference_price_ts=1_047, session_date="2026-09-02",
        issued_ts=1_000, cur=cur,
    )
    assert out == {"status": "future_evidence", "evaluation_id": None}
    assert not any("INSERT" in sql for sql in cur.executed)


def test_persist_rejects_feature_observed_after_issuance():
    cur = _FakeCursor(fetchone_result=(1,))
    out = hl.persist_hunter_evaluation(
        symbol="MU", report={}, reference_price=100.0,
        reference_price_ts=990, feature_available_ts=1_006,
        session_date="2026-09-02", issued_ts=1_000, cur=cur,
    )
    assert out["status"] == "future_evidence"


def test_persist_accepts_evidence_at_or_before_issuance():
    cur = _FakeCursor(fetchone_result=(7,))
    out = hl.persist_hunter_evaluation(
        symbol="MU", report={}, reference_price=100.0,
        reference_price_ts=1_000, session_date="2026-09-02",
        issued_ts=1_000, cur=cur,
    )
    assert out == {"status": "inserted", "evaluation_id": 7}


def test_issue_hunter_samples_counts_future_evidence(monkeypatch):
    monkeypatch.setattr(hl, "_existing_session_symbols", lambda session_date: set())
    monkeypatch.setattr(
        "core.squeeze_hunter.fetch_explosion_report",
        lambda sym, persist=False, issued_ts=None: {
            "persistence": {"status": "future_evidence", "evaluation_id": None},
        },
    )
    mon = int(datetime(2026, 8, 3, 20, 5, 0, tzinfo=timezone.utc).timestamp())
    out = hl.issue_hunter_samples(symbols=["HTZ"], now_ts=mon)
    assert out["future_evidence"] == 1
    assert out["inserted"] == 0
    assert out["persistence_failed"] == 0


def test_sampler_records_decision_time_after_each_symbols_evidence(monkeypatch):
    """Slow per-symbol fetches must not leave evidence after issued_ts."""
    import core.catalyst_scoring as cs
    import core.earnings_surprise as es
    import core.options_flow as of
    import core.prices as prices
    import core.squeeze_hunter as sh
    import core.squeeze_hunter_ledger as ledger
    import core.squeeze_monitor as sm

    clock = {"t": 1_788_379_500}  # 2026-09-02 ~15:05 CT

    def _tick(seconds=1):
        clock["t"] += seconds
        return clock["t"]

    monkeypatch.setattr(sh.time, "time", lambda: clock["t"])
    monkeypatch.setattr(sm, "_short_context", lambda s: _tick(5) and {}, raising=False)
    monkeypatch.setattr(sm, "get_squeeze_picks", lambda: {"picks": []}, raising=False)
    monkeypatch.setattr(cs, "fetch_event_context", lambda s, **k: {"available": False})
    monkeypatch.setattr(es, "earnings_surprise_to_trigger", lambda s: {}, raising=False)
    monkeypatch.setattr(of, "probe_options_flow", lambda s: {}, raising=False)
    monkeypatch.setattr(sh, "_fetch_market_regime", lambda: None)

    def _session(sym):
        observed = _tick(20)  # quote observed after a slow fetch
        _tick(3)
        return {"price": 50.0, "previous_close": 49.0, "price_as_of_ts": observed,
                "session": "afterhours", "market_date": "2026-09-02"}

    monkeypatch.setattr(prices, "get_intraday_session", _session)
    persisted = []

    def _persist(**kwargs):
        persisted.append(kwargs)
        return {"status": "inserted", "evaluation_id": len(persisted)}

    monkeypatch.setattr(ledger, "persist_hunter_evaluation", _persist)
    monkeypatch.setattr(ledger, "_existing_session_symbols", lambda d: set())
    monkeypatch.setattr(ledger, "_in_sampling_window", lambda ts=None: True)
    monkeypatch.setattr(ledger, "_session_date_key", lambda ts=None: "2026-09-02")

    out = ledger.issue_hunter_samples(symbols=["AVGO", "PLUG", "MU", "PLTR"])

    assert out["inserted"] == 4
    assert len(persisted) == 4
    for row in persisted:
        assert row["reference_price_ts"] is not None
        assert row["reference_price_ts"] <= row["issued_ts"]
        assert row["feature_available_ts"] <= row["issued_ts"]
    issued = [row["issued_ts"] for row in persisted]
    assert issued == sorted(issued) and len(set(issued)) == 4


def test_measurement_reads_exclude_future_evidence_rows(monkeypatch):
    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append(" ".join(sql.split()))

        def fetchall(self):
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("core.db.db_conn", lambda: _Ctx())
    assert hl.recent_evaluations()["ok"] is True
    assert hl.recent_evaluations(symbol="MU")["ok"] is True
    hl.resolve_hunter_predictions(now=2_000_000_000)
    reads = [sql for sql in executed if "FROM ghost_squeeze_hunter_evaluations" in sql]
    assert len(reads) == 3
    for sql in reads:
        assert "COALESCE(e.reference_price_ts, e.issued_ts) <= e.issued_ts" in sql
        assert "COALESCE(e.feature_available_ts, e.issued_ts) <= e.issued_ts" in sql
