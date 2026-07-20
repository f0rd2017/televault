from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import time


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
from televault.tg.upload.resume import existing_completed_parts

logger = logging.getLogger(__name__)


class _SinglePartUploadMixin:
    async def _single_part_upload(
        self,
        *,
        path: Path,
        total_size: int,
        file_digest: str,
        file_key: str,
        file_name: str,
        normalized_folder: str,
        operation_started: float,
        prehash_elapsed: float,
        progress_cb,
        cancel_token: CancelToken,
        original_size: int,
        compression_used: bool,
        compression_seconds: float,
        compression_ratio: float | None,
        safe_limit_bytes: int,
        client_offset: int,
    ) -> dict[str, object]:
        cancel_token.raise_if_cancelled()
        self.repo.upsert_folder(normalized_folder)

        # Upload resume/dedup: if this single part is already in the index with the
        # same payload digest, return it without re-sending.
        try:
            existing_single_parts = self.repo.get_parts_for_object(
                folder_path=normalized_folder, file_key=file_key
            )
        except Exception:
            existing_single_parts = []
        if 0 in existing_completed_parts(
            existing_single_parts,
            planned_parts_total=1,
            payload_sha256=file_digest,
            caption_prefix=self.config.caption_prefix,
        ):
            existing_part0 = next(
                p for p in existing_single_parts if int(p.part_index) == 0
            )
            existing_chat_id = str(existing_part0.chat_id)
            existing_label = existing_chat_id
            for pool_client in self._client_pool:
                if str(self._chat_id_for_client(pool_client)) == existing_chat_id:
                    existing_label = self._client_label(pool_client)
                    break
            resumed_part_size = int(existing_part0.file_size or total_size)
            resumed_total_elapsed = max(0.001, time.monotonic() - operation_started)
            resumed_analytics = self._build_upload_analytics(
                phase_seconds={
                    "prehash": prehash_elapsed,
                    "read": 0.0,
                    "encrypt": 0.0,
                    "network_send": 0.0,
                    "db_upsert": 0.0,
                    "db_rebuild": 0.0,
                    "transfer": resumed_total_elapsed,
                    "total": resumed_total_elapsed,
                },
                payload_total_bytes=resumed_part_size,
                source_total_bytes=total_size,
                source_original_bytes=original_size,
                transfer_elapsed=resumed_total_elapsed,
                total_elapsed=resumed_total_elapsed,
                compression_mode=str(self.config.upload_compression_mode),
                compression_used=compression_used,
                compression_seconds=compression_seconds,
                compression_ratio=compression_ratio,
                safe_limit_bytes=safe_limit_bytes,
                flood_wait_count=0,
                flood_wait_seconds=0.0,
                adaptive_block={
                    "initial_part_concurrency": 1,
                    "final_part_concurrency": 1,
                    "flood_wait_count": 0,
                },
                upload_profile={
                    "chunk_size": int(resumed_part_size),
                    "concurrency": 1,
                    "inner_workers": 1,
                    "parts_total": 1,
                    "direct_mode": True,
                    "direct_parallel_parts": False,
                    "direct_parallel_profile": None,
                    "resumed": True,
                    "channels_used": [existing_chat_id],
                    "parts_by_channel": {existing_chat_id: 1},
                    "clients_used": [existing_label],
                    "cross_channel_parts": False,
                },
            )
            logger.info(
                "Upload single-part resume: file_key=%s already uploaded, skipping send",
                file_key,
            )
            return {
                "file_key": file_key,
                "parts_total": 1,
                "sha256": file_digest,
                "folder_path": normalized_folder,
                "orig_name": file_name,
                "analytics": resumed_analytics,
                "channels_used": [existing_chat_id],
                "clients_used": [existing_label],
                "cross_channel_parts": False,
            }

        progress_agg: TransferProgressAggregator | None = None
        if progress_cb is not None:
            progress_agg = TransferProgressAggregator(
                total_parts=1,
                total_bytes_hint=total_size,
                emit_interval_ms=120,
                percent_step=1.0,
                activity="Uploading",
            )
            await progress_agg.start(progress_cb)
        progress_lock = asyncio.Lock()

        async def on_progress(current: int, total: int) -> None:
            if progress_agg is not None:
                progress_agg.on_part_progress(0, int(current), int(total))

        progress_last_ts = time.monotonic()
        progress_last_bytes = 0

        def notify_progress(current: int, total: int) -> None:
            nonlocal progress_last_ts, progress_last_bytes
            if progress_agg is not None:
                progress_agg.on_part_progress(0, int(current), int(total))
            now = time.monotonic()
            if (now - progress_last_ts) < self._TELEMETRY_LOG_INTERVAL_SEC:
                return
            window_elapsed = max(0.001, now - progress_last_ts)
            total_elapsed = max(0.001, now - send_started)
            window_bytes = max(0, int(current - progress_last_bytes))
            avg_speed = float(max(0, int(current))) / total_elapsed / (1024.0 * 1024.0)
            window_speed = float(window_bytes) / window_elapsed / (1024.0 * 1024.0)
            logger.info(
                "Upload direct telemetry: file=%s bytes=%d/%d window=%.2f MB/s avg=%.2f MB/s",
                file_name,
                int(current),
                int(total),
                window_speed,
                avg_speed,
            )
            progress_last_ts = now
            progress_last_bytes = int(current)

        meta = PartMeta(
            folder_path=normalized_folder,
            file_key=file_key,
            part_index=0,
            parts_total=1,
            orig_name=file_name,
        )
        send_payload: bytes | None = None
        stored_part_size = int(total_size)
        if total_size == 0:
            # Telegram rejects truly empty sendMedia uploads; upload a tiny stub payload
            # and keep logical file size in caption metadata.
            send_payload = self._EMPTY_FILE_STUB_PAYLOAD
            stored_part_size = len(send_payload)
        caption = build_caption(
            meta,
            prefix=self.config.caption_prefix,
            extra={
                "sha256": file_digest,
                "orig_size": total_size,
                "part_size": stored_part_size,
                "enc": False,
            },
        )
        if send_payload is None and total_size <= self._INLINE_PAYLOAD_UPLOAD_MAX_BYTES:
            inline_payload = await asyncio.to_thread(path.read_bytes)
            if len(inline_payload) != total_size:
                raise RuntimeError(
                    "File changed during upload: expected "
                    f"{total_size} bytes, got {len(inline_payload)} bytes ({path})"
                )
            send_payload = inline_payload

        send_started = time.monotonic()
        direct_parallel_mode = False
        upload_ok = False
        single_part_slot = int(client_offset) % len(self._client_pool)
        single_part_client = self._pool_client(0, base_offset=client_offset)
        single_part_chat_id = self._chat_id_for_client(single_part_client)
        single_part_client_label = self._client_label(single_part_client)
        logger.debug(
            "Upload single-part dispatch: client_slot=%d client=%s chat_id=%s",
            int(single_part_slot),
            single_part_client_label,
            single_part_chat_id,
        )
        try:
            if total_size >= self._DIRECT_PARALLEL_BIGFILE_MIN_BYTES:
                direct_parallel_mode = True
                direct_parallel_workers_hint = partition.default_inner_upload_workers(
                    total_size
                )
                (
                    uploaded_file,
                    _part_send_seconds,
                    parallel_profile,
                ) = await self._upload_big_file_parallel(
                    file_path=path,
                    file_size=total_size,
                    file_name=file_name,
                    cancel_token=cancel_token,
                    progress_cb=on_progress,
                    progress_lock=progress_lock,
                    workers_hint=direct_parallel_workers_hint,
                    client=single_part_client,
                )
                message = await self._send_uploaded_file_with_retry(
                    uploaded_file,
                    caption=caption,
                    file_name=file_name,
                    cancel_token=cancel_token,
                    client=single_part_client,
                )
            else:
                parallel_profile = None
                if send_payload is not None:
                    message = await self._send_with_retry(
                        payload=send_payload,
                        caption=caption,
                        file_name=file_name,
                        progress_callback=notify_progress,
                        cancel_token=cancel_token,
                        client=single_part_client,
                    )
                else:
                    message = await self._send_path_with_retry(
                        file_path=path,
                        caption=caption,
                        file_name=file_name,
                        progress_callback=notify_progress,
                        cancel_token=cancel_token,
                        client=single_part_client,
                        expected_size=total_size,
                    )
            if progress_agg is not None:
                progress_agg.on_part_progress(0, stored_part_size, stored_part_size)
            upload_ok = True
        finally:
            if progress_agg is not None:
                await progress_agg.stop("Upload complete" if upload_ok else None)

        network_send_seconds = max(0.0, time.monotonic() - send_started)

        if message.date:
            try:
                msg_date = int(message.date.timestamp())
            except (OSError, OverflowError, ValueError):
                msg_date = now_ts()
        else:
            msg_date = now_ts()

        db_upsert_started = time.monotonic()
        self.repo.upsert_msg_part(
            PartRecord(
                msg_id=int(message.id),
                chat_id=single_part_chat_id,
                folder_path=normalized_folder,
                file_key=file_key,
                part_index=0,
                parts_total=1,
                orig_name=file_name,
                file_size=stored_part_size,
                caption_raw=caption,
                date_ts=msg_date,
            )
        )
        db_upsert_seconds = max(0.0, time.monotonic() - db_upsert_started)

        rebuild_started = time.monotonic()
        self.repo.rebuild_object_aggregate(self.chat_id, normalized_folder, file_key)
        rebuild_elapsed = max(0.0, time.monotonic() - rebuild_started)

        total_elapsed = max(0.001, time.monotonic() - operation_started)
        transfer_elapsed = max(0.001, network_send_seconds)
        transfer_mbps = float(stored_part_size) / transfer_elapsed / (1024.0 * 1024.0)
        profile = parallel_profile or {}
        analytics = self._build_upload_analytics(
            phase_seconds={
                "prehash": prehash_elapsed,
                "read": 0.0,
                "encrypt": 0.0,
                "network_send": network_send_seconds,
                "db_upsert": db_upsert_seconds,
                "db_rebuild": rebuild_elapsed,
                "transfer": transfer_elapsed,
                "total": total_elapsed,
            },
            payload_total_bytes=stored_part_size,
            source_total_bytes=total_size,
            source_original_bytes=original_size,
            transfer_elapsed=transfer_elapsed,
            total_elapsed=total_elapsed,
            compression_mode=str(self.config.upload_compression_mode),
            compression_used=compression_used,
            compression_seconds=compression_seconds,
            compression_ratio=compression_ratio,
            safe_limit_bytes=safe_limit_bytes,
            flood_wait_count=int(profile.get("flood_wait_count", 0)),
            flood_wait_seconds=float(profile.get("flood_wait_seconds", 0.0)),
            adaptive_block={
                "initial_part_concurrency": int(profile.get("initial_workers", 1)),
                "final_part_concurrency": int(profile.get("effective_workers", 1)),
                "flood_wait_count": int(profile.get("flood_wait_count", 0)),
            },
            upload_profile={
                "chunk_size": int(stored_part_size),
                "concurrency": int(profile.get("initial_workers", 1)),
                "inner_workers": int(profile.get("effective_workers", 1)),
                "parts_total": 1,
                "direct_mode": True,
                "direct_parallel_parts": bool(direct_parallel_mode),
                "direct_parallel_profile": parallel_profile,
                "channels_used": [single_part_chat_id],
                "parts_by_channel": {single_part_chat_id: 1},
                "clients_used": [single_part_client_label],
                "cross_channel_parts": False,
            },
        )
        logger.info(
            (
                "Upload direct mode: file_key=%s size=%d bytes net=%.3fs "
                "total=%.3fs transfer_speed=%.2f MB/s parallel_parts=%s"
            ),
            file_key,
            total_size,
            network_send_seconds,
            total_elapsed,
            transfer_mbps,
            direct_parallel_mode,
        )
        return {
            "file_key": file_key,
            "parts_total": 1,
            "sha256": file_digest,
            "folder_path": normalized_folder,
            "orig_name": file_name,
            "analytics": analytics,
            "channels_used": [single_part_chat_id],
            "clients_used": [single_part_client_label],
            "cross_channel_parts": False,
        }
