"""Security / ops audit fixes (2026-09-25 audit U04, U05, U08, U18, U21, U23,
U25, U26, U38, U48, U61, U69, U46 and the /mcp/tools route shadow)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from shared import request_guard as rg

ROOT = Path(__file__).resolve().parents[1]


def _req(xff=None, host="10.0.0.5"):
    headers = {} if xff is None else {"x-forwarded-for": xff}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))


# ── U26: client identity from the trusted (rightmost) hop ─────────────────

def test_client_ip_ignores_client_chosen_leftmost_entry(monkeypatch):
    monkeypatch.delenv("GHOST_TRUSTED_PROXY_HOPS", raising=False)
    # A client sends its own XFF; the edge appends the real address.
    assert rg.client_ip(_req("6.6.6.6, 203.0.113.9")) == "203.0.113.9"
    assert rg.client_ip(_req("1.1.1.1, 2.2.2.2, 203.0.113.9")) == "203.0.113.9"
    assert rg.client_ip(_req("203.0.113.9")) == "203.0.113.9"


def test_client_ip_hop_count_and_fallbacks(monkeypatch):
    monkeypatch.setenv("GHOST_TRUSTED_PROXY_HOPS", "2")
    assert rg.client_ip(_req("6.6.6.6, 203.0.113.9, 10.1.1.1")) == "203.0.113.9"
    monkeypatch.setenv("GHOST_TRUSTED_PROXY_HOPS", "0")
    assert rg.client_ip(_req("6.6.6.6, 203.0.113.9")) == "10.0.0.5"
    monkeypatch.delenv("GHOST_TRUSTED_PROXY_HOPS")
    assert rg.client_ip(_req(None)) == "10.0.0.5"
    assert rg.client_ip(SimpleNamespace(headers={}, client=None)) == "unknown"


def test_wolf_app_rate_limit_key_uses_trusted_hop(monkeypatch):
    import wolf_app

    monkeypatch.delenv("GHOST_TRUSTED_PROXY_HOPS", raising=False)
    a = wolf_app._client_ip(_req("1.1.1.1, 198.51.100.7", host="1.1.1.1"))
    b = wolf_app._client_ip(_req("2.2.2.2, 198.51.100.7", host="2.2.2.2"))
    assert a == b == "198.51.100.7"


# ── U18: failed-credential lockout ─────────────────────────────────────────

def test_failure_lockout_locks_then_ages_out(monkeypatch):
    monkeypatch.setenv("AUTH_FAILURE_LIMIT", "3")
    monkeypatch.setenv("AUTH_FAILURE_WINDOW_S", "100")
    lock = rg.FailureLockout(limit_env="AUTH_FAILURE_LIMIT", window_env="AUTH_FAILURE_WINDOW_S",
                             default_limit=10, default_window_s=900)
    t0 = 1_000_000.0
    assert lock.record_failure("ip", now=t0) is False
    assert lock.record_failure("ip", now=t0 + 1) is False
    assert lock.retry_after("ip", now=t0 + 2) == 0
    assert lock.record_failure("ip", now=t0 + 2) is True
    assert lock.retry_after("ip", now=t0 + 3) > 0
    assert lock.retry_after("other", now=t0 + 3) == 0
    # The oldest failure ages out after the window -> below the limit again.
    assert lock.retry_after("ip", now=t0 + 101) == 0


def test_cron_secret_guessing_is_locked_out(monkeypatch):
    import wolf_app

    monkeypatch.setenv("GHOST_TEST_MODE", "1")
    monkeypatch.setenv("CRON_SECRET", "right-secret")
    monkeypatch.setenv("AUTH_FAILURE_LIMIT", "3")
    rg.CREDENTIAL_LOCKOUT.reset()
    with TestClient(wolf_app.APP) as client:
        for _ in range(3):
            r = client.post("/api/health/audit?auto_fix=false&persist=false",
                            headers={"x-cron-secret": "guess"})
            assert r.status_code == 403
        # Locked: even the correct secret is refused (no oracle).
        r = client.post("/api/health/audit?auto_fix=false&persist=false",
                        headers={"x-cron-secret": "right-secret"})
        assert r.status_code == 429
        assert int(r.headers["Retry-After"]) > 0
        # A request that presents no secret is not affected by the lockout.
        assert client.get("/favicon.ico").status_code == 200
    rg.CREDENTIAL_LOCKOUT.reset()


def test_admin_login_feeds_lockout(monkeypatch):
    import wolf_app

    monkeypatch.setenv("CRON_SECRET", "letmein")
    monkeypatch.setenv("LOGIN_ATTEMPTS_PER_MIN", "100")
    monkeypatch.setenv("AUTH_FAILURE_LIMIT", "2")
    rg.CREDENTIAL_LOCKOUT.reset()
    client = TestClient(wolf_app.APP)
    assert client.post("/admin/login", json={"secret": "x"}).status_code == 401
    assert client.post("/admin/login", json={"secret": "y"}).status_code == 401
    r = client.post("/admin/login", json={"secret": "letmein"})
    assert r.status_code == 429
    rg.CREDENTIAL_LOCKOUT.reset()


def _mcp_request(headers, host="203.0.113.50"):
    hdrs = {"x-forwarded-proto": "https"}
    hdrs.update(headers)
    return SimpleNamespace(headers=hdrs, client=SimpleNamespace(host=host), cookies={},
                           url=SimpleNamespace(scheme="https", netloc="ghost.example"))


def test_mcp_static_token_guessing_is_locked_out(monkeypatch):
    from mcp import security

    monkeypatch.setenv("GHOST_MCP_TOKEN", "the-token")
    monkeypatch.setenv("GHOST_TEST_MODE", "1")
    monkeypatch.setenv("AUTH_FAILURE_LIMIT", "2")
    rg.CREDENTIAL_LOCKOUT.reset()
    for _ in range(2):
        with pytest.raises(HTTPException) as exc:
            security.require_mcp_auth(_mcp_request({"x-ghost-mcp-token": "nope"}))
        assert exc.value.status_code == 401
    with pytest.raises(HTTPException) as exc:
        security.require_mcp_auth(_mcp_request({"x-ghost-mcp-token": "the-token"}))
    assert exc.value.status_code == 429
    # Other clients are unaffected.
    security.require_mcp_auth(_mcp_request({"x-ghost-mcp-token": "the-token"}, host="198.51.100.1"))
    rg.CREDENTIAL_LOCKOUT.reset()


def test_mcp_missing_or_jwt_credentials_do_not_count(monkeypatch):
    from mcp import security

    monkeypatch.setenv("GHOST_MCP_TOKEN", "the-token")
    monkeypatch.setenv("AUTH_FAILURE_LIMIT", "1")
    rg.CREDENTIAL_LOCKOUT.reset()
    # No credential (lazy-OAuth discovery) and an expired/foreign JWT are not guesses.
    assert security.is_mcp_authenticated(_mcp_request({})) is False
    assert security.is_mcp_authenticated(
        _mcp_request({"authorization": "Bearer a.b.c"})) is False
    assert rg.CREDENTIAL_LOCKOUT.retry_after("203.0.113.50") == 0
    rg.CREDENTIAL_LOCKOUT.reset()


def _oauth_app(monkeypatch):
    from mcp import oauth_routes

    monkeypatch.setattr(oauth_routes, "oauth_configured", lambda: True)
    monkeypatch.setattr(oauth_routes, "fetch_cimd_client",
                        lambda cid: {"client_id": cid, "redirect_uris": ["https://claude.ai/cb"]})
    app = FastAPI()
    app.include_router(oauth_routes.router)
    return TestClient(app)


def test_oauth_consent_page_escapes_client_id(monkeypatch):
    client = _oauth_app(monkeypatch)
    evil = 'https://"><img src=x onerror=alert(1)>@evil.example/cimd.json'
    r = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": evil, "redirect_uri": "https://claude.ai/cb",
        "state": '"><script>alert(2)</script>', "code_challenge": "abc",
    })
    assert r.status_code == 200
    assert "<img" not in r.text and "<script>alert" not in r.text
    assert "onerror=alert" not in r.text.replace("onerror%3Dalert", "")
    # Only the hostname is shown, not the attacker's userinfo.
    assert "<b>evil.example</b>" in r.text
    # The consent copy no longer claims read-only access (U40).
    assert "read-only" not in r.text


def test_oauth_secret_guessing_is_locked_out(monkeypatch):
    monkeypatch.setenv("GHOST_OAUTH_SECRET", "op-secret")
    monkeypatch.setenv("AUTH_FAILURE_LIMIT", "2")
    rg.CREDENTIAL_LOCKOUT.reset()
    client = _oauth_app(monkeypatch)
    form = {"oauth_query": "client_id=https%3A%2F%2Fclaude.ai%2Fcimd&redirect_uri=https%3A%2F%2Fclaude.ai%2Fcb"}
    for guess in ("a", "b"):
        r = client.post("/oauth/authorize", data={**form, "secret": guess})
        assert r.status_code == 401
    r = client.post("/oauth/authorize", data={**form, "secret": "op-secret"},
                    follow_redirects=False)
    assert r.status_code == 429
    rg.CREDENTIAL_LOCKOUT.reset()


def test_oauth_signing_key_prefers_dedicated_env(monkeypatch):
    from mcp import oauth_server

    monkeypatch.setenv("GHOST_OAUTH_SECRET", "typed-secret")
    monkeypatch.setenv("GHOST_OAUTH_SIGNING_KEY", "random-signing-key")
    assert oauth_server._signing_key() == b"random-signing-key"
    monkeypatch.delenv("GHOST_OAUTH_SIGNING_KEY")
    assert oauth_server._signing_key() == b"typed-secret"  # legacy fallback kept


def test_mcp_tools_route_not_shadowed_by_path_token(monkeypatch):
    from mcp.routes import router

    monkeypatch.setenv("GHOST_TEST_MODE", "1")
    monkeypatch.setenv("GHOST_MCP_TOKEN", "t0k")
    app = FastAPI()
    app.include_router(router)
    r = TestClient(app).get("/mcp/tools", headers={"x-ghost-mcp-token": "t0k"})
    assert r.status_code == 200
    assert "tools" in r.json()


# ── U61: admin cookie key separable from CRON_SECRET ──────────────────────

def test_admin_cookie_uses_dedicated_key_when_set(monkeypatch):
    import wolf_app

    monkeypatch.setenv("CRON_SECRET", "cron")
    monkeypatch.delenv("GHOST_ADMIN_COOKIE_KEY", raising=False)
    legacy = wolf_app._admin_mint_token()
    assert wolf_app._admin_token_valid(legacy)
    monkeypatch.setenv("GHOST_ADMIN_COOKIE_KEY", "cookie-key")
    assert not wolf_app._admin_token_valid(legacy)
    fresh = wolf_app._admin_mint_token()
    exp = fresh.split(".")[0]
    assert fresh.split(".")[1] == hmac.new(b"cookie-key", exp.encode(), hashlib.sha256).hexdigest()
    assert wolf_app._admin_token_valid(fresh)


# ── U05 / U69: DB session timeouts + pool wait ────────────────────────────

def test_pool_dsn_adds_timeouts_and_keeps_existing_options(monkeypatch):
    import core.db as db
    from psycopg2.extensions import parse_dsn

    monkeypatch.delenv("DB_STATEMENT_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("DB_LOCK_TIMEOUT_MS", raising=False)
    out = parse_dsn(db._pool_dsn("postgresql://u:p@h:5432/d?sslmode=require&options=-c%20search_path%3Dx"))
    assert out["sslmode"] == "require"
    assert "search_path=x" in out["options"]
    assert "statement_timeout=300000" in out["options"]
    assert "lock_timeout=120000" in out["options"]
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "0")
    monkeypatch.setenv("DB_LOCK_TIMEOUT_MS", "0")
    assert db._pool_dsn("postgresql://u:p@h/d") == "postgresql://u:p@h/d"


def test_init_db_falls_back_when_pooler_rejects_options(monkeypatch):
    import core.db as db

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/test")
    seen = []

    class FakePool:
        def __init__(self, mn, mx, dsn):
            seen.append(dsn)
            if "statement_timeout" in dsn:
                raise db.psycopg2.OperationalError("unsupported startup parameter: options")

    monkeypatch.setattr(db.psycopg2.pool, "ThreadedConnectionPool", FakePool)
    monkeypatch.setattr(db, "_ensure_tables", lambda: None)
    monkeypatch.setattr(db, "_migrate_schema", lambda: None)
    monkeypatch.setattr(db, "_pool", None)
    db.init_db()
    assert len(seen) == 2 and seen[1] == "postgresql://u:p@localhost/test"
    assert db._pool is not None


def test_get_conn_waits_for_a_slot_off_the_event_loop(monkeypatch):
    import core.db as db

    calls = {"n": 0}

    class FakePool:
        def getconn(self):
            calls["n"] += 1
            if calls["n"] < 8:  # more than DB_POOL_GET_RETRIES (4)
                raise db.psycopg2.pool.PoolError("connection pool exhausted")
            return "conn"

    monkeypatch.setattr(db, "_pool", FakePool())
    monkeypatch.setattr(db, "_GETCONN_WAIT_S", 5.0)
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)
    assert db.get_conn() == "conn"
    assert calls["n"] == 8


def test_get_conn_does_not_park_the_event_loop(monkeypatch):
    import core.db as db

    calls = {"n": 0}

    class FakePool:
        def getconn(self):
            calls["n"] += 1
            raise db.psycopg2.pool.PoolError("connection pool exhausted")

    monkeypatch.setattr(db, "_pool", FakePool())
    monkeypatch.setattr(db, "_GETCONN_WAIT_S", 30.0)
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)

    async def on_loop():
        with pytest.raises(db.psycopg2.pool.PoolError):
            db.get_conn()

    asyncio.run(on_loop())
    assert calls["n"] == db._GETCONN_RETRIES


# ── U05 / U04: scheduler overlap visibility + failure streaks ─────────────

def test_scheduler_tracks_consecutive_failures(monkeypatch):
    from core import scheduler

    state = {"fail": True}

    def job():
        if state["fail"]:
            raise RuntimeError("boom")

    task = scheduler.Task(name="t", fn=job, interval_s=60, timeout_s=5)
    asyncio.run(scheduler._run_task(task))
    asyncio.run(scheduler._run_task(task))
    assert task.consecutive_failures == 2
    state["fail"] = False
    asyncio.run(scheduler._run_task(task))
    assert task.consecutive_failures == 0


def test_scheduler_overlap_skip_logs_warning(monkeypatch, caplog):
    from core import scheduler

    task = scheduler.Task(name="slow", fn=lambda: None, interval_s=10, timeout_s=5)
    task.running = True
    task.next_run_at = 0
    monkeypatch.setattr(scheduler, "_tasks", {"slow": task})
    monkeypatch.setattr(scheduler, "_dispatch_gate", None)
    monkeypatch.setattr(scheduler, "_running", True)

    async def fake_sleep(_s):
        scheduler._running = False

    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)
    with caplog.at_level("WARNING", logger="ghost.scheduler"):
        asyncio.run(scheduler._loop())
    assert task.skipped_overlap_count == 1
    assert any("still in progress" in r.getMessage() for r in caplog.records)


def test_health_scores_failing_and_overrunning_tasks():
    import wolf_app

    issues, warnings = wolf_app._scheduler_task_findings([
        {"name": "ok", "consecutive_failures": 0},
        {"name": "flaky", "consecutive_failures": 1},
        {"name": "broken", "consecutive_failures": 4},
        {"name": "hung", "consecutive_failures": 0, "running": True,
         "last_run_ago_s": 5000, "interval_s": 300, "timeout_s": 600},
    ])
    assert issues == ["Tasks failing repeatedly: brokenx4"]
    assert "Tasks failing: flakyx1" in warnings
    assert any(w.startswith("Tasks overrunning: hung") for w in warnings)


def test_prediction_cycle_staleness_default_tracks_scan_loop(monkeypatch):
    import wolf_app

    monkeypatch.delenv("PREDICTION_CYCLE_STALE_MIN", raising=False)
    monkeypatch.delenv("MARKET_SCAN_ENABLED", raising=False)
    assert wolf_app._prediction_cycle_stale_min() == 360
    monkeypatch.setenv("MARKET_SCAN_ENABLED", "0")
    assert wolf_app._prediction_cycle_stale_min() == 2160
    monkeypatch.setenv("PREDICTION_CYCLE_STALE_MIN", "90")
    assert wolf_app._prediction_cycle_stale_min() == 90


# ── U08: agent workflow health degrades with workers offline ──────────────

class _WfCursor:
    def __init__(self, pending, online):
        self.pending, self.online, self._last = pending, online, ""

    def execute(self, sql, params=None):
        self._last = sql

    def fetchall(self):
        if "FROM ghost_agent_tasks GROUP BY status" in self._last:
            return [("PENDING", self.pending)] if self.pending else []
        return []

    def fetchone(self):
        if "FROM ghost_agent_workers" in self._last:
            return (2, self.online)
        return (0,)


class _WfConn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur


@pytest.mark.parametrize("pending,online,status", [
    (3, 0, "degraded"), (3, 1, "healthy"), (0, 0, "healthy"),
])
def test_workflow_health_workers_offline(monkeypatch, pending, online, status):
    import core.db
    from core import agent_workflow

    monkeypatch.delenv("AGENT_WORKFLOW_MIN_ONLINE_WORKERS", raising=False)
    monkeypatch.setattr(core.db, "db_conn", lambda: _WfConn(_WfCursor(pending, online)))
    out = agent_workflow.workflow_health()
    assert out["status"] == status
    assert ("workers_offline" in out["issues"]) is (status == "degraded")


# ── U38: leadership retry off the event loop ──────────────────────────────

def test_wait_for_leadership_attempts_off_event_loop(monkeypatch):
    from core import leader_lock as ll

    threads = []

    def attempt():
        threads.append(threading.current_thread() is threading.main_thread())
        return len(threads) >= 2

    monkeypatch.setattr(ll, "try_acquire_leader", attempt)
    started = []
    asyncio.run(ll.wait_for_leadership(lambda: started.append(True), retry_s=0))
    assert started == [True]
    assert threads == [False, False]


# ── U25: boot mutations are leader-only ───────────────────────────────────

def test_boot_purges_run_only_in_leader_runtime():
    import wolf_app

    src = inspect.getsource(wolf_app.lifespan)
    for call in ("_auto_purge_bad_models()", "_purge_v3_stale_or_weak()",
                 "DELETE FROM user_portfolio"):
        # Only inside the leader runtime (the startup-train thread purges too).
        assert src.count(call) == src[src.index("async def _start_leader_runtime()"):].count(call)
    assert "_leader_boot_maintenance" in src
    maint = inspect.getsource(wolf_app._leader_boot_maintenance)
    assert "DELETE FROM user_portfolio" in maint and "_auto_purge_bad_models()" in maint


def test_leader_boot_maintenance_purges_ghost_portfolio_rows(monkeypatch):
    import wolf_app

    deleted = []

    class Cur:
        def execute(self, sql, params=None):
            if sql.startswith("DELETE"):
                deleted.append(params[0])

        def fetchall(self):
            return [(1, "WOLF"), (2, "ZZE2E1"), (3, "TEST")]

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return Cur()

    monkeypatch.setattr(wolf_app, "db_conn", lambda: Conn())
    monkeypatch.setattr(wolf_app, "_auto_purge_bad_models", lambda: 0)
    monkeypatch.setattr(wolf_app, "_purge_v3_stale_or_weak", lambda: 0)
    wolf_app._leader_boot_maintenance()
    assert deleted == [2, 3]


# ── U48: public audit history runs no DDL ─────────────────────────────────

def test_health_audit_history_runs_no_ddl(monkeypatch):
    import wolf_app

    executed = []

    class Cur:
        def execute(self, sql, params=None):
            executed.append(sql)

        def fetchone(self):
            return (None,)

        def fetchall(self):
            return []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return Cur()

    monkeypatch.setattr(wolf_app, "db_conn", lambda: Conn())
    out = wolf_app.health_audit_history(limit=5)
    assert out == {"ok": True, "runs": []}
    assert not any(re.search(r"\bCREATE\b", s, re.I) for s in executed)


# ── U23: runtime pins + edge CI interpreter ───────────────────────────────

def test_every_preflight_module_is_pinned_exactly():
    from scripts import runtime_preflight as preflight

    pins = {}
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if "==" in line and not line.startswith("#"):
            name, ver = line.split("==", 1)
            pins[name] = ver
    missing = [name for name in preflight.MODULES if name not in pins]
    assert missing == []
    assert pins["scipy"] == "1.17.1" and pins["pandas"] == "3.0.6"
    assert pins["numba"] == "0.67.0" and pins["llvmlite"] == "0.49.0"


def test_edge_ci_uses_production_python_and_pinned_deps():
    workflow = (ROOT / ".github" / "workflows" / "edge.yml").read_text()
    assert 'python-version-file: ".python-version"' in workflow
    assert '"3.11"' not in workflow
    for line in (ROOT / "edge" / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            assert "==" in line, f"unpinned edge dependency: {line}"


# ── U39: core holiday table covers 2027 and agrees with edge's ────────────

def test_core_holidays_cover_2027_and_match_edge():
    import datetime as dt

    from core import market_hours as mh
    from edge import calendar as ecal

    core_2027 = {d for d in mh._NYSE_FULL_DAY_HOLIDAYS if d.startswith("2027")}
    edge_2027 = {d for d in ecal.BUILTIN_HOLIDAYS if d.startswith("2027")}
    assert len(core_2027) == 10 and core_2027 == edge_2027
    assert mh.is_market_holiday(dt.date(2027, 3, 26))
    assert mh.is_half_day(dt.date(2027, 11, 26))
    assert not mh.is_market_holiday(dt.date(2027, 3, 25))
