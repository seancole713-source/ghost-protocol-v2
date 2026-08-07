"""Scheduler timeout behavior for non-cancellable synchronous workers."""
import asyncio
import time

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
