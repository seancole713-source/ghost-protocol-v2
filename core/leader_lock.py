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


def leader_lock_enabled() -> bool:
    """SCHEDULER_LEADER_LOCK=0 disables election (dev / single-process tests)."""
    return os.getenv("SCHEDULER_LEADER_LOCK", "1").strip().lower() in ("1", "true", "yes", "on")


def is_leader() -> bool:
    """True if this process currently holds the leader lock."""
    return _leader_conn is not None


def try_acquire_leader() -> bool:
    """Try to become the background-work leader. Returns True if acquired.

    Opens a dedicated connection (outside the pool) and takes a session-level
    advisory lock. The connection is held for the process lifetime; the lock is
    released automatically when the process exits or the connection drops.

    Fail-open on error: if the lock cannot be evaluated (no DATABASE_URL, DB
    down, etc.) we assume leadership rather than silently disabling all
    background work — the single-instance deployment is the common case and a
    false "not leader" would stop the scheduler entirely.
    """
    global _leader_conn
    if not leader_lock_enabled():
        LOGGER.info("Leader lock disabled (SCHEDULER_LEADER_LOCK=0) — assuming leader")
        return True
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        LOGGER.warning("No DATABASE_URL — cannot elect leader; assuming leader")
        return True
    conn: Optional[psycopg2.extensions.connection] = None
    try:
        conn = psycopg2.connect(dsn)
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
        LOGGER.warning("Leader lock acquisition failed (%s) — assuming leader", str(e)[:120])
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return True


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
    global _leader_conn
    if _leader_conn is not None:
        try:
            _leader_conn.close()
        except Exception:
            pass
        _leader_conn = None
        LOGGER.info("Released scheduler leader lock")
