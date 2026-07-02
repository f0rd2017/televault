from __future__ import annotations

import asyncio

import pytest

from app.core.rate_limiter import AdaptiveRateLimiter


@pytest.mark.asyncio
async def test_adaptive_rate_limiter_backoff_and_recover() -> None:
    limiter = AdaptiveRateLimiter(
        initial_rate=10.0,
        min_rate=1.0,
        max_rate=20.0,
        window_sec=0.2,
    )

    await limiter.acquire()
    baseline = limiter.snapshot()["rate"]
    assert isinstance(baseline, float)
    assert baseline >= 10.0

    limiter.record_flood_wait(3.0)
    lowered = limiter.snapshot()["rate"]
    assert isinstance(lowered, float)
    assert lowered < baseline

    await asyncio.sleep(0.25)
    limiter.record_success()
    raised = limiter.snapshot()["rate"]
    assert isinstance(raised, float)
    assert raised >= lowered
