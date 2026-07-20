from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
import tempfile
import time

import psutil

from televault.core.jobs import CancelToken
from televault.tg import partition
from televault.core.transfer_progress import TransferProgressAggregator
from televault.core.types import (
    PartMeta,
    PartRecord,
)
from televault.core.utils import (
    now_ts,
)
from televault.tg.parser import build_caption
from televault.tg.upload.records import _UploadRecordBuffer
from televault.tg.upload.resume import existing_completed_parts

logger = logging.getLogger(__name__)


class _MultipartUploadMixin:
    @classmethod
    def _copy_file_segment_to_path_sync(
        cls,
        source_path: Path,
        target_path: Path,
        offset: int,
        length: int,
        cancel_token: CancelToken,
    ) -> int:
        written = 0
        remaining = max(0, int(length))
        with source_path.open("rb") as src:
            src.seek(max(0, int(offset)))
            with target_path.open("wb") as dst:
                while remaining > 0:
                    cancel_token.raise_if_cancelled()
                    chunk = src.read(min(cls._DISK_PART_COPY_BUFFER, remaining))
                    if not chunk:
                        break
                    dst.write(chunk)
                    block = int(len(chunk))
                    written += block
                    remaining -= block
        return int(written)

    async def _multipart_upload_from_disk(
        self,
        *,
        source_path: Path,
        total_size: int,
        original_size: int,
        file_digest: str,
        file_key: str,
        file_name: str,
        normalized_folder: str,
        operation_started: float,
        prehash_elapsed: float,
        progress_cb,
        cancel_token: CancelToken,
        compression_mode: str,
        compression_used: bool,
        compression_seconds: float,
        compression_ratio: float | None,
        safe_limit_bytes: int,
        part_size_bytes: int,
        balanced_target_chunk: int | None,
        client_offset: int,
    ) -> dict[str, object]:
        self.repo.upsert_folder(normalized_folder)

        part_size = max(1, min(int(part_size_bytes), int(safe_limit_bytes)))
        parts_total = max(1, math.ceil(total_size / part_size))
        part_sizes = {
            idx: min(part_size, total_size - (idx * part_size))
            for idx in range(parts_total)
        }
        total_payload_size = int(sum(int(v) for v in part_sizes.values()))

        # Upload resume: parts already in the index (sent in a previous run) with
        # the same parts_total and payload digest are skipped below.
        try:
            existing_parts = self.repo.get_parts_for_object(
                folder_path=normalized_folder, file_key=file_key
            )
        except Exception:
            existing_parts = []
        resume_completed_parts = existing_completed_parts(
            existing_parts,
            planned_parts_total=parts_total,
            payload_sha256=file_digest,
            caption_prefix=self.config.caption_prefix,
        )
        existing_part_by_index = {int(p.part_index): p for p in existing_parts}
        if resume_completed_parts:
            logger.info(
                "Upload resume (disk): file_key=%s skipping %d/%d already-uploaded parts",
                file_key,
                len(resume_completed_parts),
                parts_total,
            )

        progress_agg: TransferProgressAggregator | None = None
        if progress_cb is not None:
            progress_agg = TransferProgressAggregator(
                total_parts=parts_total,
                total_bytes_hint=total_payload_size,
                emit_interval_ms=120,
                percent_step=1.0,
                activity="Uploading",
                source_bytes_hint=original_size,
            )
            await progress_agg.start(progress_cb)

        record_buffer = _UploadRecordBuffer(
            repo=self.repo,
            chat_id=self.chat_id,
            folder_path=normalized_folder,
            file_key=file_key,
            batch_size=self._DB_UPSERT_BATCH,
            rebuild_throttle_sec=self._DB_REBUILD_THROTTLE_SEC,
        )
        split_seconds = 0.0
        network_send_seconds = 0.0
        flood_wait_count = 0
        flood_wait_seconds = 0.0
        transfer_started = time.monotonic()
        upload_ok = False

        # Log system metrics at upload start
        _proc = psutil.Process()
        _mem = psutil.virtual_memory()
        logger.info(
            "Upload system metrics at start: file=%s pid_mem=%.0f MB system_ram_used=%.0f MB "
            "system_ram_available=%.0f MB cpu_percent=%.1f%% disk_parts=%d disk_size_mb=%d",
            file_name,
            _proc.memory_info().rss / (1024 * 1024),
            (_mem.total - _mem.available) / (1024 * 1024),
            _mem.available / (1024 * 1024),
            psutil.cpu_percent(interval=None),
            parts_total,
            total_size / (1024 * 1024),
        )
        completed_parts = 0
        channel_payload_bytes: dict[str, int] = {}
        channel_parts_count: dict[str, int] = {}
        clients_used: set[str] = set()

        # Seed counters/progress from parts already uploaded in a previous run.
        for resumed_index in sorted(resume_completed_parts):
            resumed_payload = int(part_sizes.get(resumed_index, 0))
            completed_parts += 1
            resumed_part = existing_part_by_index.get(resumed_index)
            resumed_chat_id = str(resumed_part.chat_id) if resumed_part else ""
            if resumed_chat_id:
                channel_payload_bytes[resumed_chat_id] = int(
                    channel_payload_bytes.get(resumed_chat_id, 0) + resumed_payload
                )
                channel_parts_count[resumed_chat_id] = int(
                    channel_parts_count.get(resumed_chat_id, 0) + 1
                )
            if progress_agg is not None:
                progress_agg.on_part_progress(
                    resumed_index, resumed_payload, resumed_payload
                )

        pool_size = max(1, len(self._client_pool))
        workers = max(1, min(int(parts_total), int(pool_size)))
        disk_part_parallel_workers_hint = partition.default_inner_upload_workers(
            part_size
        )
        queue_size = max(
            2, min(int(workers) * self._DISK_PART_UPLOAD_QUEUE_FACTOR, workers * 2)
        )
        part_queue: asyncio.Queue[tuple[int, Path, int] | None] = asyncio.Queue(
            maxsize=queue_size
        )

        def make_progress_callback(part_index: int):
            def on_progress(current: int, total: int) -> None:
                if progress_agg is None:
                    return
                progress_agg.on_part_progress(part_index, int(current), int(total))

            return on_progress

        disk_parallel_part_uploads = 0

        async def consumer() -> None:
            nonlocal \
                network_send_seconds, \
                flood_wait_count, \
                flood_wait_seconds, \
                completed_parts
            nonlocal disk_parallel_part_uploads
            while True:
                item = await part_queue.get()
                if item is None:
                    part_queue.task_done()
                    return

                part_index, temp_part_path, written = item
                client_slot = (int(part_index) + int(client_offset)) % len(
                    self._client_pool
                )
                upload_client = self._pool_client(part_index, base_offset=client_offset)
                upload_chat_id = self._chat_id_for_client(upload_client)
                upload_client_label = self._client_label(upload_client)
                logger.debug(
                    "Upload disk part dispatch: part=%d/%d client_slot=%d client=%s chat_id=%s",
                    int(part_index) + 1,
                    int(parts_total),
                    int(client_slot),
                    upload_client_label,
                    upload_chat_id,
                )
                try:
                    cancel_token.raise_if_cancelled()
                    meta = PartMeta(
                        folder_path=normalized_folder,
                        file_key=file_key,
                        part_index=part_index,
                        parts_total=parts_total,
                        orig_name=file_name,
                    )
                    caption = build_caption(
                        meta,
                        prefix=self.config.caption_prefix,
                        extra={
                            "sha256": file_digest,
                            "orig_size": total_size,
                            "part_size": int(written),
                            "enc": False,
                        },
                    )

                    send_stats = {"flood_wait_count": 0, "flood_wait_seconds": 0.0}
                    send_started = time.monotonic()
                    part_file_name = f"{file_name}.part{part_index:04d}"
                    if int(written) >= self._DIRECT_PARALLEL_BIGFILE_MIN_BYTES:
                        part_progress_lock = asyncio.Lock()

                        async def on_part_progress(current: int, total: int) -> None:
                            cb = make_progress_callback(part_index)
                            cb(int(current), int(total))

                        (
                            uploaded_file,
                            _part_send_seconds,
                            part_profile,
                        ) = await self._upload_big_file_parallel(
                            file_path=temp_part_path,
                            file_size=int(written),
                            file_name=part_file_name,
                            cancel_token=cancel_token,
                            progress_cb=on_part_progress,
                            progress_lock=part_progress_lock,
                            workers_hint=disk_part_parallel_workers_hint,
                            client=upload_client,
                        )
                        message = await self._send_uploaded_file_with_retry(
                            uploaded_file,
                            caption=caption,
                            file_name=part_file_name,
                            stats=send_stats,
                            cancel_token=cancel_token,
                            client=upload_client,
                        )
                        send_stats["flood_wait_count"] = int(
                            send_stats.get("flood_wait_count", 0)
                        ) + int(part_profile.get("flood_wait_count", 0))
                        send_stats["flood_wait_seconds"] = float(
                            send_stats.get("flood_wait_seconds", 0.0)
                        ) + float(part_profile.get("flood_wait_seconds", 0.0))
                        if (
                            int(
                                part_profile.get(
                                    "effective_workers", disk_part_parallel_workers_hint
                                )
                            )
                            > 1
                        ):
                            disk_parallel_part_uploads += 1
                    else:
                        message = await self._send_path_with_retry(
                            file_path=temp_part_path,
                            caption=caption,
                            file_name=part_file_name,
                            progress_callback=make_progress_callback(part_index),
                            stats=send_stats,
                            cancel_token=cancel_token,
                            client=upload_client,
                        )
                    network_send_seconds += max(0.0, time.monotonic() - send_started)
                    flood_wait_count += int(send_stats.get("flood_wait_count", 0))
                    flood_wait_seconds += float(
                        send_stats.get("flood_wait_seconds", 0.0)
                    )
                    completed_parts += 1
                    clients_used.add(upload_client_label)
                    channel_payload_bytes[upload_chat_id] = int(
                        channel_payload_bytes.get(upload_chat_id, 0) + int(written)
                    )
                    channel_parts_count[upload_chat_id] = int(
                        channel_parts_count.get(upload_chat_id, 0) + 1
                    )
                    if progress_agg is not None:
                        progress_agg.on_part_progress(
                            part_index, int(written), int(written)
                        )

                    if message.date:
                        try:
                            msg_date = int(message.date.timestamp())
                        except (OSError, OverflowError, ValueError):
                            msg_date = now_ts()
                    else:
                        msg_date = now_ts()

                    await record_buffer.add(
                        PartRecord(
                            msg_id=int(message.id),
                            chat_id=upload_chat_id,
                            folder_path=normalized_folder,
                            file_key=file_key,
                            part_index=part_index,
                            parts_total=parts_total,
                            orig_name=file_name,
                            file_size=int(written),
                            caption_raw=caption,
                            date_ts=msg_date,
                        )
                    )
                finally:
                    temp_part_path.unlink(missing_ok=True)
                    part_queue.task_done()

        consumer_tasks = [asyncio.create_task(consumer()) for _ in range(workers)]

        def raise_consumer_failures() -> None:
            for task in consumer_tasks:
                if not task.done() or task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    raise exc

        async def put_with_backpressure(item: tuple[int, Path, int] | None) -> None:
            while True:
                raise_consumer_failures()
                cancel_token.raise_if_cancelled()
                try:
                    await asyncio.wait_for(part_queue.put(item), timeout=0.25)
                    return
                except asyncio.TimeoutError:
                    continue

        def cleanup_pending_queue_files() -> None:
            while True:
                try:
                    leftover = part_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if leftover is not None:
                    _, leftover_path, _ = leftover
                    leftover_path.unlink(missing_ok=True)
                try:
                    part_queue.task_done()
                except ValueError:
                    break

        try:
            for part_index in range(parts_total):
                raise_consumer_failures()
                cancel_token.raise_if_cancelled()
                if part_index in resume_completed_parts:
                    # Already uploaded in a previous run — skip slicing/sending.
                    continue
                offset = int(part_index * part_size)
                expected_bytes = int(part_sizes[part_index])

                temp_file = tempfile.NamedTemporaryFile(
                    prefix=f"televault_slice_{part_index:04d}_",
                    suffix=".bin",
                    delete=False,
                )
                temp_part_path = Path(temp_file.name)
                temp_file.close()
                try:
                    split_started = time.monotonic()
                    written = await asyncio.to_thread(
                        self._copy_file_segment_to_path_sync,
                        source_path,
                        temp_part_path,
                        offset,
                        expected_bytes,
                        cancel_token,
                    )
                    split_seconds += max(0.0, time.monotonic() - split_started)
                    if written != expected_bytes:
                        raise RuntimeError(
                            f"Part slice size mismatch for index {part_index}: {written} != {expected_bytes}"
                        )
                    await put_with_backpressure(
                        (part_index, temp_part_path, int(written))
                    )
                    temp_part_path = None
                finally:
                    if temp_part_path is not None:
                        temp_part_path.unlink(missing_ok=True)

            for _ in range(workers):
                await put_with_backpressure(None)
            await asyncio.gather(*consumer_tasks)

            await record_buffer.flush(force=True)
            upload_ok = True
        except asyncio.CancelledError:
            for task in consumer_tasks:
                task.cancel()
            await asyncio.gather(*consumer_tasks, return_exceptions=True)
            cleanup_pending_queue_files()
            try:
                await record_buffer.flush(force=True)
            except Exception:
                logger.exception(
                    "Failed to flush pending upload records after cancellation"
                )
            raise
        except Exception:
            for task in consumer_tasks:
                task.cancel()
            await asyncio.gather(*consumer_tasks, return_exceptions=True)
            cleanup_pending_queue_files()
            try:
                await record_buffer.flush(force=True)
            except Exception:
                logger.exception(
                    "Failed to flush pending upload records after interruption"
                )
            raise
        finally:
            cleanup_pending_queue_files()
            if progress_agg is not None:
                await progress_agg.stop("Upload complete" if upload_ok else None)

        rebuild_started = time.monotonic()
        self.repo.rebuild_object_aggregate(self.chat_id, normalized_folder, file_key)
        record_buffer.db_rebuild_seconds += max(0.0, time.monotonic() - rebuild_started)

        transfer_elapsed = max(0.001, time.monotonic() - transfer_started)
        total_elapsed = max(0.001, time.monotonic() - operation_started)
        transfer_speed_mbps = (
            float(total_payload_size) / transfer_elapsed / (1024.0 * 1024.0)
        )

        analytics = self._build_upload_analytics(
            phase_seconds={
                "prehash": prehash_elapsed,
                "read": split_seconds,
                "encrypt": 0.0,
                "network_send": network_send_seconds,
                "db_upsert": record_buffer.db_upsert_seconds,
                "db_rebuild": record_buffer.db_rebuild_seconds,
                "transfer": transfer_elapsed,
                "total": total_elapsed,
            },
            payload_total_bytes=total_payload_size,
            source_total_bytes=total_size,
            source_original_bytes=original_size,
            transfer_elapsed=transfer_elapsed,
            total_elapsed=total_elapsed,
            compression_mode=compression_mode,
            compression_used=compression_used,
            compression_seconds=compression_seconds,
            compression_ratio=compression_ratio,
            safe_limit_bytes=safe_limit_bytes,
            flood_wait_count=flood_wait_count,
            flood_wait_seconds=flood_wait_seconds,
            adaptive_block={
                "initial_part_concurrency": int(workers),
                "final_part_concurrency": int(workers),
                "flood_wait_count": int(flood_wait_count),
            },
            upload_profile={
                "chunk_size": int(part_size),
                "concurrency": int(workers),
                "max_adaptive_concurrency": int(workers),
                "effective_concurrency": int(workers),
                "inner_workers": int(disk_part_parallel_workers_hint),
                "parts_total": int(parts_total),
                "disk_backed_parts": True,
                "balanced_part_sizing": bool(balanced_target_chunk is not None),
                "balanced_target_chunk": (
                    int(balanced_target_chunk)
                    if balanced_target_chunk is not None
                    else None
                ),
                "parallel_chunk_upload": bool(disk_part_parallel_workers_hint > 1),
                "parallel_chunk_parts": int(disk_parallel_part_uploads),
                "parallel_chunk_workers_hint": int(disk_part_parallel_workers_hint),
                "adaptive": {
                    "initial_part_concurrency": int(workers),
                    "final_part_concurrency": int(workers),
                    "flood_wait_count": int(flood_wait_count),
                },
                "channels_used": sorted(
                    str(chat_id) for chat_id in channel_parts_count.keys()
                ),
                "parts_by_channel": {
                    str(chat_id): int(value)
                    for chat_id, value in channel_parts_count.items()
                },
                "clients_used": sorted(str(label) for label in clients_used),
                "cross_channel_parts": bool(len(channel_parts_count) > 1),
            },
            payload_by_channel=channel_payload_bytes,
        )
        logger.info(
            (
                "Upload disk-part mode: file_key=%s parts=%d chunk_size=%d split=%.3fs net=%.3fs "
                "total=%.3fs transfer=%.2f MB/s"
            ),
            file_key,
            parts_total,
            part_size,
            split_seconds,
            network_send_seconds,
            total_elapsed,
            transfer_speed_mbps,
        )

        return {
            "file_key": file_key,
            "parts_total": parts_total,
            "sha256": file_digest,
            "folder_path": normalized_folder,
            "orig_name": file_name,
            "channels_used": analytics["upload_profile"]["channels_used"],
            "clients_used": analytics["upload_profile"]["clients_used"],
            "cross_channel_parts": analytics["upload_profile"]["cross_channel_parts"],
            "analytics": analytics,
        }
