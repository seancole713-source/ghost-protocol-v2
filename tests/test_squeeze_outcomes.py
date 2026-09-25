"""Tests for squeeze daily log (core.squeeze_outcomes) — pure helpers, no live DB."""

from core.squeeze_outcomes import (
    _parse_bar_date,
    _resolve_row,
    record_squeeze_prediction,
    squeeze_daily_log,
    squeeze_log_enabled,
)


def test_squeeze_log_enabled_default_on(monkeypatch):
    monkeypatch.delenv("SQUEEZE_DAILY_LOG", raising=False)
    assert squeeze_log_enabled() is True


def test_squeeze_log_disabled(monkeypatch):
    monkeypatch.setenv("SQUEEZE_DAILY_LOG", "0")
    assert squeeze_log_enabled() is False
    assert record_squeeze_prediction({"symbol": "HOOD", "buy": 1, "sell": 2}) is None
    out = squeeze_daily_log()
    assert out["enabled"] is False


def test_squeeze_outcome_rejects_external_advisory_before_db(monkeypatch):
    import core.db as db

    monkeypatch.setattr(
        db, "db_conn",
        lambda: (_ for _ in ()).throw(AssertionError("database should not be touched")),
    )
    assert record_squeeze_prediction({
        "symbol": "ARCT", "buy": 20, "sell": 22,
        "advisory_only": True, "decision_eligible": False,
    }) is None


def test_squeeze_daily_log_read_path_issues_select_only(monkeypatch):
    import core.db as db

    class _Cursor:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append(sql.strip())

        def fetchall(self):
            return []

    class _Connection:
        def __init__(self, cursor):
            self._cursor = cursor

        def cursor(self):
            return self._cursor

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    cursor = _Cursor()
    monkeypatch.setattr(db, "db_conn", lambda: _Connection(cursor))

    out = squeeze_daily_log(session_date="2026-03-16")

    assert out["ok"] is True
    assert cursor.statements
    assert all(statement.upper().startswith("SELECT") for statement in cursor.statements)


def test_parse_bar_date_iso_to_et_session():
    assert _parse_bar_date("2026-06-10T20:00:00Z") == "2026-06-10"


def test_resolve_row_win_when_high_reaches_sell():
    meta = _resolve_row(10.0, 12.0, 8.0, {"open": 10.0, "high": 12.5, "low": 9.8, "close": 11.2})
    assert meta["outcome"] == "WIN"
    assert meta["hit_target"] is True
    assert meta["hit_stop"] is False
    assert meta["session_close"] == 11.2
    assert meta["close_pnl_pct"] == 12.0
    assert meta["target_gap_pct"] == round((11.2 - 12.0) / 12.0 * 100, 3)
    assert meta["precision_score"] is not None
    assert meta["precision_grade"] in {"A", "B", "C", "D", "F"}
    assert meta["precision"]["target_stop_result"] == "WIN"


def test_resolve_row_loss_when_low_hits_stop():
    meta = _resolve_row(10.0, 12.0, 9.5, {"open": 10.0, "high": 10.4, "low": 9.4, "close": 9.7})
    assert meta["outcome"] == "LOSS"
    assert meta["hit_stop"] is True
    assert meta["hit_target"] is False


def test_resolve_row_mixed_when_both_hit():
    meta = _resolve_row(10.0, 11.0, 9.0, {"open": 10.0, "high": 11.5, "low": 8.8, "close": 10.2})
    assert meta["outcome"] == "MIXED"
    assert meta["hit_target"] is True
    assert meta["hit_stop"] is True


def test_resolve_row_neutral_when_neither_hit():
    meta = _resolve_row(10.0, 12.0, 8.0, {"open": 10.0, "high": 10.2, "low": 9.2, "close": 10.1})
    assert meta["outcome"] == "NEUTRAL"
    assert meta["hit_3pct"] is False


def test_resolve_row_hit_3pct_flag():
    meta = _resolve_row(10.0, 15.0, 8.0, {"open": 10.0, "high": 10.31, "low": 9.9, "close": 10.2})
    assert meta["hit_3pct"] is True
    assert meta["outcome"] == "NEUTRAL"


def test_resolve_row_precision_marks_direction_win_but_target_too_low():
    meta = _resolve_row(11.41, 11.87, 10.27, {"open": 11.35, "high": 12.61, "low": 11.35, "close": 12.30})
    assert meta["outcome"] == "WIN"
    assert meta["precision_score"] < 75
    assert meta["mistake_type"] in {"target_too_low", "stop_too_wide", "direction_right_low_precision"}


def test_post_alert_ohlc_excludes_pre_alert_bars(monkeypatch):
    """Forensic SQ-3: grading must use only bars at-or-after the alert time,
    so a pre-alert spike-and-fade high can't auto-grade as a WIN."""
    import core.squeeze_outcomes as so

    # Bars: 09:30 (pre-alert, high 12.5) and 10:00 (post-alert, high 10.4).
    bars = [
        {"ts": "2026-06-10T14:30:00Z", "open": 10.0, "high": 12.5, "low": 9.8, "close": 11.0},
        {"ts": "2026-06-10T15:00:00Z", "open": 10.2, "high": 10.4, "low": 9.9, "close": 10.1},
    ]
    monkeypatch.setattr("core.signal_engine._fetch_ohlcv", lambda *a, **k: bars)
    # Alert at 14:45 UTC (09:45 CT) — after the first bar, before the second.
    alerted_at = 1781102700  # 2026-06-10T14:45:00Z
    ohlc = so._post_alert_ohlc("WOLF", "2026-06-10", alerted_at)
    assert ohlc is not None
    assert ohlc["high"] == 10.4  # pre-alert 12.5 high is excluded
    assert ohlc["open"] == 10.2


# --- F37 (audit 2026-09-25): explicit grading basis, no silent full-day grade ---


def test_missing_intraday_bars_mark_row_unresolved_not_win(monkeypatch):
    """Acceptance: with no 5-minute bars the row gets basis full_day_approx
    and is not counted as a win, even though the full-day high hit sell."""
    import core.squeeze_outcomes as so

    monkeypatch.setattr(so, "_post_alert_ohlc", lambda *a, **k: None)
    monkeypatch.setattr(
        so, "_session_ohlc",
        lambda *a, **k: {"open": 10.0, "high": 12.5, "low": 9.8, "close": 10.1},
    )
    meta = so.grade_squeeze_row("WOLF", "2026-06-10", 1781102700, 10.0, 11.0, 9.5)
    assert meta["outcome"] == "UNRESOLVED"
    assert meta["grading_basis"] == "full_day_approx"
    assert meta["hit_target"] is None and meta["hit_stop"] is None
    assert meta["precision_score"] is None
    assert meta["session_high"] == 12.5  # kept for display only
    assert so.is_graded_row(meta) is False


def test_post_alert_grade_is_labelled_and_reports_mfe(monkeypatch):
    import core.squeeze_outcomes as so

    monkeypatch.setattr(
        so, "_post_alert_ohlc",
        lambda *a, **k: {"open": 10.2, "high": 10.4, "low": 9.9, "close": 10.1},
    )
    monkeypatch.setattr(
        so, "_session_ohlc", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fallback")),
    )
    meta = so.grade_squeeze_row("WOLF", "2026-06-10", 1781102700, 10.0, 11.0, 9.5)
    assert meta["grading_basis"] == "post_alert_5m"
    assert meta["outcome"] == "NEUTRAL"
    assert meta["post_alert_mfe_pct"] == 4.0
    assert so.is_graded_row(meta) is True


def test_is_graded_row_excludes_full_day_basis_and_unresolved():
    from core.squeeze_outcomes import is_graded_row

    assert is_graded_row({"outcome": "WIN", "grading_basis": "post_alert_5m"}) is True
    assert is_graded_row({"outcome": "LOSS", "grading_basis": None}) is True  # legacy row
    assert is_graded_row({"outcome": "WIN", "grading_basis": "full_day_approx"}) is False
    assert is_graded_row({"outcome": "UNRESOLVED", "grading_basis": "full_day_approx"}) is False
    assert is_graded_row({"outcome": None}) is False


class _RecordingCursor:
    def __init__(self, rows=None):
        self.statements = []
        self._rows = rows or []

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return None


class _RecordingConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_resolve_writes_basis_and_does_not_count_unresolved(monkeypatch):
    import core.db as db
    import core.squeeze_outcomes as so

    cursor = _RecordingCursor(rows=[(7, "WOLF", 10.0, 11.0, 9.5, 1781102700)])
    monkeypatch.setattr(db, "db_conn", lambda: _RecordingConnection(cursor))
    monkeypatch.setattr(so, "_post_alert_ohlc", lambda *a, **k: None)
    monkeypatch.setattr(
        so, "_session_ohlc",
        lambda *a, **k: {"open": 10.0, "high": 12.5, "low": 9.8, "close": 10.1},
    )
    assert so.resolve_squeeze_outcomes("2026-06-10") == 0  # nothing GRADED
    select = next(sql for sql, _ in cursor.statements if sql.startswith("SELECT id, symbol"))
    assert "outcome = 'UNRESOLVED' AND grading_basis = 'full_day_approx'" in select
    sql, params = next((s, p) for s, p in cursor.statements if s.startswith("UPDATE ghost_squeeze_outcomes"))
    assert "grading_basis = %s" in sql
    assert params[0] == "UNRESOLVED"
    assert "full_day_approx" in params
    assert params[-1] == 7


def test_daily_log_summary_excludes_unresolved_from_wins(monkeypatch):
    import core.db as db
    from core.squeeze_outcomes import squeeze_daily_log

    def row(rid, outcome, basis):
        base = [rid, "2026-06-10", "WOLF", "squeeze_active", "telegram", 1781102700 + rid,
                10.0, 11.0, 9.5, 70, 0.4, 76, 3.0, 9.8,
                outcome, 10.0, 12.5, 9.8, 10.1, None, None, None, 1.0, -8.2,
                None, None, None, None, 1781130000, basis, None, 3.6]
        return tuple(base)

    rows = [
        row(1, "WIN", "post_alert_5m"),
        row(2, "UNRESOLVED", "full_day_approx"),
        row(3, "LOSS", "post_alert_5m"),
    ]
    # distinct buy/sell/stop so the candidate/telegram dedupe keeps all three
    rows = [r[:6] + (10.0 + i, 11.0 + i, 9.5 + i) + r[9:] for i, r in enumerate(rows)]
    cursor = _RecordingCursor(rows=rows)
    monkeypatch.setattr(db, "db_conn", lambda: _RecordingConnection(cursor))
    out = squeeze_daily_log(session_date="2026-06-10")
    day = out["days"][0]
    assert day["wins"] == 1 and day["losses"] == 1
    assert day["resolved"] == 2 and day["unresolved"] == 1
    unresolved = next(r for r in out["rows"] if r["id"] == 2)
    assert unresolved["grading_basis"] == "full_day_approx"
    assert unresolved.get("precision") in (None, {})  # no grade backfilled from full-day
    assert unresolved["alert_time_fade_pct"] == 3.6


def test_loss_streak_ignores_unresolved_rows(monkeypatch):
    """An UNRESOLVED (full-day) row must neither extend nor break the streak."""
    import core.db as db
    from core import squeeze_monitor as sm

    cursor = _RecordingCursor(rows=[("LOSS",), ("LOSS",), ("LOSS",)])
    monkeypatch.setattr(db, "db_conn", lambda: _RecordingConnection(cursor))
    assert sm._symbol_loss_streak("wolf") == 3
    sql, params = cursor.statements[-1]
    assert "outcome IN ('WIN','LOSS','MIXED','NEUTRAL')" in sql
    assert "IS NOT NULL" not in sql
    assert params == ("WOLF",)

