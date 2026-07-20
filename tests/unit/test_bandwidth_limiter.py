from __future__ import annotations

import asyncio
import time

from televault.core.rate_limiter import BandwidthLimiter

_MB = 1024 * 1024


def test_unlimited_is_noop_and_disabled():
    lim = BandwidthLimiter(0.0)
    assert lim.enabled is False
    assert lim.mbps == 0.0

    async def go() -> float:
        start = time.monotonic()
        # Even a huge request passes instantly when the limit is off.
        await lim.acquire(500 * _MB)
        await lim.acquire(0)
        await lim.acquire(-5)
        return time.monotonic() - start

    assert asyncio.run(go()) < 0.1


def test_negative_mbps_treated_as_unlimited():
    lim = BandwidthLimiter(-10.0)
    assert lim.enabled is False
    asyncio.run(lim.acquire(10 * _MB))  # does not hang


def test_mbps_property():
    assert BandwidthLimiter(25.0).mbps == 25.0
    assert BandwidthLimiter(25.0).enabled is True


def test_throttle_delays_when_over_capacity():
    # rate=100 MB/s, capacity=100 MB. Drain the bucket, then take another 40 MB →
    # wait ~40/100 = 0.4 s.
    lim = BandwidthLimiter(100.0)

    async def go() -> float:
        await lim.acquire(100 * _MB)  # drains the full bucket (instant)
        start = time.monotonic()
        await lim.acquire(40 * _MB)  # deficit → waits ~0.4 s
        return time.monotonic() - start

    elapsed = asyncio.run(go())
    assert 0.25 <= elapsed <= 1.0


def test_huge_single_acquire_does_not_deadlock():
    # A request bigger than the bucket capacity must pass (driving tokens negative), not hang.
    lim = BandwidthLimiter(50.0)

    async def go() -> float:
        start = time.monotonic()
        await lim.acquire(500 * _MB)  # >> capacity — but the bucket is full, pass
        return time.monotonic() - start

    assert asyncio.run(go()) < 0.2


def test_average_rate_is_capped():
    # Take many bytes in small chunks — the total time is at least
    # what the rate dictates (a rough check that throttling actually slows things down).
    lim = BandwidthLimiter(50.0)  # 50 MB/s

    async def go() -> float:
        start = time.monotonic()
        # 100 MB at 50 MB/s with a starting bucket of 50 MB → ~1 s for the 'extra' 50 MB.
        for _ in range(100):
            await lim.acquire(1 * _MB)
        return time.monotonic() - start

    elapsed = asyncio.run(go())
    assert elapsed >= 0.7
