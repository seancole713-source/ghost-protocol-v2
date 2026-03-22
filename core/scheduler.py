"""
core/scheduler.py - Single background task runner for Ghost Protocol v2.
One scheduler. All tasks registered here. No duplicates.
"""
import asyncio, logging, time
from typing import Callable, Dict, List
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
    last_error: str = ""

_tasks: Dict[str, Task] = {}
_running = False

def register(name: str, fn: Callable, interval_s: int):
    """Register a background task. Call before start()."""
    _tasks[name] = Task(name=name, fn=fn, interval_s=interval_s)
    LOGGER.info(f"Task registered: {name} every {interval_s}s")

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
                asyncio.create_task(_run_task(task))
        await asyncio.sleep(10)

async def _run_task(task: Task):
    """Run a single task, catch errors, update metadata."""
    try:
        task.last_run = time.time()
        if asyncio.iscoroutinefunction(task.fn):
            await task.fn()
        else:
            await asyncio.get_event_loop().run_in_executor(None, task.fn)
        task.run_count += 1
        LOGGER.debug(f"Task {task.name} completed (run #{task.run_count})")
    except Exception as e:
        task.error_count += 1
        task.last_error = str(e)
        LOGGER.error(f"Task {task.name} failed: {e}")

def status() -> List[dict]:
    """Return status of all tasks for health check."""
    now = time.time()
    return [{
        "name": t.name,
        "interval_s": t.interval_s,
        "last_run_ago_s": int(now - t.last_run) if t.last_run else None,
        "run_count": t.run_count,
        "error_count": t.error_count,
        "last_error": t.last_error or None,
        "healthy": t.error_count == 0 or t.run_count > t.error_count,
    } for t in _tasks.values()]

def stop():
    global _running
    _running = False
    LOGGER.info("Scheduler stopped")