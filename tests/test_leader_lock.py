"""tests/test_leader_lock.py — BG-4/ST-8 scheduler leader election.

A session-level PostgreSQL advisory lock elects exactly one background-work
leader across replicas. Non-leaders serve HTTP only; the leader runs the
scheduler + intraday monitors. Fail-open on error (single-instance is the
common case; a false "not leader" would stop all background work).
"""
from __future__ import annotations

import core.leader_lock as ll


class _FakeCursor:
    def __init__(self, got):
        self._got = got

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchone(self):
        return (self._got,)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, got):
        self._got = got
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._got)

    def close(self):
        self.closed = True


def test_acquire_leader_when_lock_free(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SCHEDULER_LEADER_LOCK", "1")
    conn = _FakeConn(True)
    monkeypatch.setattr(ll.psycopg2, "connect", lambda dsn: conn)
    ll.release_leader()
    assert ll.try_acquire_leader() is True
    assert ll.is_leader() is True
    # The lock was taken on a dedicated connection with the right key.
    assert conn.autocommit is True
    ll.release_leader()
    assert conn.closed is True
    assert ll.is_leader() is False


def test_acquire_leader_skips_when_held_elsewhere(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SCHEDULER_LEADER_LOCK", "1")
    conn = _FakeConn(False)
    monkeypatch.setattr(ll.psycopg2, "connect", lambda dsn: conn)
    ll.release_leader()
    assert ll.try_acquire_leader() is False
    assert ll.is_leader() is False
    assert conn.closed is True  # non-leader closes its probe connection


def test_acquire_leader_fails_open_on_db_error(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SCHEDULER_LEADER_LOCK", "1")

    def _boom(dsn):
        raise RuntimeError("db down")

    monkeypatch.setattr(ll.psycopg2, "connect", _boom)
    ll.release_leader()
    assert ll.try_acquire_leader() is True  # fail-open: assume leader
    assert ll.is_leader() is False  # but no lock actually held


def test_acquire_leader_fails_open_without_dsn(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SCHEDULER_LEADER_LOCK", "1")
    ll.release_leader()
    assert ll.try_acquire_leader() is True


def test_leader_lock_disabled_assumes_leader(monkeypatch):
    monkeypatch.setenv("SCHEDULER_LEADER_LOCK", "0")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    ll.release_leader()
    assert ll.try_acquire_leader() is True
    assert ll.is_leader() is False  # no lock held in disabled mode
