"""tests/test_leader_lock.py — BG-4/ST-8 scheduler leader election.

A session-level PostgreSQL advisory lock elects exactly one background-work
leader across replicas. Non-leaders serve HTTP only; the leader runs the
scheduler + intraday monitors. Fail CLOSED on error: a failed connect during a
rolling deploy must not produce a second leader; the HTTP-only replica keeps
retrying leadership, and the leader re-checks its lock session periodically.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import core.leader_lock as ll


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._row = None

    def execute(self, sql, params=None):
        if self._conn.dead:
            raise RuntimeError("server closed the connection unexpectedly")
        self._conn.queries.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            assert params == (ll.LEADER_LOCK_KEY,)
            self._row = (self._conn.got,)
        else:
            self._row = (1,)

    def fetchone(self):
        return self._row

    def close(self):
        pass


class _FakeConn:
    def __init__(self, got):
        self.got = got
        self.autocommit = False
        self.closed = False
        self.dead = False
        self.queries = []

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True


def _connect_returning(*conns):
    it = iter(conns)
    calls = []

    def connect(dsn, **kwargs):
        calls.append(kwargs)
        nxt = next(it)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    connect.calls = calls
    return connect


@pytest.fixture(autouse=True)
def _lock_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SCHEDULER_LEADER_LOCK", "1")
    ll.release_leader()
    yield
    ll.release_leader()


def test_acquire_leader_when_lock_free(monkeypatch):
    conn = _FakeConn(True)
    connect = _connect_returning(conn)
    monkeypatch.setattr(ll.psycopg2, "connect", connect)
    assert ll.try_acquire_leader() is True
    assert ll.is_leader() is True
    # The lock was taken on a dedicated connection with the right key, with a
    # bounded connect and TCP keepalives so a dropped session is noticed.
    assert conn.autocommit is True
    assert connect.calls[0]["connect_timeout"] > 0
    assert connect.calls[0]["keepalives"] == 1
    # Idempotent: a live held lock does not open a second connection.
    assert ll.try_acquire_leader() is True
    assert len(connect.calls) == 1
    ll.release_leader()
    assert conn.closed is True
    assert ll.is_leader() is False


def test_acquire_leader_skips_when_held_elsewhere(monkeypatch):
    conn = _FakeConn(False)
    monkeypatch.setattr(ll.psycopg2, "connect", _connect_returning(conn))
    assert ll.try_acquire_leader() is False
    assert ll.is_leader() is False
    assert conn.closed is True  # non-leader closes its probe connection


def test_acquire_leader_fails_closed_on_db_error(monkeypatch):
    monkeypatch.setattr(
        ll.psycopg2, "connect", _connect_returning(RuntimeError("db down")),
    )
    assert ll.try_acquire_leader() is False  # fail CLOSED: not leader
    assert ll.is_leader() is False


def test_acquire_leader_fails_closed_when_lock_query_errors(monkeypatch):
    conn = _FakeConn(True)
    conn.dead = True
    monkeypatch.setattr(ll.psycopg2, "connect", _connect_returning(conn))
    assert ll.try_acquire_leader() is False
    assert ll.is_leader() is False
    assert conn.closed is True


def test_failed_boot_election_is_retried_until_lock_is_free(monkeypatch):
    """Rolling deploy: the connect fails at boot (no second leader), the
    HTTP-only path retries and takes over once the lock can be evaluated."""
    held_elsewhere, winner = _FakeConn(False), _FakeConn(True)
    monkeypatch.setattr(ll.psycopg2, "connect", _connect_returning(
        RuntimeError("connection refused"), held_elsewhere, winner,
    ))
    assert ll.try_acquire_leader() is False
    started = []

    async def fake_sleep(delay):
        pass

    monkeypatch.setattr(ll.asyncio, "sleep", fake_sleep)
    asyncio.run(ll.wait_for_leadership(lambda: started.append(True), retry_s=0))
    assert started == [True]
    assert ll.is_leader() is True


def test_acquire_leader_without_dsn_is_single_process_leader(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert ll.try_acquire_leader() is True
    assert ll.is_leader() is True


def test_leader_lock_disabled_assumes_leader(monkeypatch):
    monkeypatch.setenv("SCHEDULER_LEADER_LOCK", "0")
    assert ll.try_acquire_leader() is True
    assert ll.is_leader() is True  # gates stay open in disabled mode
    assert ll.check_leadership() == "leader"
    ll.release_leader()
    assert ll.is_leader() is False


def test_liveness_check_keeps_healthy_leader(monkeypatch):
    conn = _FakeConn(True)
    monkeypatch.setattr(ll.psycopg2, "connect", _connect_returning(conn))
    assert ll.try_acquire_leader() is True
    assert ll.check_leadership() == "leader"
    assert conn.queries[-1][0] == "SELECT 1"
    assert ll.is_leader() is True


def test_dropped_lock_session_is_reacquired(monkeypatch):
    first, second = _FakeConn(True), _FakeConn(True)
    connect = _connect_returning(first, second)
    monkeypatch.setattr(ll.psycopg2, "connect", connect)
    assert ll.try_acquire_leader() is True
    first.dead = True  # server restart / network drop released the lock
    assert ll.check_leadership() == "reacquired"
    assert first.closed is True
    assert ll.is_leader() is True
    assert ll._leader_conn is second


def test_dropped_lock_session_taken_by_other_replica_relinquishes(monkeypatch):
    first, other_holds = _FakeConn(True), _FakeConn(False)
    monkeypatch.setattr(ll.psycopg2, "connect", _connect_returning(
        first, other_holds, RuntimeError("db down"), _FakeConn(True),
    ))
    assert ll.try_acquire_leader() is True
    first.dead = True
    assert ll.check_leadership() == "lost"
    assert ll.is_leader() is False  # scheduler/monitors gate closes
    # DB unreachable on the next check: stays follower (fail closed).
    assert ll.check_leadership() == "follower"
    assert ll.is_leader() is False
    # Lock free again: the next periodic check re-acquires.
    assert ll.check_leadership() == "reacquired"
    assert ll.is_leader() is True


def test_closed_connection_attribute_counts_as_dead(monkeypatch):
    conn = _FakeConn(True)
    monkeypatch.setattr(ll.psycopg2, "connect", _connect_returning(conn, RuntimeError("down")))
    assert ll.try_acquire_leader() is True
    conn.closed = 1
    assert ll.check_leadership() == "lost"
    assert ll.is_leader() is False


def test_watch_leadership_runs_checks_off_loop(monkeypatch):
    seen = []
    states = iter(("leader", "lost", "reacquired"))

    def fake_check():
        state = next(states)
        seen.append(state)
        return state

    async def run():
        task = asyncio.get_running_loop().create_task(ll.watch_leadership(interval_s=0))
        for _ in range(200):
            if len(seen) >= 3:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    monkeypatch.setattr(ll, "check_leadership", fake_check)
    asyncio.run(run())
    assert seen[:3] == ["leader", "lost", "reacquired"]


def test_leader_liveness_interval_is_bounded(monkeypatch):
    monkeypatch.setenv("SCHEDULER_LEADER_LIVENESS_S", "1")
    assert ll.leader_liveness_interval_s() == 5.0
    monkeypatch.setenv("SCHEDULER_LEADER_LIVENESS_S", "9999")
    assert ll.leader_liveness_interval_s() == 300.0
    monkeypatch.setenv("SCHEDULER_LEADER_LIVENESS_S", "junk")
    assert ll.leader_liveness_interval_s() == 30.0


def test_leader_retry_interval_is_bounded(monkeypatch):
    monkeypatch.setenv("SCHEDULER_LEADER_RETRY_S", "0")
    assert ll.leader_retry_interval_s() == 1.0
    monkeypatch.setenv("SCHEDULER_LEADER_RETRY_S", "999")
    assert ll.leader_retry_interval_s() == 60.0
    monkeypatch.setenv("SCHEDULER_LEADER_RETRY_S", "not-a-number")
    assert ll.leader_retry_interval_s() == 5.0


def test_wait_for_leadership_retries_then_starts_runtime(monkeypatch):
    attempts = iter((False, False, True))
    sleeps = []
    started = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    async def start_runtime():
        started.append(True)

    monkeypatch.setattr(ll.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(ll, "try_acquire_leader", lambda: next(attempts))

    asyncio.run(ll.wait_for_leadership(start_runtime, retry_s=0))

    assert sleeps == [0.0, 0.0, 0.0]
    assert started == [True]


def test_lifespan_keeps_nonleader_alive_for_handoff():
    source = (Path(__file__).resolve().parents[1] / "wolf_app.py").read_text(
        encoding="utf-8"
    )
    assert "wait_for_leadership(_start_leader_runtime)" in source
    assert "serving HTTP and retrying leadership" in source
    assert "skipping scheduler + intraday monitors" not in source


def test_scheduler_dispatch_gate_blocks_jobs_when_not_leader(monkeypatch):
    """Per-tick guard: a closed gate (lock lost) starts no job; reopening
    resumes dispatch. A gate that raises fails closed."""
    import core.scheduler as sch

    ran = []
    leader = {"on": False}
    monkeypatch.setattr(sch, "_tasks", {})
    monkeypatch.setattr(sch, "_running", True)
    sch.register("job", lambda: ran.append(1), interval_s=3600, initial_delay_s=0)
    sch.set_dispatch_gate(lambda: leader["on"])
    ticks = {"n": 0}
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        ticks["n"] += 1
        if ticks["n"] == 2:
            leader["on"] = True
        if ticks["n"] >= 3:
            sch._running = False
        await real_sleep(0)

    async def run():
        await sch._loop()
        for _ in range(100):
            if ran:
                break
            await real_sleep(0.01)

    monkeypatch.setattr(sch.asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(run())
    finally:
        sch.set_dispatch_gate(None)
    assert ran == [1]  # nothing on the two closed ticks, one run once open
    assert sch.heartbeat()["last_tick_ago_s"] is not None

    def boom():
        raise RuntimeError("gate error")

    sch.set_dispatch_gate(boom)
    try:
        assert sch._gate_open() is False
    finally:
        sch.set_dispatch_gate(None)


def test_lifespan_gates_scheduler_and_watches_leadership():
    root = Path(__file__).resolve().parents[1]
    source = (root / "wolf_app.py").read_text(encoding="utf-8")
    assert "scheduler.set_dispatch_gate(is_leader)" in source
    assert "watch_leadership()" in source
    for mod in ("wolf_monitor.py", "squeeze_monitor.py"):
        text = (root / "core" / mod).read_text(encoding="utf-8")
        assert "if not is_leader():" in text, mod
