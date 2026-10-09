"""Scheduler timeout behavior for non-cancellable synchronous workers."""
import asyncio
import time

from core import scheduler
from core.scheduler import Task, _run_task


def test_sync_timeout_blocks_overlap_until_worker_really_finishes():
    def slow_job():
        time.sleep(0.05)

    task = Task(name="slow", fn=slow_job, interval_s=1, timeout_s=0.01)

    async def exercise():
        runner = asyncio.create_task(_run_task(task))
        await asyncio.sleep(0.02)
        assert task.running is True
        await runner

    asyncio.run(exercise())
    assert task.running is False
    assert task.timeout_count == 1
    assert task.run_count == 1


def test_register_delays_first_run_by_default(monkeypatch):
    now = time.time()
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "_tasks", {})

    scheduler.register("delayed", lambda: None, interval_s=300)

    assert scheduler._tasks["delayed"].next_run_at == now + 300


def test_register_accepts_controlled_initial_delay(monkeypatch):
    now = time.time()
    monkeypatch.setattr(scheduler.time, "time", lambda: now)
    monkeypatch.setattr(scheduler, "_tasks", {})

    scheduler.register("early", lambda: None, interval_s=300, initial_delay_s=45)

    assert scheduler._tasks["early"].next_run_at == now + 45


def test_aligned_tasks_land_on_wall_clock_boundaries():
    """2026-10-09: the 5-min edge tick ran at :x4:20/:x9:20 (deploy phase), so the 09:45 ET radar
    window's first tick came at 09:49:20. Aligned, it lands at :x0:05/:x5:05 whatever the deploy time."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from core import scheduler as S
    et = ZoneInfo("America/New_York")
    t = datetime(2026, 10, 9, 9, 44, 20, tzinfo=et).timestamp()
    nxt = S._next_aligned(t, 300, 5)
    assert datetime.fromtimestamp(nxt, tz=et).strftime("%H:%M:%S") == "09:45:05"
    assert S._next_aligned(nxt, 300, 5) - nxt == 300                      # strictly after a boundary
    task = S.Task(name="x", fn=lambda: None, interval_s=300, align_s=300, offset_s=5)
    assert datetime.fromtimestamp(S._next_due(task, t), tz=et).strftime("%H:%M:%S") == "09:45:05"
    plain = S.Task(name="y", fn=lambda: None, interval_s=300)
    assert S._next_due(plain, t) == t + 300                                 # unaligned tasks unchanged


def test_register_aligns_the_first_run(monkeypatch):
    from core import scheduler as S
    monkeypatch.setattr(S, "_tasks", {})
    monkeypatch.setattr(S.time, "time", lambda: 1_000_000_123.0)
    S.register("al", lambda: None, interval_s=300, initial_delay_s=60, align_s=300, offset_s=5)
    first = S._tasks["al"].next_run_at
    assert first >= 1_000_000_183.0 and (first - 5) % 300 == 0


def test_the_edge_tick_is_registered_on_the_clock():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "wolf_app.py").read_text()
    block = src[src.index("scheduler.register(\n            \"edge_shadow\""):][:800]
    assert "align_s=300" in block and "offset_s=5" in block
