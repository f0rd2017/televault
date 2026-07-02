from __future__ import annotations

import asyncio
import time

_BYTES_PER_MB = 1024 * 1024


class BandwidthLimiter:
    """Async byte token-bucket — ограничивает полосу (МБ/с) загрузки/скачивания.

    В отличие от :class:`AdaptiveRateLimiter` (1 токен = 1 запрос), здесь
    1 токен = 1 байт. ``acquire(n)`` блокирует, пока не «накапает» n байт по
    заданной скорости. ``mbps <= 0`` → лимит выключен (acquire — no-op), это
    значение по умолчанию, поведение без троттлинга не меняется.

    Лимитер общий на инстанс загрузчика/скачивателя, поэтому параллельные части
    делят единый бюджет — суммарная полоса не превышает заданную.
    """

    def __init__(self, mbps: float, *, burst_sec: float = 1.0) -> None:
        self._rate = max(0.0, float(mbps)) * _BYTES_PER_MB  # байт/с (0 = без лимита)
        self._burst_sec = max(0.1, float(burst_sec))
        # Ёмкость бакета: запас на короткие всплески (но не меньше 1 МБ, чтобы
        # одиночный крупный блок не упирался в нулевую ёмкость).
        self._capacity = (
            max(self._rate * self._burst_sec, float(_BYTES_PER_MB))
            if self._rate > 0
            else 0.0
        )
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._rate > 0

    @property
    def mbps(self) -> float:
        return self._rate / _BYTES_PER_MB if self._rate > 0 else 0.0

    async def acquire(self, nbytes: int) -> None:
        if self._rate <= 0 or nbytes <= 0:
            return
        need = float(nbytes)
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self._last)
                self._last = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                # Разрешаем, если хватает токенов ИЛИ бакет уже полон (запрос
                # больше ёмкости — пропускаем, уведя токены в минус, чтобы
                # следующие acquire ждали пропорционально; иначе — дедлок).
                if self._tokens >= need or self._tokens >= self._capacity:
                    self._tokens -= need
                    return
                deficit = need - self._tokens
                sleep_time = max(0.01, deficit / self._rate)
            await asyncio.sleep(sleep_time)


class AdaptiveRateLimiter:
    """Async token-bucket limiter with flood-wait backoff and gradual recovery."""

    def __init__(
        self,
        *,
        initial_rate: float,
        min_rate: float,
        max_rate: float,
        window_sec: float = 1.0,
    ) -> None:
        self._rate = float(max(min_rate, min(max_rate, initial_rate)))
        self._min_rate = float(max(0.05, min_rate))
        self._max_rate = float(max(self._min_rate, max_rate))
        self._window_sec = float(max(0.2, window_sec))
        self._tokens = float(self._rate)
        self._last_refill = time.monotonic()
        self._last_adjust = self._last_refill
        self._last_flood = 0.0
        self._flood_wait_count = 0
        self._flood_wait_seconds = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                self._refill_locked()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self._maybe_recover_locked()
                    return
                # Use the rate at the time of the decision to sleep
                rate = max(self._rate, self._min_rate)
                sleep_time = max(0.005, 1.0 / rate)
                # Check again immediately after acquiring the lock
                self._refill_locked()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self._maybe_recover_locked()
                    return
            await asyncio.sleep(sleep_time)

    def record_success(self) -> None:
        self._recover_rate()

    def record_flood_wait(self, wait_seconds: float) -> None:
        wait = max(0.0, float(wait_seconds))
        self._flood_wait_count += 1
        self._flood_wait_seconds += wait
        self._last_flood = time.monotonic()
        self._rate = max(self._min_rate, self._rate * 0.8)
        self._last_adjust = self._last_flood
        self._tokens = min(self._tokens, self._rate)

    def snapshot(self) -> dict[str, float | int]:
        return {
            "rate": float(self._rate),
            "min_rate": float(self._min_rate),
            "max_rate": float(self._max_rate),
            "flood_wait_count": int(self._flood_wait_count),
            "flood_wait_seconds": float(self._flood_wait_seconds),
        }

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        if elapsed <= 0:
            return
        self._tokens = min(self._rate, self._tokens + (elapsed * self._rate))

    def _maybe_recover_locked(self) -> None:
        self._recover_rate()

    def _recover_rate(self) -> None:
        """Gradually bump the rate back up after a quiet window (no recent flood/adjust).

        Single-threaded asyncio with no awaits inside, so it is safe to call both
        while holding ``_lock`` (from ``acquire``) and outside it (``record_success``).
        """
        now = time.monotonic()
        if (now - self._last_flood) < self._window_sec:
            return
        if (now - self._last_adjust) < self._window_sec:
            return
        if self._rate >= self._max_rate:
            return
        self._rate = min(self._max_rate, self._rate * 1.10)
        self._last_adjust = now
        self._tokens = min(self._tokens, self._rate)
