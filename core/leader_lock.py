"""core/leader_lock.py — single background-work leader election across replicas.

Ghost Protocol runs single-instance, but Railway deploys can overlap (the old
instance drains while the new one boots) and scaling to >1 replica would
otherwise run the scheduler + intraday monitors in every process — double-sending
Telegram cards and double-writing DB rows.

A PostgreSQL **session-level** advisory lock elects exactly one process as the
background-work leader. The lock is held on a dedicated connection (outside the
pool) for the process lifetime; when the process exits or the connection drops,
the lock is released and another replica can take over.

This is distinct from the transaction-scoped ``pg_advisory_xact_lock`` keys used
elsewhere (``_PERF_CYCLE_LOCK_ID``, ``_PREDICTION_SAVE_LOCK_ID``,
``_SEED_ADVISORY_LOCK_KEY``): those auto-release at commit, while a leader lock
must survive across the many transactions a scheduler runs over its lifetime.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Optional

import psycopg2

LOGGER = logging.getLogger("ghost.leader")

# Arbitrary constant, unique app-wide (kept distinct from the xact-lock keys).
LEADER_LOCK_KEY = 1_042_007_001

_leader_conn: Optional[psycopg2.extensions.connection] = None
# True only when election is deliberately off (SCHEDULER_LEADER_LOCK=0) or there
# is no database to elect through (local dev). Never set on an election ERROR.
_leader_without_lock = False

# Bounded connect so a boot during a DB blip cannot hang the lifespan, and TCP
# keepalives so a silently dropped session is noticed by the liveness check.
_CONNECT_KWARGS = {
    "connect_timeout": 5,
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}


def leader_lock_enabled() -> bool:
    """SCHEDULER_LEADER_LOCK=0 disables election (dev / single-process tests)."""
    return os.getenv("SCHEDULER_LEADER_LOCK", "1").strip().lower() in ("1", "true", "yes", "on")


def is_leader() -> bool:
    """True if this process may run background work right now.

    Either it holds the session advisory lock, or election is deliberately
    disabled. This is the per-tick gate the scheduler and monitors consult, so
    it must be a cheap in-memory read (no I/O).
    """
    return _leader_without_lock or _leader_conn is not None


def try_acquire_leader() -> bool:
    """Try to become the background-work leader. Returns True if acquired.

    Opens a dedicated connection (outside the pool) and takes a session-level
    advisory lock. The connection is held for the process lifetime; the lock is
    released automatically when the process exits or the connection drops.

    Fail CLOSED on error: if the lock cannot be evaluated (DB unreachable,
    connect timeout, ...) this process is NOT the leader. During a rolling
    deploy the old instance may still hold the lock; assuming leadership on a
    failed connect would run two schedulers (double cards, double paper
    orders). The caller retries via ``wait_for_leadership`` so a single
    instance still becomes leader as soon as the DB answers.

    Idempotent: a process that already holds a live lock returns True without
    opening a second connection.
    """
    global _leader_conn, _leader_without_lock
    if not leader_lock_enabled():
        LOGGER.info("Leader lock disabled (SCHEDULER_LEADER_LOCK=0) — assuming leader")
        _leader_without_lock = True
        return True
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        # Not an error path: without a database there is nothing to elect
        # through and nothing shared to double-write (local dev only).
        LOGGER.warning("No DATABASE_URL — cannot elect leader; assuming single-process leader")
        _leader_without_lock = True
        return True
    _leader_without_lock = False
    if _leader_conn is not None:
        if _connection_alive(_leader_conn):
            return True
        _drop_leader_conn("held lock connection is dead")
    conn: Optional[psycopg2.extensions.connection] = None
    try:
        conn = psycopg2.connect(dsn, **_CONNECT_KWARGS)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(%s)", (LEADER_LOCK_KEY,))
        got = cur.fetchone()
        cur.close()
        if got and got[0]:
            _leader_conn = conn
            LOGGER.info("Acquired scheduler leader lock (key=%s)", LEADER_LOCK_KEY)
            return True
        conn.close()
        LOGGER.info("Another replica holds the leader lock — running HTTP-only (no scheduler/monitors)")
        return False
    except Exception as e:
        LOGGER.warning(
            "Leader lock acquisition failed (%s) — NOT leader; will retry",
            str(e)[:120],
        )
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return False


def _connection_alive(conn) -> bool:
    """Cheap liveness probe on the dedicated lock connection.

    A session advisory lock lives exactly as long as its session, so a session
    that still answers ``SELECT 1`` still holds the lock; one that errors (server
    restart, network drop, idle kill) has already released it server-side.
    """
    try:
        if getattr(conn, "closed", 0):
            return False
        cur = conn.cursor()
        try:
            cur.execute("SELECT 1")
            row = cur.fetchone()
        finally:
            try:
                cur.close()
            except Exception:
                pass
        return bool(row) and row[0] == 1
    except Exception as e:
        LOGGER.warning("Leader lock connection check failed: %s", str(e)[:120])
        return False


def _drop_leader_conn(reason: str) -> None:
    global _leader_conn
    conn, _leader_conn = _leader_conn, None
    LOGGER.critical("Lost scheduler leader lock (%s) — background work paused", reason)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def check_leadership() -> str:
    """Verify (and if needed repair) leadership. Blocking; run off the event loop.

    Returns one of:
      "leader"      still holding a live lock (or election disabled)
      "reacquired"  the lock session had dropped and was re-taken
      "lost"        the lock session dropped and could not be re-taken
                    (another replica took over, or the DB is unreachable)
      "follower"    not leader before the check and still not leader
    While not leader, ``is_leader()`` is False so gated work pauses; a later
    call re-acquires when the lock is free again.
    """
    if _leader_without_lock:
        return "leader"
    if _leader_conn is None:
        return "reacquired" if try_acquire_leader() else "follower"
    if _connection_alive(_leader_conn):
        return "leader"
    _drop_leader_conn("lock connection dropped")
    if try_acquire_leader():
        LOGGER.warning("Re-acquired scheduler leader lock after connection drop")
        return "reacquired"
    return "lost"


def leader_liveness_interval_s() -> float:
    """How often the leader verifies its lock session (SCHEDULER_LEADER_LIVENESS_S)."""
    try:
        interval = float(os.getenv("SCHEDULER_LEADER_LIVENESS_S", "30"))
    except (TypeError, ValueError):
        interval = 30.0
    return max(5.0, min(300.0, interval))


async def watch_leadership(*, interval_s: float | None = None) -> None:
    """Background loop: detect a dropped lock session and re-acquire or relinquish.

    Relinquishing is in-memory: ``is_leader()`` turns False, which stops the
    scheduler dispatching and the intraday monitors from acting until the lock
    is re-taken. Runs forever; cancel it on shutdown.
    """
    interval = leader_liveness_interval_s() if interval_s is None else max(0.0, interval_s)
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(interval)
        try:
            state = await loop.run_in_executor(None, check_leadership)
        except Exception as e:  # never let the watcher die silently
            LOGGER.warning("Leadership check errored: %s", str(e)[:120])
            continue
        if state in ("lost", "reacquired"):
            LOGGER.warning("Scheduler leadership state: %s", state)


def leader_retry_interval_s() -> float:
    """Bounded retry interval for an HTTP-only replica awaiting leadership."""
    try:
        interval = float(os.getenv("SCHEDULER_LEADER_RETRY_S", "5"))
    except (TypeError, ValueError):
        interval = 5.0
    return max(1.0, min(60.0, interval))


async def wait_for_leadership(
    on_acquired: Callable[[], Awaitable[None] | None],
    *,
    retry_s: float | None = None,
) -> None:
    """Retry election after startup so rolling deploys cannot strand the app.

    Railway keeps the old instance alive until the new HTTP instance is ready.
    A one-shot startup election therefore cannot wait for the old process: doing
    so deadlocks readiness, while giving up permanently leaves no scheduler once
    the old process drains. The new instance serves HTTP, then calls this loop in
    a background task until the session lock becomes available.
    """
    interval = leader_retry_interval_s() if retry_s is None else max(0.0, retry_s)
    while True:
        await asyncio.sleep(interval)
        if not try_acquire_leader():
            continue
        LOGGER.info("Acquired scheduler leadership after HTTP-ready handoff")
        result = on_acquired()
        if inspect.isawaitable(result):
            await result
        return


def release_leader() -> None:
    """Release the leader lock (closes the dedicated connection)."""
    global _leader_conn, _leader_without_lock
    _leader_without_lock = False
    if _leader_conn is not None:
        try:
            _leader_conn.close()
        except Exception:
            pass
        _leader_conn = None
        LOGGER.info("Released scheduler leader lock")
