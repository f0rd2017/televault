"""Tests for AdaptiveRateLimiter and TransferProgressAggregator."""

from __future__ import annotations

import time

import pytest

from app.core.rate_limiter import AdaptiveRateLimiter
from app.core.transfer_progress import TransferProgressAggregator


# ── AdaptiveRateLimiter ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_acquire_succeeds_with_tokens() -> None:
    limiter = AdaptiveRateLimiter(initial_rate=10.0, min_rate=1.0, max_rate=20.0)
    await limiter.acquire()


@pytest.mark.asyncio
async def test_acquire_waits_when_depleted() -> None:
    limiter = AdaptiveRateLimiter(initial_rate=2.0, min_rate=0.5, max_rate=10.0)
    for _ in range(3):
        await limiter.acquire()
    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.05


def test_record_flood_wait_decreases_rate() -> None:
    limiter = AdaptiveRateLimiter(initial_rate=10.0, min_rate=1.0, max_rate=20.0)
    before = limiter.snapshot()["rate"]
    limiter.record_flood_wait(5.0)
    after = limiter.snapshot()["rate"]
    assert after < before


def test_rate_cannot_exceed_max() -> None:
    limiter = AdaptiveRateLimiter(initial_rate=5.0, min_rate=1.0, max_rate=10.0)
    for _ in range(100):
        time.sleep(0.02)
        limiter.record_success()
    assert limiter.snapshot()["rate"] <= 10.0


def test_rate_cannot_go_below_min() -> None:
    limiter = AdaptiveRateLimiter(initial_rate=5.0, min_rate=2.0, max_rate=10.0)
    for _ in range(50):
        limiter.record_flood_wait(10.0)
    assert limiter.snapshot()["rate"] >= 2.0


def test_snapshot_contents() -> None:
    limiter = AdaptiveRateLimiter(initial_rate=8.0, min_rate=1.0, max_rate=16.0)
    s = limiter.snapshot()
    assert s["rate"] == 8.0
    assert s["min_rate"] == 1.0
    assert s["max_rate"] == 16.0
    assert s["flood_wait_count"] == 0
    assert s["flood_wait_seconds"] == 0.0


def test_flood_wait_tracks_count() -> None:
    limiter = AdaptiveRateLimiter(initial_rate=10.0, min_rate=1.0, max_rate=20.0)
    limiter.record_flood_wait(3.0)
    limiter.record_flood_wait(2.0)
    s = limiter.snapshot()
    assert s["flood_wait_count"] == 2
    assert s["flood_wait_seconds"] == 5.0


# ── TransferProgressAggregator ──────────────────────────────────────


def test_human_speed_boundaries() -> None:
    assert TransferProgressAggregator._human_speed(500) == "500 B/s"
    assert TransferProgressAggregator._human_speed(1500) == "1.5 KB/s"
    result = TransferProgressAggregator._human_speed(5 * 1024 * 1024)
    assert "MB/s" in result
