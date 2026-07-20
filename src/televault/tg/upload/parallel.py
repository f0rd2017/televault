from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from pathlib import Path
import time

from telethon import functions, helpers, types
from telethon.errors import FloodWaitError
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from televault.core.jobs import CancelToken
from televault.tg.adaptive import _AdaptiveUploadController
from televault.tg.upload._common import _is_retryable_error

logger = logging.getLogger(__name__)


class _ParallelUploadMixin:
    async def _iter_file_chunks(
        self,
        file_path: Path,
        part_size: int,
        part_count: int,
        cancel_token: CancelToken,
    ) -> AsyncIterator[tuple[int, bytes]]:
        """Yield (part_index, chunk) reading the file sequentially off-thread."""
        with file_path.open("rb") as src:
            for part_index in range(part_count):
                cancel_token.raise_if_cancelled()
                chunk = await asyncio.to_thread(src.read, part_size)
                if not chunk:
                    break
                yield part_index, chunk

    async def _iter_bytes_chunks(
        self,
        payload: bytes,
        part_size: int,
        part_count: int,
        cancel_token: CancelToken,
    ) -> AsyncIterator[tuple[int, bytes]]:
        """Yield (part_index, chunk) slicing an in-memory payload."""
        file_size = len(payload)
        for part_index in range(part_count):
            cancel_token.raise_if_cancelled()
            start = part_index * part_size
            end = min(file_size, start + part_size)
            yield part_index, payload[start:end]

    async def _run_parallel_part_upload(
        self,
        *,
        file_name: str,
        file_size: int,
        part_count: int,
        chunk_iter: AsyncIterator[tuple[int, bytes]],
        cancel_token: CancelToken,
        progress_cb,
        progress_lock: asyncio.Lock,
        workers_hint: int | None,
        client,
        telemetry_label: str,
    ) -> tuple[types.InputFileBig, float, dict[str, object]]:
        """Upload one logical part via SaveBigFilePart with adaptive worker pool.

        Shared engine for the file-backed and bytes-backed variants; the only
        difference between them is ``chunk_iter`` (the source of 512KB blocks).
        """
        effective_client = client or self.client
        file_id = helpers.generate_random_long()
        worker_cap = (
            self._PREMIUM_CONCURRENCY_CAP
            if self.transfer_limits.is_premium
            else self._REGULAR_CONCURRENCY_CAP
        )
        hint = (
            int(workers_hint)
            if workers_hint is not None
            else int(self.config.concurrency)
        )
        workers = max(1, min(worker_cap, hint, part_count))
        if workers_hint is None:
            workers = max(
                workers,
                min(worker_cap, self._auto_boost_concurrency_target(), part_count),
            )
            max_workers = max(
                1, min(worker_cap, part_count, self._MAX_UPLOAD_CONCURRENCY)
            )
            min_workers = (
                2 if self.transfer_limits.is_premium and max_workers >= 2 else 1
            )
            if workers < min_workers:
                workers = min_workers
        else:
            min_workers = 1
            max_workers = workers
        adaptive = _AdaptiveUploadController(
            initial_concurrency=workers,
            max_concurrency=max_workers,
            is_premium=bool(self.transfer_limits.is_premium),
            min_concurrency=min_workers,
        )
        queue_size = max(4, max_workers * self._UPLOAD_QUEUE_FACTOR)
        queue: asyncio.Queue[tuple[int, bytes] | None] = asyncio.Queue(
            maxsize=queue_size
        )

        uploaded_bytes = 0
        completed_parts = 0
        total_send_seconds = 0.0
        flood_wait_count = 0
        flood_wait_seconds = 0.0
        transfer_started = time.monotonic()
        telemetry_last_ts = transfer_started
        telemetry_last_bytes = 0

        def log_telemetry(*, reason: str, force: bool = False) -> None:
            nonlocal telemetry_last_ts, telemetry_last_bytes
            now = time.monotonic()
            if (
                not force
                and (now - telemetry_last_ts) < self._TELEMETRY_LOG_INTERVAL_SEC
            ):
                return
            total_elapsed = max(0.001, now - transfer_started)
            window_elapsed = max(0.001, now - telemetry_last_ts)
            window_bytes = max(0, int(uploaded_bytes - telemetry_last_bytes))
            avg_speed = (
                float(max(0, uploaded_bytes)) / total_elapsed / (1024.0 * 1024.0)
            )
            window_speed = float(window_bytes) / window_elapsed / (1024.0 * 1024.0)
            logger.info(
                (
                    "Upload %s telemetry: file=%s reason=%s parts=%d/%d bytes=%d/%d "
                    "window=%.2f MB/s avg=%.2f MB/s queue=%d slots=%d/%d target=%d flood=%d(%.1fs)"
                ),
                telemetry_label,
                file_name,
                reason,
                completed_parts,
                part_count,
                uploaded_bytes,
                file_size,
                window_speed,
                avg_speed,
                queue.qsize(),
                0,
                max_workers,
                max_workers,
                flood_wait_count,
                flood_wait_seconds,
            )
            telemetry_last_ts = now
            telemetry_last_bytes = int(uploaded_bytes)

        async def worker() -> None:
            nonlocal \
                uploaded_bytes, \
                completed_parts, \
                total_send_seconds, \
                flood_wait_count, \
                flood_wait_seconds
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    return
                part_index, chunk = item
                try:
                    cancel_token.raise_if_cancelled()
                    await adaptive.acquire_slot(cancel_token)
                    part_started = time.monotonic()
                    send_stats = {"flood_wait_count": 0, "flood_wait_seconds": 0.0}
                    try:
                        await self._save_big_part_with_retry(
                            file_id,
                            part_index,
                            part_count,
                            chunk,
                            stats=send_stats,
                            on_flood_wait=adaptive.record_flood_wait,
                            cancel_token=cancel_token,
                            client=effective_client,
                        )
                    finally:
                        await adaptive.release_slot()
                    send_elapsed = max(0.0, time.monotonic() - part_started)
                    total_send_seconds += send_elapsed
                    flood_wait_count += int(send_stats["flood_wait_count"])
                    flood_wait_seconds += float(send_stats["flood_wait_seconds"])
                    adaptive.record_sample(
                        sent_bytes=len(chunk),
                        elapsed_seconds=send_elapsed,
                        flood_wait_count=int(send_stats["flood_wait_count"]),
                        flood_wait_seconds=float(send_stats["flood_wait_seconds"]),
                        flood_wait_live_recorded=bool(
                            send_stats.get("flood_wait_live_recorded", False)
                        ),
                    )
                    async with progress_lock:
                        uploaded_bytes += int(len(chunk))
                        completed_parts += 1
                        if progress_cb is not None:
                            await progress_cb(uploaded_bytes, file_size)
                    log_telemetry(reason="part")
                finally:
                    queue.task_done()

        worker_tasks = [asyncio.create_task(worker()) for _ in range(max_workers)]
        log_telemetry(reason="start", force=True)

        def raise_worker_failures() -> None:
            for task in worker_tasks:
                if not task.done() or task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    raise exc

        async def put_with_backpressure(item: tuple[int, bytes] | None) -> None:
            # Prevent deadlock if all workers fail while producer is blocked on full queue.
            while True:
                raise_worker_failures()
                cancel_token.raise_if_cancelled()
                try:
                    await asyncio.wait_for(queue.put(item), timeout=0.25)
                    return
                except asyncio.TimeoutError:
                    log_telemetry(reason="backpressure")
                    continue

        try:
            async for part_index, chunk in chunk_iter:
                raise_worker_failures()
                cancel_token.raise_if_cancelled()
                await put_with_backpressure((part_index, chunk))

            for _ in range(max_workers):
                await put_with_backpressure(None)
            await asyncio.gather(*worker_tasks)
        except asyncio.CancelledError:
            for task in worker_tasks:
                task.cancel()
            await asyncio.gather(*worker_tasks, return_exceptions=True)
            raise
        except Exception:
            for task in worker_tasks:
                task.cancel()
            await asyncio.gather(*worker_tasks, return_exceptions=True)
            raise
        log_telemetry(reason="final", force=True)

        adaptive_summary = adaptive.summary()
        return (
            types.InputFileBig(file_id, part_count, file_name),
            total_send_seconds,
            {
                "initial_workers": int(workers),
                "max_workers": int(max_workers),
                "effective_workers": int(
                    adaptive_summary.get("final_concurrency") or workers
                ),
                "flood_wait_count": int(flood_wait_count),
                "flood_wait_seconds": float(flood_wait_seconds),
                "adaptive": adaptive_summary,
            },
        )

    async def _upload_big_file_parallel(
        self,
        *,
        file_path: Path,
        file_size: int,
        file_name: str,
        cancel_token: CancelToken,
        progress_cb,
        progress_lock: asyncio.Lock,
        workers_hint: int | None = None,
        client=None,
    ) -> tuple[types.InputFileBig, float, dict[str, object]]:
        part_size = self._TG_REQUEST_SIZE
        part_count = max(1, math.ceil(file_size / part_size))
        return await self._run_parallel_part_upload(
            file_name=file_name,
            file_size=file_size,
            part_count=part_count,
            chunk_iter=self._iter_file_chunks(
                file_path, part_size, part_count, cancel_token
            ),
            cancel_token=cancel_token,
            progress_cb=progress_cb,
            progress_lock=progress_lock,
            workers_hint=workers_hint,
            client=client,
            telemetry_label="bigfile",
        )

    async def _upload_bytes_big_file_parallel(
        self,
        *,
        payload: bytes,
        file_name: str,
        cancel_token: CancelToken,
        progress_cb,
        progress_lock: asyncio.Lock,
        workers_hint: int | None = None,
        client=None,
    ) -> tuple[types.InputFileBig, float, dict[str, object]]:
        part_size = self._TG_REQUEST_SIZE
        file_size = len(payload)
        part_count = max(1, math.ceil(file_size / part_size))
        return await self._run_parallel_part_upload(
            file_name=file_name,
            file_size=file_size,
            part_count=part_count,
            chunk_iter=self._iter_bytes_chunks(
                payload, part_size, part_count, cancel_token
            ),
            cancel_token=cancel_token,
            progress_cb=progress_cb,
            progress_lock=progress_lock,
            workers_hint=workers_hint,
            client=client,
            telemetry_label="payload",
        )

    async def _save_big_part_with_retry(
        self,
        file_id: int,
        part_index: int,
        part_count: int,
        payload: bytes,
        stats: dict[str, float | int] | None = None,
        on_flood_wait=None,
        cancel_token: CancelToken | None = None,
        client=None,
    ) -> None:
        effective_client = client or self.client
        while True:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            try:
                async for attempt in AsyncRetrying(
                    retry=retry_if_exception(_is_retryable_error),
                    wait=wait_exponential(
                        multiplier=self.config.retry.base_delay, min=1, max=60
                    ),
                    stop=stop_after_attempt(self.config.retry.max_attempts),
                    reraise=True,
                ):
                    with attempt:
                        attempt_no = int(attempt.retry_state.attempt_number)
                        logger.debug(
                            "SaveBigFilePart attempt=%d file_id=%d part=%d/%d bytes=%d",
                            attempt_no,
                            int(file_id),
                            part_index + 1,
                            part_count,
                            len(payload),
                        )
                        await self._upload_bandwidth.acquire(len(payload))
                        ok = await effective_client(
                            functions.upload.SaveBigFilePartRequest(
                                file_id=file_id,
                                file_part=part_index,
                                file_total_parts=part_count,
                                bytes=payload,
                            )
                        )
                        if not ok:
                            raise RuntimeError(
                                f"Failed to upload part {part_index + 1}/{part_count}"
                            )
                        logger.debug(
                            "SaveBigFilePart success file_id=%d part=%d/%d",
                            int(file_id),
                            part_index + 1,
                            part_count,
                        )
                        return
            except (ConnectionError, TimeoutError) as exc:
                # All retries exhausted with a connection error — the proxy may
                # have died. Try the next level in the chain (backup->direct).
                await self._on_persistent_connection_failure(effective_client, exc)
                raise
            except FloodWaitError as exc:
                wait_seconds = float(max(0, int(exc.seconds)))
                if stats is not None:
                    stats["flood_wait_count"] = (
                        int(stats.get("flood_wait_count", 0)) + 1
                    )
                    stats["flood_wait_seconds"] = (
                        float(stats.get("flood_wait_seconds", 0.0)) + wait_seconds
                    )
                    stats["flood_wait_live_recorded"] = bool(
                        stats.get("flood_wait_live_recorded", False)
                    )
                if callable(on_flood_wait):
                    on_flood_wait(wait_seconds)
                    if stats is not None:
                        stats["flood_wait_live_recorded"] = True
                cumulative_count = (
                    int(stats.get("flood_wait_count", 0)) if stats is not None else 0
                )
                cumulative_seconds = (
                    float(stats.get("flood_wait_seconds", 0.0))
                    if stats is not None
                    else 0.0
                )
                logger.warning(
                    "Upload FloodWait while sending part %d/%d: %ss cumulative=%d(%.1fs)",
                    part_index + 1,
                    part_count,
                    exc.seconds,
                    cumulative_count,
                    cumulative_seconds,
                )
                await self._sleep_with_cancel(
                    wait_seconds + self._FLOOD_WAIT_BUFFER_SECONDS,
                    cancel_token=cancel_token,
                )
