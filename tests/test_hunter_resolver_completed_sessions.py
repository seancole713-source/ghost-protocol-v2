"""F03: the Hunter resolver may only finalize outcomes from COMPLETED sessions.

The resolution insert is append-only (ON CONFLICT DO NOTHING), so a 14-day
outcome read from a still-trading day-14 bar would be frozen forever. These
tests pin the completed-exchange-session contract, half-day closes, the
publication delay, session completeness/order validation, and the read-time
resolver-generation cohort filter.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core import squeeze_hunter_ledger as hl
from core.daily_bar_contract import next_session, previous_session

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")
DELAY = hl.HUNTER_RESOLUTION_PUBLICATION_DELAY_MIN


def _ts(day: date, hh: int, mm: int = 0, tz=CT) -> int:
    return int(datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz).timestamp())


def _sessions_after(day0: date, n: int) -> list:
    out, day = [], day0
    for _ in range(n):
        day = next_session(day)
        out.append(day)
    return out


def _label(day: date) -> str:
    return f"{day.isoformat()}T00:00:00Z"


def _bars(days, close=100.0, high=101.0, low=99.0) -> list:
    return [{"ts": _label(d), "open": 100.0, "high": high, "low": low, "close": close} for d in days]


DAY0 = date(2026, 9, 14)  # Monday
SESSIONS = _sessions_after(DAY0, 14)
DAY14 = SESSIONS[-1]
ISSUED = _ts(DAY0, 15, 30)  # inside the frozen post-close sampling window


def test_window_skips_holidays_and_weekends():
    # No exchange holiday falls in this window; 14 sessions == 14 weekdays.
    assert all(d.weekday() < 5 for d in SESSIONS)
    assert DAY14 == date(2026, 10, 2)


def test_day14_before_close_is_not_resolved_repro():
    """Repro: 10:00 ET on day 14 the partial bar shows +2% and no +20% touch."""
    partial = _bars(SESSIONS[:-1]) + [
        {"ts": _label(DAY14), "open": 100.0, "high": 102.0, "low": 99.0, "close": 102.0}
    ]
    now = _ts(DAY14, 10, 0, tz=ET)
    assert hl._forward_window(partial, ISSUED, now)["status"] == "pending"
    assert hl._resolve_one(1, "HTZ", ISSUED, 100.0, partial, now) is None

    # After the session has closed and published, the completed bar decides.
    final = _bars(SESSIONS[:-1]) + [
        {"ts": _label(DAY14), "open": 100.0, "high": 121.0, "low": 99.0, "close": 120.0}
    ]
    out = hl._resolve_one(1, "HTZ", ISSUED, 100.0, final, _ts(DAY14 + timedelta(days=1), 8))
    assert out is not None
    assert out["return_14d_pct"] == 20.0
    assert out["hit_plus_20"] is True
    assert out["resolver_version"] == hl.HUNTER_RESOLVER_VERSION


def test_at_close_waits_for_publication_delay():
    series = _bars(SESSIONS)
    close = _ts(DAY14, 15, 0)
    assert hl._resolve_one(1, "HTZ", ISSUED, 100.0, series, close) is None
    assert hl._resolve_one(1, "HTZ", ISSUED, 100.0, series, close + (DELAY - 1) * 60) is None
    assert hl._resolve_one(1, "HTZ", ISSUED, 100.0, series, close + DELAY * 60) is not None


def test_after_close_resolves_with_completed_values():
    series = _bars(SESSIONS, close=105.0, high=110.0, low=95.0)
    out = hl._resolve_one(1, "HTZ", ISSUED, 100.0, series, _ts(DAY14, 20, 0))
    assert out["return_1d_pct"] == 5.0
    assert out["return_14d_pct"] == 5.0
    assert out["max_favorable_pct"] == 10.0
    assert out["max_adverse_pct"] == -5.0
    assert out["hit_plus_20"] is False


def test_early_close_day_uses_half_day_close():
    half = date(2026, 11, 27)  # day after Thanksgiving: 12:00 CT close
    day0 = half
    for _ in range(14):
        day0 = previous_session(day0)
    sessions = _sessions_after(day0, 14)
    assert sessions[-1] == half
    assert date(2026, 11, 26) not in sessions  # Thanksgiving is not a session
    series = _bars(sessions)
    issued = _ts(day0, 15, 30)
    # Before the early close: still trading.
    assert hl._resolve_one(1, "X", issued, 100.0, series, _ts(half, 11, 30)) is None
    # Closed, but within the publication delay.
    assert hl._resolve_one(1, "X", issued, 100.0, series, _ts(half, 12, 0) + (DELAY - 1) * 60) is None
    # Early close + delay: final, even though a regular day would still be open.
    assert hl._resolve_one(1, "X", issued, 100.0, series, _ts(half, 12, 0) + DELAY * 60) is not None


def test_missing_session_is_never_filled_by_a_later_bar():
    extra = next_session(DAY14)
    gappy = _bars([d for i, d in enumerate(SESSIONS) if i != 6] + [extra])
    now = _ts(extra, 20, 0)
    assert len(gappy) == 14  # the old counter would have resolved this
    assert hl._forward_window(gappy, ISSUED, now)["status"] == "incomplete"
    assert hl._resolve_one(1, "HTZ", ISSUED, 100.0, gappy, now) is None


def test_bar_order_does_not_matter_but_conflicting_duplicates_do():
    series = _bars(SESSIONS)
    now = _ts(DAY14, 20, 0)
    a = hl._resolve_one(1, "HTZ", ISSUED, 100.0, series, now)
    b = hl._resolve_one(1, "HTZ", ISSUED, 100.0, list(reversed(series)), now)
    assert a == b
    # An identical duplicate is harmless; a conflicting one is ambiguous.
    assert hl._resolve_one(1, "HTZ", ISSUED, 100.0, series + [dict(series[3])], now) == a
    bad = dict(series[3], close=150.0)
    assert hl._forward_window(series + [bad], ISSUED, now)["status"] == "ambiguous"
    assert hl._resolve_one(1, "HTZ", ISSUED, 100.0, series + [bad], now) is None


def test_epoch_and_label_timestamps_agree():
    labels = _bars(SESSIONS)
    epochs = [dict(b, ts=_ts(d, 7, 0)) for b, d in zip(labels, SESSIONS)]
    now = _ts(DAY14, 20, 0)
    assert hl._resolve_one(1, "HTZ", ISSUED, 100.0, labels, now) == \
        hl._resolve_one(1, "HTZ", ISSUED, 100.0, epochs, now)


class _RecordingCursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))


def test_resolve_one_row_incomplete_waits_then_goes_terminal():
    gappy = _bars([d for i, d in enumerate(SESSIONS) if i != 6])
    rec = {"id": 7, "issued_ts": ISSUED, "reference_price": 100.0}
    cur = _RecordingCursor()
    assert hl._resolve_one_row(cur, rec, "HTZ", gappy, _ts(DAY14, 20, 0)) == "skip"
    assert cur.calls == []
    late = hl._session_final_ts(DAY14) + hl.HUNTER_INCOMPLETE_HISTORY_GRACE_S
    assert hl._resolve_one_row(cur, rec, "HTZ", gappy, late) == "terminal"
    sql, params = cur.calls[-1]
    assert "INSERT INTO ghost_squeeze_hunter_resolutions" in sql
    assert "incomplete_session_history" in params
    assert params[3:10] == (None,) * 7  # no outcome is asserted
    assert hl.HUNTER_RESOLVER_VERSION in params


def test_resolution_rows_are_stamped_with_resolver_version():
    cur = _RecordingCursor()
    rec = {"id": 9, "issued_ts": ISSUED, "reference_price": 100.0}
    assert hl._resolve_one_row(cur, rec, "HTZ", _bars(SESSIONS), _ts(DAY14, 20, 0)) == "resolved"
    sql, params = cur.calls[-1]
    assert "resolver_version" in sql
    assert hl.HUNTER_RESOLVER_VERSION in params


def test_schema_adds_resolver_version_without_backfill():
    cur = _RecordingCursor()
    hl.ensure_hunter_tables(cur)
    sqls = [sql for sql, _ in cur.calls]
    assert any(
        "ALTER TABLE ghost_squeeze_hunter_resolutions ADD COLUMN IF NOT EXISTS resolver_version" in s
        for s in sqls
    )
    assert not any("UPDATE ghost_squeeze_hunter_resolutions" in s for s in sqls)


def test_old_resolution_excluded_from_corrected_cohort(monkeypatch):
    """A legacy (NULL-version) resolution is kept but not joined by measurement reads."""
    calls = []
    legacy = {"id": 1, "resolver_version": None, "return_14d_pct": 2.0}
    corrected = {"id": 2, "resolver_version": hl.HUNTER_RESOLVER_VERSION, "return_14d_pct": 20.0}

    class _Cur:
        def execute(self, sql, params=None):
            calls.append((" ".join(sql.split()), params))

        def fetchall(self):
            # Emulate the LEFT JOIN cohort filter against both stored rows.
            sql, params = calls[-1]
            version = params[0]
            out = []
            for row in (legacy, corrected):
                joined = row["resolver_version"] == version
                vals = [None] * 27
                vals[0] = row["id"]
                if joined:
                    vals[18] = row["return_14d_pct"]
                    vals[26] = row["resolver_version"]
                out.append(tuple(vals))
            return out

    class _Conn:
        def cursor(self):
            return _Cur()

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("core.db.db_conn", lambda: _Ctx())
    result = hl.recent_evaluations()
    assert result["resolution_cohort"] == hl.HUNTER_RESOLVER_VERSION
    sql, params = calls[-1]
    assert "r.resolver_version = %s" in sql
    assert params[0] == hl.HUNTER_RESOLVER_VERSION
    rows = {r["id"]: r for r in result["rows"]}
    assert rows[1]["return_14d_pct"] is None and rows[1]["resolver_version"] is None
    assert rows[2]["return_14d_pct"] == 20.0
    hl.recent_evaluations(symbol="HTZ")
    sql, params = calls[-1]
    assert "r.resolver_version = %s" in sql and params[0] == hl.HUNTER_RESOLVER_VERSION
