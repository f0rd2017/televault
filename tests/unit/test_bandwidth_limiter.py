from __future__ import annotations

import asyncio
import time

from app.core.rate_limiter import BandwidthLimiter

_MB = 1024 * 1024


def test_unlimited_is_noop_and_disabled():
    lim = BandwidthLimiter(0.0)
    assert lim.enabled is False
    assert lim.mbps == 0.0

    async def go() -> float:
        start = time.monotonic()
        # Даже гигантский запрос при выключенном лимите проходит мгновенно.
        await lim.acquire(500 * _MB)
        await lim.acquire(0)
        await lim.acquire(-5)
        return time.monotonic() - start

    assert asyncio.run(go()) < 0.1


def test_negative_mbps_treated_as_unlimited():
    lim = BandwidthLimiter(-10.0)
    assert lim.enabled is False
    asyncio.run(lim.acquire(10 * _MB))  # не виснет


def test_mbps_property():
    assert BandwidthLimiter(25.0).mbps == 25.0
    assert BandwidthLimiter(25.0).enabled is True


def test_throttle_delays_when_over_capacity():
    # rate=100 МБ/с, ёмкость=100 МБ. Осушаем бакет, затем берём ещё 40 МБ →
    # ждём ~40/100 = 0.4 с.
    lim = BandwidthLimiter(100.0)

    async def go() -> float:
        await lim.acquire(100 * _MB)  # осушает полный бакет (мгновенно)
        start = time.monotonic()
        await lim.acquire(40 * _MB)  # дефицит → ждёт ~0.4 с
        return time.monotonic() - start

    elapsed = asyncio.run(go())
    assert 0.25 <= elapsed <= 1.0


def test_huge_single_acquire_does_not_deadlock():
    # Запрос больше ёмкости бакета должен пройти (уведя токены в минус), а не виснуть.
    lim = BandwidthLimiter(50.0)

    async def go() -> float:
        start = time.monotonic()
        await lim.acquire(500 * _MB)  # >> ёмкости — но бакет полон, пропускаем
        return time.monotonic() - start

    assert asyncio.run(go()) < 0.2


def test_average_rate_is_capped():
    # Берём суммарно много байт мелкими порциями — суммарное время не меньше
    # того, что диктует скорость (грубая проверка, что троттл реально тормозит).
    lim = BandwidthLimiter(50.0)  # 50 МБ/с

    async def go() -> float:
        start = time.monotonic()
        # 100 МБ при 50 МБ/с и стартовом бакете 50 МБ → ~1 с на «лишние» 50 МБ.
        for _ in range(100):
            await lim.acquire(1 * _MB)
        return time.monotonic() - start

    elapsed = asyncio.run(go())
    assert elapsed >= 0.7
