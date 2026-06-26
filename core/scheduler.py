"""
core/scheduler.py - Single background task runner for Ghost Protocol v2.
One scheduler. All tasks registered here. No duplicates.

P2-2 (audit): per-task timeout via asyncio.wait_for prevents one slow task
from stalling all background jobs.
"""
import asyncio, logging, time
from typing import Callable, Dict, List, Optional
from dataclasses import dataclass, field

LOGGER = logging.getLogger("ghost.scheduler")

@dataclass
class Task:
    name: str
    fn: Callable
    interval_s: int
    last_run: float = 0.0
    run_count: int = 0
    error_count: int = 0
    timeout_count: int = 0
    last_error: str = ""
    # P2-2: per-task timeout (env-tunable, default 120s)
    timeout_s: Optional[float] = None
    # PR #77: overlap guard — prevents scheduler from starting a new
    # instance while the previous one is still running.
    running: bool = field(default=False, init=False)
    skipped_overlap_count: int = field(default=0, init=False)

_tasks: Dict[str, Task] = {}
_running = False

# P2-2: default task timeout
_DEFAULT_TASK_TIMEOUT_S = float(
    __import__("os").getenv("SCHEDULER_TASK_TIMEOUT_S", "120")
)


def register(name: str, fn: Callable, interval_s: int, timeout_s: Optional[float] = None):
    """Register a background task. Call before start()."""
    _tasks[name] = Task(
        name=name, fn=fn, interval_s=interval_s,
        timeout_s=timeout_s if timeout_s is not None else _DEFAULT_TASK_TIMEOUT_S,
    )
    LOGGER.info(f"Task registered: {name} every {interval_s}s timeout={_tasks[name].timeout_s}s")

def start():
    """Start the scheduler loop in a background asyncio task."""
    global _running
    if _running:
        LOGGER.warning("Scheduler already running")
        return
    _running = True
    asyncio.create_task(_loop())
    LOGGER.info(f"Scheduler started with {len(_tasks)} tasks")

async def _loop():
    """Main scheduler loop. Checks tasks every 10s."""
    while _running:
        now = time.time()
        for task in list(_tasks.values()):
            if now - task.last_run >= task.interval_s:
                if task.running:
                    task.skipped_overlap_count += 1
                    LOGGER.debug("Task %s skipped (overlap #%s)", task.name, task.skipped_overlap_count)
                    continue
                asyncio.create_task(_run_task(task))
        await asyncio.sleep(10)

async def _run_task(task: Task):
    """Run a single task with timeout, catch errors, update metadata."""
    task.running = True
    try:
        task.last_run = time.time()
        coro = task.fn() if asyncio.iscoroutinefunction(task.fn) else asyncio.get_event_loop().run_in_executor(None, task.fn)
        timeout = task.timeout_s if task.timeout_s and task.timeout_s > 0 else None
        if timeout:
            await asyncio.wait_for(coro, timeout=timeout)
        else:
            await coro
        task.run_count += 1
        LOGGER.debug(f"Task {task.name} completed (run #{task.run_count})")
    except asyncio.TimeoutError:
        task.timeout_count += 1
        task.last_error = f"timeout after {task.timeout_s}s"
        LOGGER.error(f"Task {task.name} TIMEOUT after {task.timeout_s}s (timeout #{task.timeout_count})")
    except Exception as e:
        task.error_count += 1
        task.last_error = str(e)[:200]
        LOGGER.error(f"Task {task.name} failed: {e}")
    finally:
        task.running = False

def status() -> List[dict]:
    """Return status of all tasks for health check."""
    now = time.time()
    return [{
        "name": t.name,
        "interval_s": t.interval_s,
        "timeout_s": t.timeout_s,
        "last_run_ago_s": int(now - t.last_run) if t.last_run else None,
        "run_count": t.run_count,
        "error_count": t.error_count,
        "timeout_count": t.timeout_count,
        "last_error": t.last_error or None,
        "healthy": (t.error_count == 0 and t.timeout_count == 0) or t.run_count > (t.error_count + t.timeout_count),
    } for t in _tasks.values()]

def stop():
    global _running
    _running = False
    LOGGER.info("Scheduler stopped")