from __future__ import annotations

import asyncio

import pytest

from app.core.jobs import JobManager


@pytest.mark.asyncio
async def test_scheduler_lane_caps_and_fairness() -> None:
    manager = JobManager(
        parallelism=2,
        lane_caps={
            "upload_small": 1,
            "upload_large": 1,
            "download": 1,
        },
        lane_weights={
            "upload_small": 2,
            "upload_large": 1,
            "download": 1,
            "default": 1,
        },
    )

    lock = asyncio.Lock()
    active_by_lane: dict[str, int] = {"upload_small": 0, "upload_large": 0}
    peak_by_lane: dict[str, int] = {"upload_small": 0, "upload_large": 0}
    started_lanes: list[str] = []

    async def runner(ctx):
        lane = str(ctx.payload.get("_lane") or "default")
        async with lock:
            if lane in active_by_lane:
                active_by_lane[lane] += 1
                peak_by_lane[lane] = max(peak_by_lane[lane], active_by_lane[lane])
            started_lanes.append(lane)
        await asyncio.sleep(0.03)
        async with lock:
            if lane in active_by_lane:
                active_by_lane[lane] -= 1
        return {"lane": lane}

    for idx in range(4):
        manager.enqueue("upload", {"_lane": "upload_small", "n": idx}, runner)
    for idx in range(2):
        manager.enqueue("upload", {"_lane": "upload_large", "n": idx}, runner)

    await asyncio.wait_for(manager._queue.join(), timeout=3.0)
    await manager.stop()

    assert peak_by_lane["upload_small"] <= 1
    assert peak_by_lane["upload_large"] <= 1
    assert "upload_large" in started_lanes[:4]
