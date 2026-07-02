"""Адаптивные контроллеры параллелизма для загрузки/выгрузки.

Контроллеры самодостаточны: реагируют на флуд-вейты и скорость, поднимая/снижая
конкурентность. Никакой зависимости от TgUploader/TgDownloader — только asyncio,
время и токен отмены.
"""

from __future__ import annotations

import asyncio
import time

from app.core.jobs import CancelToken


class _AdaptiveUploadController:
    def __init__(
        self,
        *,
        initial_concurrency: int,
        max_concurrency: int,
        is_premium: bool,
        min_concurrency: int = 1,
    ) -> None:
        self._target_concurrency = max(1, int(initial_concurrency))
        self._max_concurrency = max(1, int(max_concurrency))
        self._min_concurrency = max(1, min(int(min_concurrency), self._max_concurrency))
        if self._target_concurrency < self._min_concurrency:
            self._target_concurrency = self._min_concurrency
        self._condition = asyncio.Condition()
        self._active_slots = 0

        self._initial_concurrency = self._target_concurrency
        self._stable_no_flood_samples = 0
        self._sample_count = 0
        self._flood_wait_count = 0
        self._flood_wait_seconds = 0.0
        self._flood_streak = 0
        self._low_speed_streak = 0
        self._ema_speed_mbps = 0.0
        self._last_flood_ts = 0.0
        self._last_downscale_ts = 0.0
        self._adjustments: list[str] = []

        self._upscale_speed_threshold = 5.5 if is_premium else 2.5
        self._downscale_speed_threshold = (
            0.3 if is_premium else 0.15
        )  # Оптимизация: было 1.4/0.7, не снижать concurrency при нормальной работе через прокси
        self._low_speed_guard_mbps = (
            max(2.4, self._downscale_speed_threshold * 1.8)
            if is_premium
            else max(1.2, self._downscale_speed_threshold * 1.8)
        )
        self._flood_cooldown_seconds = 8.0
        self._probe_interval_seconds = 9.0
        self._min_downscale_interval_seconds = (
            30.0 if is_premium else 30.0
        )  # Оптимизация: было 9.0/11.0, реже снижать
        self._last_adjust_ts = 0.0

    async def acquire_slot(self, cancel_token: CancelToken) -> None:
        while True:
            cancel_token.raise_if_cancelled()
            async with self._condition:
                if self._active_slots < self._target_concurrency:
                    self._active_slots += 1
                    return
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue

    async def release_slot(self) -> None:
        async with self._condition:
            if self._active_slots > 0:
                self._active_slots -= 1
            self._condition.notify_all()

    def record_sample(
        self,
        *,
        sent_bytes: int,
        elapsed_seconds: float,
        flood_wait_count: int = 0,
        flood_wait_seconds: float = 0.0,
        flood_wait_live_recorded: bool = False,
    ) -> None:
        elapsed = max(0.001, float(elapsed_seconds))
        speed_mbps = float(max(0, int(sent_bytes))) / elapsed / (1024.0 * 1024.0)

        self._sample_count += 1
        if speed_mbps > 0:
            if self._ema_speed_mbps <= 0:
                self._ema_speed_mbps = speed_mbps
            else:
                self._ema_speed_mbps = self._ema_speed_mbps * 0.7 + speed_mbps * 0.3

        if flood_wait_count > 0:
            if not flood_wait_live_recorded:
                self._flood_wait_count += int(flood_wait_count)
                self._flood_wait_seconds += float(max(0.0, flood_wait_seconds))
                self._stable_no_flood_samples = 0
                self._last_flood_ts = time.monotonic()
                self._flood_streak += 1
                self._low_speed_streak = 0
                should_decrease = (
                    self._flood_streak >= 3 or float(flood_wait_seconds) >= 3.0
                )
                if should_decrease and self._can_downscale():
                    self._decrease(reason=f"floodwait x{flood_wait_count}")
            return

        self._flood_streak = 0
        if speed_mbps > 0 and speed_mbps < self._downscale_speed_threshold:
            self._low_speed_streak += 1
        else:
            self._low_speed_streak = 0

        if self._low_speed_streak >= 3 and self._can_downscale():
            # Avoid shrinking part parallelism on short latency spikes when aggregate
            # throughput is still healthy.
            if self._ema_speed_mbps > self._low_speed_guard_mbps:
                self._low_speed_streak = 0
            else:
                before = self._target_concurrency
                self._target_concurrency -= 1
                self._record_adjustment(
                    "downscale low-speed "
                    f"{speed_mbps:.2f} MB/s ema={self._ema_speed_mbps:.2f} "
                    f"({before}->{self._target_concurrency})"
                )
                now = time.monotonic()
                self._last_adjust_ts = now
                self._last_downscale_ts = now
                self._stable_no_flood_samples = 0
                self._low_speed_streak = 0
                return

        self._stable_no_flood_samples += 1
        if self._stable_no_flood_samples < 3:
            return
        if self._target_concurrency >= self._max_concurrency:
            return
        now = time.monotonic()
        if (now - self._last_flood_ts) < self._flood_cooldown_seconds:
            return
        if self._ema_speed_mbps < self._upscale_speed_threshold:
            # Probe occasionally so controller can recover after conservative downscale.
            if (now - self._last_adjust_ts) < self._probe_interval_seconds:
                return

        before = self._target_concurrency
        self._target_concurrency += 1
        self._record_adjustment(
            f"upscale concurrency {before}->{self._target_concurrency}"
        )
        self._last_adjust_ts = now
        self._stable_no_flood_samples = 0

    def record_flood_wait(self, wait_seconds: float) -> None:
        wait = float(max(0.0, wait_seconds))
        self._flood_wait_count += 1
        self._flood_wait_seconds += wait
        self._stable_no_flood_samples = 0
        self._last_flood_ts = time.monotonic()
        self._flood_streak += 1
        self._low_speed_streak = 0
        should_decrease = self._flood_streak >= 2 or wait >= 2.0
        if should_decrease and self._can_downscale():
            self._decrease(reason=f"live floodwait {wait:.0f}s")

    def summary(self) -> dict[str, object]:
        return {
            "initial_concurrency": int(self._initial_concurrency),
            "min_concurrency": int(self._min_concurrency),
            "final_concurrency": int(self._target_concurrency),
            "max_concurrency": int(self._max_concurrency),
            "samples": int(self._sample_count),
            "ema_speed_mbps": float(self._ema_speed_mbps),
            "flood_wait_count": int(self._flood_wait_count),
            "flood_wait_seconds": float(self._flood_wait_seconds),
            "adjustments": list(self._adjustments),
        }

    def state(self) -> dict[str, int]:
        return {
            "target_concurrency": int(self._target_concurrency),
            "active_slots": int(self._active_slots),
            "max_concurrency": int(self._max_concurrency),
            "min_concurrency": int(self._min_concurrency),
        }

    def _decrease(self, reason: str) -> None:
        if self._target_concurrency <= self._min_concurrency:
            return
        before = self._target_concurrency
        self._target_concurrency -= 1
        now = time.monotonic()
        self._last_adjust_ts = now
        self._last_downscale_ts = now
        self._record_adjustment(f"{reason}: {before}->{self._target_concurrency}")

    def _can_downscale(self) -> bool:
        if self._target_concurrency <= self._min_concurrency:
            return False
        now = time.monotonic()
        return (now - self._last_downscale_ts) >= self._min_downscale_interval_seconds

    def _record_adjustment(self, message: str) -> None:
        if len(self._adjustments) >= 12:
            return
        self._adjustments.append(message)


class _AdaptiveDownloadController:
    def __init__(
        self,
        *,
        initial_part_concurrency: int,
        max_part_concurrency: int,
        initial_stride_streams: int,
        max_stride_streams: int,
        total_stream_budget: int,
        is_premium: bool,
    ) -> None:
        self._target_part_concurrency = max(1, int(initial_part_concurrency))
        self._max_part_concurrency = max(1, int(max_part_concurrency))
        self._target_stride_streams = max(1, int(initial_stride_streams))
        self._max_stride_streams = max(1, int(max_stride_streams))
        self._total_stream_budget = max(1, int(total_stream_budget))
        self._condition = asyncio.Condition()
        self._active_slots = 0

        self._initial_part_concurrency = self._target_part_concurrency
        self._initial_stride_streams = self._target_stride_streams
        self._stable_no_flood_samples = 0
        self._sample_count = 0
        self._flood_wait_count = 0
        self._flood_wait_seconds = 0.0
        self._ema_speed_mbps = 0.0
        self._last_flood_ts = 0.0
        self._adjustments: list[str] = []
        self._low_speed_streak = 0
        self._last_adjust_ts = 0.0
        self._last_downscale_ts = 0.0

        self._upscale_speed_threshold = 4.5 if is_premium else 2.0
        self._downscale_speed_threshold = 0.9 if is_premium else 0.45
        self._flood_cooldown_seconds = 8.0
        self._probe_interval_seconds = 9.0
        self._min_downscale_interval_seconds = 4.0

    async def acquire_slot(self, cancel_token: CancelToken) -> None:
        while True:
            cancel_token.raise_if_cancelled()
            async with self._condition:
                if self._active_slots < self._target_part_concurrency:
                    self._active_slots += 1
                    return
                # Wait for notification when target concurrency changes or a slot is released
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue

    async def release_slot(self) -> None:
        async with self._condition:
            if self._active_slots > 0:
                self._active_slots -= 1
            self._condition.notify_all()

    def snapshot(self) -> tuple[int, int]:
        part_concurrency = max(1, int(self._target_part_concurrency))
        budgeted_streams = max(1, int(self._total_stream_budget) // part_concurrency)
        stride_streams = max(1, min(int(self._target_stride_streams), budgeted_streams))
        return part_concurrency, stride_streams

    def record_sample(self, stats: dict[str, object] | None) -> None:
        if not isinstance(stats, dict):
            return

        flood_count = int(max(0, int(stats.get("flood_wait_count") or 0)))
        flood_seconds = float(max(0.0, float(stats.get("flood_wait_seconds") or 0.0)))
        flood_wait_live_recorded = bool(stats.get("flood_wait_live_recorded"))
        elapsed = float(max(0.001, float(stats.get("elapsed_seconds") or 0.0)))
        downloaded = int(max(0, int(stats.get("downloaded_bytes") or 0)))
        speed_mbps = float(downloaded) / elapsed / (1024.0 * 1024.0)

        self._sample_count += 1
        if speed_mbps > 0:
            if self._ema_speed_mbps <= 0:
                self._ema_speed_mbps = speed_mbps
            else:
                self._ema_speed_mbps = self._ema_speed_mbps * 0.7 + speed_mbps * 0.3

        if flood_count > 0:
            if not flood_wait_live_recorded:
                self._flood_wait_count += flood_count
                self._flood_wait_seconds += flood_seconds
            self._stable_no_flood_samples = 0
            self._last_flood_ts = time.monotonic()
            if not flood_wait_live_recorded:
                self._decrease_parallelism(reason=f"floodwait x{flood_count}")
            return

        if speed_mbps > 0 and speed_mbps < self._downscale_speed_threshold:
            self._low_speed_streak += 1
        else:
            self._low_speed_streak = 0

        now = time.monotonic()
        if (
            self._low_speed_streak >= 2
            and self._target_part_concurrency > 1
            and (now - self._last_downscale_ts) >= self._min_downscale_interval_seconds
        ):
            self._decrease_parallelism(
                f"low-speed streak {speed_mbps:.2f} MB/s x{self._low_speed_streak}"
            )
            self._stable_no_flood_samples = 0
            self._low_speed_streak = 0
            return

        self._stable_no_flood_samples += 1
        if self._stable_no_flood_samples < 3:
            return

        now = time.monotonic()
        if (now - self._last_flood_ts) < self._flood_cooldown_seconds:
            return

        if self._ema_speed_mbps < self._upscale_speed_threshold:
            # Probe occasionally so the controller can recover after a downscale.
            if (now - self._last_adjust_ts) < self._probe_interval_seconds:
                return

        if self._target_part_concurrency < self._max_part_concurrency:
            before = self._target_part_concurrency
            self._target_part_concurrency += 1
            self._last_adjust_ts = now
            self._record_adjustment(
                f"upscale part concurrency {before}->{self._target_part_concurrency}"
            )
            self._stable_no_flood_samples = 0
            return

        if self._target_stride_streams < self._max_stride_streams:
            before = self._target_stride_streams
            self._target_stride_streams += 1
            self._last_adjust_ts = now
            self._record_adjustment(
                f"upscale stride streams {before}->{self._target_stride_streams}"
            )
            self._stable_no_flood_samples = 0

    def summary(self) -> dict[str, object]:
        _, effective_stride = self.snapshot()
        return {
            "initial_part_concurrency": int(self._initial_part_concurrency),
            "initial_stride_streams": int(self._initial_stride_streams),
            "final_part_concurrency": int(self._target_part_concurrency),
            "final_stride_streams": int(self._target_stride_streams),
            "effective_stride_streams": int(effective_stride),
            "samples": int(self._sample_count),
            "ema_speed_mbps": float(self._ema_speed_mbps),
            "flood_wait_count": int(self._flood_wait_count),
            "flood_wait_seconds": float(self._flood_wait_seconds),
            "adjustments": list(self._adjustments),
        }

    def record_flood_wait(self, wait_seconds: float) -> None:
        wait = max(0.0, float(wait_seconds))
        self._flood_wait_count += 1
        self._flood_wait_seconds += wait
        self._stable_no_flood_samples = 0
        self._last_flood_ts = time.monotonic()
        self._decrease_parallelism(reason=f"live floodwait {wait:.0f}s")

    def state(self) -> dict[str, int]:
        _, effective_stride = self.snapshot()
        return {
            "target_part_concurrency": int(self._target_part_concurrency),
            "target_stride_streams": int(self._target_stride_streams),
            "effective_stride_streams": int(effective_stride),
            "active_slots": int(self._active_slots),
            "max_part_concurrency": int(self._max_part_concurrency),
            "max_stride_streams": int(self._max_stride_streams),
        }

    def _decrease_parallelism(self, reason: str) -> None:
        changed = False
        before_part = self._target_part_concurrency
        before_stride = self._target_stride_streams

        if self._target_part_concurrency > 1:
            self._target_part_concurrency -= 1
            changed = True
        if self._target_stride_streams > 1:
            self._target_stride_streams -= 1
            changed = True

        if changed:
            now = time.monotonic()
            self._last_adjust_ts = now
            self._last_downscale_ts = now
            self._record_adjustment(
                (
                    f"{reason}: part {before_part}->{self._target_part_concurrency}, "
                    f"stride {before_stride}->{self._target_stride_streams}"
                )
            )

    def _record_adjustment(self, message: str) -> None:
        if len(self._adjustments) >= 10:
            return
        self._adjustments.append(message)
