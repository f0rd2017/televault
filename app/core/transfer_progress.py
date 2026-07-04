from __future__ import annotations

import asyncio
import threading
import time
from typing import Awaitable, Callable


class TransferProgressAggregator:
    def __init__(
        self,
        total_parts: int,
        total_bytes_hint: int | None,
        emit_interval_ms: int = 120,
        percent_step: float = 1.0,
        activity: str = "Progress",
        source_bytes_hint: int | None = None,
    ) -> None:
        self.total_parts = max(1, int(total_parts))
        self._emit_interval_sec = max(0.04, int(emit_interval_ms) / 1000.0)
        self._percent_step = max(0.1, float(percent_step))
        self._activity = str(activity or "Progress")

        hint = max(0, int(total_bytes_hint or 0))
        # effective/wire ratio: how much bigger the source data volume is than
        # what actually goes over the wire (compression/dedup). 1.0 = no gain,
        # in which case the second metric isn't shown (e.g. during downloads).
        source_hint = max(0, int(source_bytes_hint or 0))
        self._effective_ratio = (
            (source_hint / hint) if (source_hint > hint > 0) else 1.0
        )
        self._hint_expected_bytes = hint
        self._known_expected_bytes = 0
        self._total_expected_bytes = max(1, hint)
        self._total_current_bytes = 0
        self._expected_by_part: dict[int, int] = {}
        self._current_by_part: dict[int, int] = {}

        self._dispatch_cb: Callable[[float, str], Awaitable[None]] | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._dirty = False
        self._last_percent = -1.0
        self._last_emit_ts = 0.0
        self._started_ts = 0.0
        self._last_speed_ts = 0.0
        self._last_speed_bytes = 0
        self._smoothed_speed_bps = 0.0
        self._lock = threading.Lock()

    async def start(self, dispatch_cb: Callable[[float, str], Awaitable[None]]) -> None:
        self._dispatch_cb = dispatch_cb
        self._stop_event.clear()
        now = time.monotonic()
        self._last_emit_ts = now
        self._started_ts = now
        self._last_speed_ts = now
        self._last_speed_bytes = 0
        self._smoothed_speed_bps = 0.0
        if self._runner_task is None:
            self._runner_task = asyncio.create_task(
                self._run(), name="transfer-progress-aggregator"
            )

    async def stop(self, final_message: str | None = None) -> None:
        self._stop_event.set()
        if self._runner_task is not None:
            try:
                await self._runner_task
            except asyncio.CancelledError:
                pass
            self._runner_task = None

        await self._emit_if_needed(force=True)
        if final_message and self._dispatch_cb is not None:
            await self._dispatch_cb(100.0, final_message)
            self._last_percent = 100.0
            self._last_emit_ts = time.monotonic()

    def on_part_progress(self, part_id: int, current: int, total: int) -> None:
        pid = int(part_id)
        cur = max(0, int(current))
        tot = max(0, int(total))
        with self._lock:
            prev_total = self._expected_by_part.get(pid, 0)
            if tot > prev_total:
                self._expected_by_part[pid] = tot
                self._known_expected_bytes += tot - prev_total
                recalculated = max(
                    self._hint_expected_bytes, self._known_expected_bytes
                )
                if recalculated != self._total_expected_bytes:
                    self._total_expected_bytes = recalculated
                    self._dirty = True

            limit = max(cur, self._expected_by_part.get(pid, 0))
            bounded = min(cur, limit) if limit > 0 else cur
            prev_current = self._current_by_part.get(pid, 0)
            if bounded < prev_current:
                bounded = prev_current
            if bounded != prev_current:
                self._current_by_part[pid] = bounded
                self._total_current_bytes += bounded - prev_current
                self._dirty = True

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._emit_interval_sec
                )
            except asyncio.TimeoutError:
                pass
            await self._emit_if_needed(force=False)

    async def _emit_if_needed(self, force: bool) -> None:
        dispatch = self._dispatch_cb
        if dispatch is None:
            return

        with self._lock:
            dirty = self._dirty
            total_current = self._total_current_bytes
            total_expected = max(1, self._total_expected_bytes)

        if not force and not dirty:
            return

        percent = (float(total_current) / float(total_expected)) * 100.0
        percent = max(0.0, min(100.0, percent))
        now = time.monotonic()

        should_emit = force
        if not should_emit:
            if self._last_percent < 0:
                should_emit = True
            elif percent >= self._last_percent + self._percent_step:
                should_emit = True
            elif (
                now - self._last_emit_ts
            ) >= self._emit_interval_sec and percent != self._last_percent:
                should_emit = True

        if not should_emit:
            return

        delta_t_raw = now - self._last_speed_ts
        delta_t = max(1e-6, delta_t_raw)
        delta_bytes = max(0, total_current - self._last_speed_bytes)
        if delta_t_raw < 0.08:
            # Very short windows create noisy spikes; seed from global average early on.
            inst_speed = float(total_current) / max(1e-6, now - self._started_ts)
        else:
            inst_speed = float(delta_bytes) / delta_t
        if delta_bytes <= 0 and self._smoothed_speed_bps > 0.0:
            self._smoothed_speed_bps *= 0.9
        if self._smoothed_speed_bps <= 0.0:
            self._smoothed_speed_bps = inst_speed
        else:
            self._smoothed_speed_bps = (0.35 * inst_speed) + (
                0.65 * self._smoothed_speed_bps
            )
        self._last_speed_ts = now
        self._last_speed_bytes = total_current

        elapsed = max(1e-6, now - self._started_ts)
        avg_speed = float(total_current) / elapsed
        speed_bps = (
            self._smoothed_speed_bps if self._smoothed_speed_bps > 0.0 else avg_speed
        )
        wire_text = self._human_speed(speed_bps)
        if self._effective_ratio > 1.0001:
            eff_text = self._human_speed(speed_bps * self._effective_ratio)
            speed_text = f"{wire_text} wire | {eff_text} eff"
        else:
            speed_text = wire_text

        if percent < 1.0:
            percent_text = f"{percent:.1f}%"
        else:
            percent_text = f"{int(percent)}%"
        # Reset _dirty before dispatch so any progress arriving during the await
        # sets it back to True and is picked up in the next emit cycle.
        with self._lock:
            self._dirty = False
        await dispatch(percent, f"{self._activity} {percent_text} | {speed_text}")
        self._last_percent = percent
        self._last_emit_ts = now

    @staticmethod
    def _human_speed(speed_bps: float) -> str:
        speed = max(0.0, float(speed_bps))
        if speed < 1024:
            return f"{speed:.0f} B/s"
        if speed < 1024 * 1024:
            return f"{speed / 1024.0:.1f} KB/s"
        return f"{speed / (1024.0 * 1024.0):.2f} MB/s"
