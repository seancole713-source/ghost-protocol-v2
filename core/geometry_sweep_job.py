"""Run the read-only geometry sweeps ON the box and put the table in the logs.

The stop geometry is the binding constraint on the whole engine. Confirmed live
2026-09-05: every lane of the in-flight fleet retrain failed, 10 of the first 11
with NEGATIVE walk-forward edge, and ARDT/UP scored acc=67.7% with edge=0.0% --
a wide stop inflating the win rate to something that looks excellent and
predicts nothing. tier = "proven" if passes else "research" then stamps every
model research, and research tier is hard-blocked as the first statement of the
lane evaluator, which is the v3_research_tier skip on 92 of 107 symbols.

scripts/geometry_edge_sweep.py and scripts/provable_oppoint_sweep.py already
answer which stop multiplier restores edge. They were written for exactly this
decision on 2026-07-08 and have not been re-run since -- and critically, that
run measured against the 70% precision target while production now runs
contract 55, so the "no operating point at 0.65" disproof has never been tested
at the target actually in force.

They cannot be run from anywhere except this container: the OHLCV chain needs
the provider keys and egress that only exist here. Hence this job.

THREE THINGS MAKE IT SAFE.

SUBPROCESS, NOT IMPORT. The sweep sets os.environ["V3_STOP_VOL_MULT"] as it
walks candidate geometries (scripts/geometry_edge_sweep.py:28,55). Imported into
this process that would silently re-label the LIVE engine mid-flight and leave
it wherever the loop ended. A child process gets its own environment block, so
the running engine cannot observe it at all.

IT TAKES THE RETRAIN LOCK. The sweep is CPU-heavy and so is a fleet retrain.
Holding _RETRAIN_JOB_LOCK means the two can never overlap in either direction.

IT RUNS ONCE, EVER. A marker in ghost_state, keyed by sweep version, is written
on completion whatever the outcome. A failed sweep that retried every cycle
would be a self-inflicted load generator on a live trading engine; bump the
version to deliberately re-run.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("ghost.geometry_sweep")

SWEEP_VERSION = "geometry_sweep_v1"
_STATE_KEY = f"geometry_sweep:{SWEEP_VERSION}"

# geometry_edge_sweep answers "which stop multiplier restores walk-forward
# edge". provable_oppoint_sweep answers "is there a provable operating point
# there" -- the question the 2026-07-08 run only ever asked at the 70% target.
SWEEPS = ("geometry_edge_sweep.py", "provable_oppoint_sweep.py")

_MAX_LOGGED_LINES = 400


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _timeout_s() -> int:
    try:
        return max(300, min(7200, int(os.getenv("GEOMETRY_SWEEP_TIMEOUT_S", "3600"))))
    except (TypeError, ValueError):
        return 3600


def _enabled() -> bool:
    return os.getenv("GEOMETRY_SWEEP_ENABLED", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


def sweep_marker() -> Optional[Dict[str, Any]]:
    """The run-once record, or None if this version has never run."""
    try:
        from core.db import db_conn

        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT val FROM ghost_state WHERE key=%s", (_STATE_KEY,))
            row = cur.fetchone()
            if not row or not row[0]:
                return None
            return json.loads(row[0])
    except Exception:
        return None


def _store_marker(payload: Dict[str, Any]) -> None:
    from core.db import db_conn, ensure_ghost_state

    with db_conn() as conn:
        cur = conn.cursor()
        ensure_ghost_state(cur)
        cur.execute(
            "INSERT INTO ghost_state(key,val) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET val=EXCLUDED.val",
            (_STATE_KEY, json.dumps(payload)[:200000]),
        )


def run_one_sweep(script: str, *, timeout_s: Optional[int] = None) -> Dict[str, Any]:
    """Run one sweep script as a child process and log every line it prints."""
    root = _repo_root()
    path = root / "scripts" / script
    if not path.exists():
        LOGGER.error("GEOMETRY-SWEEP %s missing at %s", script, path)
        return {"script": script, "ok": False, "reason": "script_missing"}

    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    env["PYTHONUNBUFFERED"] = "1"
    started = int(time.time())
    LOGGER.warning("GEOMETRY-SWEEP START %s (timeout=%ss)", script, timeout_s or _timeout_s())
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s or _timeout_s(),
        )
    except subprocess.TimeoutExpired:
        LOGGER.error("GEOMETRY-SWEEP TIMEOUT %s after %ss", script, timeout_s or _timeout_s())
        return {"script": script, "ok": False, "reason": "timeout",
                "duration_s": int(time.time()) - started}
    except Exception as exc:  # noqa: BLE001 - diagnostic lane, never raise into the scheduler
        LOGGER.error("GEOMETRY-SWEEP FAILED %s type=%s", script, type(exc).__name__)
        return {"script": script, "ok": False, "reason": type(exc).__name__}

    lines: List[str] = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    for line in lines[:_MAX_LOGGED_LINES]:
        # WARNING so the table survives a log level that hides INFO. This is the
        # entire point of the job -- the numbers have to reach a human.
        LOGGER.warning("GEOMETRY-SWEEP | %s", line)
    if len(lines) > _MAX_LOGGED_LINES:
        LOGGER.warning("GEOMETRY-SWEEP | ... %d further lines suppressed",
                       len(lines) - _MAX_LOGGED_LINES)
    if proc.returncode != 0:
        for line in (proc.stderr or "").splitlines()[-40:]:
            LOGGER.error("GEOMETRY-SWEEP ERR | %s", line)

    duration = int(time.time()) - started
    LOGGER.warning("GEOMETRY-SWEEP DONE %s rc=%s lines=%d duration=%ss",
                   script, proc.returncode, len(lines), duration)
    return {
        "script": script,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "lines": len(lines),
        "duration_s": duration,
        "tail": lines[-60:],
    }


def run_geometry_sweep_once() -> Dict[str, Any]:
    """Scheduled entry point. Idempotent, lock-respecting, self-retiring."""
    if not _enabled():
        return {"status": "disabled", "version": SWEEP_VERSION}

    marker = sweep_marker()
    if marker:
        # Cheap no-op on every later tick. Deliberately silent: this fires every
        # cycle for the life of the deployment and must not fill the log.
        return {"status": "already_run", "version": SWEEP_VERSION,
                "ran_at": marker.get("finished_at")}

    try:
        from wolf_app import _RETRAIN_JOB_LOCK
    except Exception:  # noqa: BLE001 - importable standalone for tests
        _RETRAIN_JOB_LOCK = None

    if _RETRAIN_JOB_LOCK is not None and not _RETRAIN_JOB_LOCK.acquire(blocking=False):
        LOGGER.warning("GEOMETRY-SWEEP deferred — a retrain holds the lock; "
                       "will retry on the next cycle")
        return {"status": "deferred", "reason": "retrain_in_progress",
                "version": SWEEP_VERSION}

    started = int(time.time())
    results: List[Dict[str, Any]] = []
    try:
        for script in SWEEPS:
            results.append(run_one_sweep(script))
    finally:
        if _RETRAIN_JOB_LOCK is not None:
            _RETRAIN_JOB_LOCK.release()

    payload = {
        "version": SWEEP_VERSION,
        "started_at": started,
        "finished_at": int(time.time()),
        "duration_s": int(time.time()) - started,
        "results": results,
        "ok": all(r.get("ok") for r in results),
        "note": ("Read-only stop-geometry sweep. Ran in a child process so its "
                 "V3_STOP_VOL_MULT writes could not reach the live engine."),
    }
    # Written whatever happened. A sweep that failed and retried every cycle
    # would be a self-inflicted load generator on a live trading engine.
    try:
        _store_marker(payload)
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("GEOMETRY-SWEEP marker not stored type=%s — it may re-run",
                     type(exc).__name__)
    LOGGER.warning("GEOMETRY-SWEEP COMPLETE ok=%s duration=%ss — read the "
                   "'GEOMETRY-SWEEP |' lines above for the tables",
                   payload["ok"], payload["duration_s"])
    return payload
