"""Regression tests for the 2026-09-25 audit tail (core model / misc U-findings).

Each test names its finding. None of these loosen a gate: they fix price-basis
and interval honesty in the OHLCV fallback chain (U56), the core holiday
calendar (U39), pagination (U47), and similar correctness defects.
"""
from __future__ import annotations

import datetime as _dt


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


# -- U56: OHLCV fallback chain keeps the caller's interval and price basis --

def _alpaca_empty_env(monkeypatch):
    import core.signal_engine as se

    monkeypatch.setenv("ALPACA_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setitem(se._SIP_FORBIDDEN, "until", 0)
    return se


def test_intraday_request_never_falls_back_to_daily_only_tiers(monkeypatch):
    se = _alpaca_empty_env(monkeypatch)
    monkeypatch.setattr("requests.get", lambda url, **kw: _Resp(200, {"bars": []}))
    called = []
    for name in ("_try_polygon_ohlcv", "_try_yfinance_ohlcv", "_try_stooq_ohlcv"):
        monkeypatch.setattr(se, name, lambda *a, _n=name, **k: called.append(_n) or [
            {"ts": "2026-09-15", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}])
    assert se._fetch_ohlcv_once("AAA", "stock", "5d", "5m") is None
    assert called == []


def test_daily_request_still_uses_fallback_tiers(monkeypatch):
    se = _alpaca_empty_env(monkeypatch)
    monkeypatch.setattr("requests.get", lambda url, **kw: _Resp(200, {"bars": []}))
    monkeypatch.setattr(se, "_try_polygon_ohlcv", lambda *a, **k: None)
    monkeypatch.setattr(se, "_try_yfinance_ohlcv", lambda s, p: [
        {"ts": "2026-09-15", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}])
    rows = se._fetch_ohlcv_once("AAA", "stock", "1y", "1d", adjustment="split")
    assert rows and rows[0]["close"] == 1


def test_polygon_fallback_honours_raw_price_basis(monkeypatch):
    se = _alpaca_empty_env(monkeypatch)
    monkeypatch.setenv("POLYGON_API_KEY", "pk")
    urls = []

    def fake_get(url, **kw):
        urls.append(url)
        if "polygon.io" in url:
            return _Resp(200, {"status": "OK", "results": [
                {"t": 1_730_400_000_000, "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 100}]})
        return _Resp(200, {"bars": []})

    monkeypatch.setattr("requests.get", fake_get)
    assert se._fetch_ohlcv_once("AAA", "stock", "1y", "1d")  # default basis: raw
    assert se._fetch_ohlcv_once("AAA", "stock", "1y", "1d", adjustment="split")
    poly = [u for u in urls if "polygon.io" in u]
    assert "adjusted=false" in poly[0]
    assert "adjusted=true" in poly[1]


def test_yfinance_fallback_requests_split_only_adjustment():
    import core.signal_engine as se

    seen = {}

    class _Tk:
        def history(self, **kwargs):
            seen.update(kwargs)
            return None

    assert se._yf_rows_from_history(_Tk(), period="1y") is None
    assert seen.get("auto_adjust") is False


# -- U39: the core holiday table covers 2027 and agrees with edge's --

def test_core_holiday_table_covers_2027_and_matches_edge():
    from core.market_hours import _NYSE_FULL_DAY_HOLIDAYS, _NYSE_HALF_DAYS, is_market_holiday
    from edge.calendar import BUILTIN_EARLY_CLOSE, BUILTIN_HOLIDAYS

    core_2027 = {d for d in _NYSE_FULL_DAY_HOLIDAYS if d.startswith("2027")}
    edge_2027 = {d for d in BUILTIN_HOLIDAYS if d.startswith("2027")}
    assert core_2027 == edge_2027 and len(core_2027) == 10
    assert {d for d in _NYSE_HALF_DAYS if d.startswith("2027")} == {
        d for d in BUILTIN_EARLY_CLOSE if d.startswith("2027")}
    assert is_market_holiday(_dt.date(2027, 1, 1))
    assert is_market_holiday(_dt.date(2027, 1, 18))
    assert not is_market_holiday(_dt.date(2027, 1, 19))


# -- U47: /api/picks has_more reflects the resolved rows it pages over --

def _picks_db(monkeypatch, resolved_rows, tally):
    import wolf_app

    class _Cur:
        description = [("id",), ("symbol",), ("outcome",), ("entry_price",)]

        def execute(self, sql, params=None):
            self._sql = sql
            self._params = params

        def fetchall(self):
            if "GROUP BY outcome" in self._sql:
                return tally
            if "outcome IS NOT NULL" in self._sql:
                lim, off = self._params[-2], self._params[-1]
                return resolved_rows[off:off + lim]
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _Ctx())
    return wolf_app


def test_picks_has_more_counts_expired_rows_not_just_wins_and_losses(monkeypatch):
    rows = [(i, "AAA", "EXPIRED", 1.0) for i in range(5)]  # no WIN/LOSS at all
    wolf_app = _picks_db(monkeypatch, rows, [])
    first = wolf_app.get_picks(limit=2, offset=0)
    assert len(first["recent"]) == 2 and first["has_more"] is True
    last = wolf_app.get_picks(limit=2, offset=4)
    assert len(last["recent"]) == 1 and last["has_more"] is False


# -- U48: health audit self-heal is opt-in; the history GET runs no DDL --

def _history_db(monkeypatch, table_exists, rows=()):
    import wolf_app

    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append(sql)
            self._sql = sql

        def fetchone(self):
            return ("health_audit_runs" if table_exists else None,)

        def fetchall(self):
            return list(rows)

    class _Conn:
        def cursor(self):
            return _Cur()

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(wolf_app, "db_conn", lambda: _Ctx())
    return wolf_app, executed


def test_health_audit_history_get_never_creates_tables(monkeypatch):
    wolf_app, executed = _history_db(monkeypatch, table_exists=False)
    assert wolf_app.health_audit_history() == {"ok": True, "runs": []}
    wolf_app, executed2 = _history_db(
        monkeypatch, table_exists=True, rows=[(1, 1_790_000_000, "PASS", 99.0, 0, 3)])
    out = wolf_app.health_audit_history(limit=5)
    assert out["runs"][0]["status"] == "PASS"
    assert not any("CREATE" in sql.upper() for sql in executed + executed2)


def test_health_audit_auto_fix_defaults_off():
    import inspect

    import wolf_app
    from core.health_audit import run_health_audit

    assert inspect.signature(wolf_app.health_audit).parameters["auto_fix"].default is False
    assert inspect.signature(run_health_audit).parameters["auto_fix"].default is False


# -- U08: agent workflow health degrades when every expected worker is offline --

def _workflow_db(monkeypatch, workers_row):
    import core.db as db

    class _Cur:
        def execute(self, sql, params=None):
            self._sql = sql

        def fetchall(self):
            return []

        def fetchone(self):
            if "FROM ghost_agent_workers" in self._sql:
                return workers_row
            return (0,)

    class _Conn:
        def cursor(self):
            return _Cur()

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(db, "db_conn", lambda: _Ctx())


def test_workflow_health_degrades_with_all_workers_offline(monkeypatch):
    from core.agent_workflow import workflow_health

    _workflow_db(monkeypatch, (2, 0, 2))  # registered, online, expected
    out = workflow_health()
    assert out["status"] == "degraded" and out["ok"] is False
    assert "all_workers_offline" in out["degraded_reasons"]


def test_workflow_health_ok_when_one_worker_online_or_all_stopped(monkeypatch):
    from core.agent_workflow import workflow_health

    _workflow_db(monkeypatch, (2, 1, 2))
    assert workflow_health()["status"] == "healthy"
    _workflow_db(monkeypatch, (2, 0, 0))  # both deliberately STOPPED
    assert workflow_health()["status"] == "healthy"


# -- U41 / U42: console copy tells the truth about health and the Wilson minimum --

def _repo_file(name):
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / name).read_text(encoding="utf-8")


def test_console_wilson_minimum_matches_backend_math():
    import re
    import shutil
    import subprocess

    import pytest

    from core.binomial_stats import wilson_lower_bound
    from core.super_ghost_top_picks import MIN_COMPLETED, MIN_DIRECTION_WIN_RATE

    expected = next(n for n in range(1, 200)
                    if wilson_lower_bound(n, n) >= MIN_DIRECTION_WIN_RATE)
    expected = max(MIN_COMPLETED, expected)
    assert expected == 9
    html = _repo_file("ghost_console.html")
    assert "' / 5 min</b>" not in html and "' / 5 minimum" not in html
    fn = re.search(r"function topGateMinN\(\)\{.*?\}return 5\}", html)
    assert fn, "topGateMinN() missing"
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    out = subprocess.run([node, "-e", fn.group(0) + ";console.log(topGateMinN())"],
                         capture_output=True, text=True, timeout=20)
    assert out.stdout.strip() == str(expected)


def test_paper_wallet_dates_follow_the_et_session_not_utc(monkeypatch):
    """U53: at 20:30 ET (00:30 UTC next day) the book is still on today."""
    import datetime as real_dt

    import core.paper_wallet as pw

    fixed_utc = real_dt.datetime(2026, 9, 30, 0, 30, tzinfo=real_dt.timezone.utc)

    class _FrozenDateTime(real_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_utc.astimezone(tz) if tz else fixed_utc.replace(tzinfo=None)

    monkeypatch.setattr(real_dt, "datetime", _FrozenDateTime)
    assert pw._session_today() == real_dt.date(2026, 9, 29)
    assert pw._month_key() == "2026-09"  # UTC would already say October


def test_picks_page_reports_health_status_not_http_200():
    html = _repo_file("picks.html")
    assert "(health.__ok?'Healthy':" not in html
    assert "hStatus === 'healthy' ? 'Healthy'" in html
