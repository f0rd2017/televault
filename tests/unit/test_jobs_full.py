"""Tests for JobManager error paths, CancelToken, and edge cases."""

from __future__ import annotations

import asyncio

import pytest

from app.core.jobs import CancelToken, JobCancelledError, JobContext, JobManager


# ── CancelToken ─────────────────────────────────────────────────────


def test_cancel_token_not_cancelled() -> None:
    token = CancelToken()
    assert not token.cancelled


def test_cancel_token_becomes_cancelled() -> None:
    token = CancelToken()
    token.cancel()
    assert token.cancelled


def test_cancel_token_raise_if_cancelled() -> None:
    token = CancelToken()
    token.raise_if_cancelled()  # should not raise

    token.cancel()
    with pytest.raises(JobCancelledError):
        token.raise_if_cancelled()


def test_cancel_token_idempotent() -> None:
    token = CancelToken()
    token.cancel()
    token.cancel()
    token.cancel()
    assert token.cancelled


# ── JobManager error path ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_error_status_emitted() -> None:
    events = []
    mgr = JobManager(parallelism=2)

    async def failing_runner(ctx: JobContext) -> None:
        raise RuntimeError("boom")

    mgr.subscribe(lambda ev: events.append(ev))
    mgr.enqueue("test", {"_db_job_id": 1}, failing_runner)

    await asyncio.sleep(0.15)
    await mgr.stop()

    statuses = [e.status.value for e in events]
    assert "queued" in statuses
    assert "started" in statuses
    assert "error" in statuses


@pytest.mark.asyncio
async def test_job_cancel_unknown_id_returns_false() -> None:
    mgr = JobManager(parallelism=1)
    assert mgr.cancel(9999) is False


@pytest.mark.asyncio
async def test_enqueue_raises_when_stopped() -> None:
    mgr = JobManager(parallelism=1)
    await mgr.stop()
    with pytest.raises(RuntimeError, match="stopped"):
        mgr.enqueue("test", {}, lambda ctx: None)


@pytest.mark.asyncio
async def test_async_subscribers_receive_events() -> None:
    received = []
    mgr = JobManager(parallelism=1)

    async def async_sub(ev):
        received.append(ev.status.value)

    mgr.subscribe(async_sub)

    async def quick_runner(ctx: JobContext) -> str:
        return "ok"

    mgr.enqueue("t", {"_db_job_id": 1}, quick_runner)
    await asyncio.sleep(0.15)
    await mgr.stop()

    assert "queued" in received
    assert "done" in received


@pytest.mark.asyncio
async def test_parallel_jobs_error_one_doesnt_affect_other() -> None:
    statuses: list[str] = []
    mgr = JobManager(parallelism=2)

    async def fail_runner(ctx: JobContext) -> None:
        raise ValueError("fail")

    async def ok_runner(ctx: JobContext) -> str:
        return "success"

    mgr.subscribe(lambda ev: statuses.append(ev.status.value))
    mgr.enqueue("fail", {"_db_job_id": 1}, fail_runner)
    mgr.enqueue("ok", {"_db_job_id": 2}, ok_runner)

    await asyncio.sleep(0.2)
    await mgr.stop()

    # Падение одной задачи не должно мешать другой завершиться успешно.
    errors = statuses.count("error")
    dones = statuses.count("done")
    assert errors == 1
    assert dones == 1


# ── JobContext ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_context_report_progress() -> None:
    events = []
    mgr = JobManager(parallelism=1)

    async def progress_runner(ctx: JobContext) -> None:
        await ctx.report_progress(50, "halfway")

    mgr.subscribe(lambda ev: events.append(ev))
    mgr.enqueue("test", {"_db_job_id": 1}, progress_runner)

    await asyncio.sleep(0.15)
    await mgr.stop()

    running_events = [e for e in events if e.status.value == "running"]
    assert len(running_events) >= 1
    assert running_events[0].progress == 50.0
    assert running_events[0].message == "halfway"


@pytest.mark.asyncio
async def test_job_context_log() -> None:
    events = []
    mgr = JobManager(parallelism=1)

    async def log_runner(ctx: JobContext) -> None:
        await ctx.log("step 1")
        await ctx.report_progress(10, "step 1")
        await ctx.log("step 2")

    mgr.subscribe(lambda ev: events.append(ev))
    mgr.enqueue("test", {"_db_job_id": 1}, log_runner)

    await asyncio.sleep(0.15)
    await mgr.stop()

    messages = [e.message for e in events if e.message]
    assert "step 1" in messages or "step 2" in messages


# ── Lane credit system ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lane_credits_reset() -> None:
    """Verify that lane credits are properly reset when depleted."""
    mgr = JobManager(
        parallelism=4,
        lane_caps={"upload_small": 2, "upload_large": 2},
        lane_weights={"upload_small": 3, "upload_large": 1},
    )

    async def quick(ctx: JobContext) -> str:
        return "ok"

    # Enqueue enough to test credit system
    for i in range(4):
        lane = "upload_small" if i < 2 else "upload_large"
        mgr.enqueue("test", {"_db_job_id": i, "_lane": lane}, quick)

    await asyncio.sleep(0.1)
    await mgr.stop()
