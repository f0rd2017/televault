import asyncio

import pytest

from app.core.jobs import JobManager
from app.core.types import JobStatus


@pytest.mark.asyncio
async def test_job_manager_done() -> None:
    manager = JobManager()
    events = []
    manager.subscribe(events.append)

    async def runner(ctx):
        await ctx.report_progress(50, "half")
        return {"ok": True}

    manager.enqueue("refresh", {}, runner)
    await asyncio.wait_for(manager._queue.join(), timeout=2)
    await asyncio.sleep(0.05)

    statuses = [event.status for event in events]
    assert JobStatus.QUEUED in statuses
    assert JobStatus.STARTED in statuses
    assert JobStatus.DONE in statuses

    await manager.stop()


@pytest.mark.asyncio
async def test_job_manager_cancel() -> None:
    manager = JobManager()
    events = []
    manager.subscribe(events.append)

    async def runner(ctx):
        await asyncio.sleep(0.05)
        ctx.cancel_token.raise_if_cancelled()
        return None

    job_id = manager.enqueue("upload", {}, runner)
    manager.cancel(job_id)

    await asyncio.wait_for(manager._queue.join(), timeout=2)
    await asyncio.sleep(0.05)

    statuses = [event.status for event in events]
    assert JobStatus.CANCELLED in statuses
    await manager.stop()


@pytest.mark.asyncio
async def test_job_manager_cancel_interrupts_blocking_runner_and_keeps_worker_alive() -> None:
    manager = JobManager(parallelism=1)
    events = []
    manager.subscribe(events.append)
    started = asyncio.Event()
    blocker = asyncio.Event()

    async def blocking_runner(_ctx):
        started.set()
        await blocker.wait()
        return None

    job_id = manager.enqueue("upload", {}, blocking_runner)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert manager.cancel(job_id) is True
    await asyncio.wait_for(manager._queue.join(), timeout=2.0)

    async def quick_runner(_ctx):
        return {"ok": True}

    followup_job_id = manager.enqueue("refresh", {}, quick_runner)
    await asyncio.wait_for(manager._queue.join(), timeout=2.0)
    await asyncio.sleep(0.05)

    by_id: dict[int, list[JobStatus]] = {}
    for event in events:
        by_id.setdefault(event.job_id, []).append(event.status)

    assert JobStatus.CANCELLED in by_id.get(job_id, [])
    assert JobStatus.DONE in by_id.get(followup_job_id, [])
    await manager.stop()


@pytest.mark.asyncio
async def test_job_manager_parallel_workers() -> None:
    manager = JobManager(parallelism=2)
    running = 0
    peak_running = 0
    lock = asyncio.Lock()

    async def runner(ctx):
        nonlocal running, peak_running
        async with lock:
            running += 1
            peak_running = max(peak_running, running)
        await asyncio.sleep(0.08)
        async with lock:
            running -= 1
        return {"ok": True}

    manager.enqueue("job-a", {}, runner)
    manager.enqueue("job-b", {}, runner)
    await asyncio.wait_for(manager._queue.join(), timeout=2)

    assert peak_running >= 2
    await manager.stop()


@pytest.mark.asyncio
async def test_job_manager_stop_emits_cancelled_for_running_and_pending() -> None:
    manager = JobManager(parallelism=1)
    events = []
    manager.subscribe(events.append)
    started = asyncio.Event()

    async def runner(ctx):
        started.set()
        while True:
            await asyncio.sleep(0.05)
            ctx.cancel_token.raise_if_cancelled()

    running_job_id = manager.enqueue("job-running", {}, runner)
    pending_job_id = manager.enqueue("job-pending", {}, runner)

    await asyncio.wait_for(started.wait(), timeout=1.0)
    await manager.stop()
    await asyncio.sleep(0.05)

    by_id: dict[int, list[JobStatus]] = {}
    for event in events:
        by_id.setdefault(event.job_id, []).append(event.status)

    assert JobStatus.CANCELLED in by_id.get(running_job_id, [])
    assert JobStatus.CANCELLED in by_id.get(pending_job_id, [])
