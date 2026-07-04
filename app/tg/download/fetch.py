from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import time

from telethon.errors import FileReferenceExpiredError, FloodWaitError
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_fixed

from app.core.types import PartRecord
from app.tg.download._common import (
    _is_retryable_error,
    _preallocate_file,
)

logger = logging.getLogger(__name__)


class _DownloadFetchMixin:
    async def _fetch_messages_by_chat(
        self,
        parts: list[PartRecord],
    ) -> dict[tuple[str, int], tuple[object, object, object, str]]:
        by_key: dict[tuple[str, int], tuple[object, object, object, str]] = {}
        by_chat: dict[str, list[int]] = {}
        for part in parts:
            chat_id = str(part.chat_id or self.chat_id)
            by_chat.setdefault(chat_id, []).append(int(part.msg_id))

        batch_size = 100
        missing_chat_ids: list[str] = []
        for chat_id, msg_ids in by_chat.items():
            routes = self._download_routes_by_chat_id.get(chat_id, [])
            if not routes and chat_id == str(self.chat_id):
                routes = [(self.client, self.chat, "main")]
            if not routes:
                missing_chat_ids.append(chat_id)
                continue

            unique_ids = list(dict.fromkeys(int(msg_id) for msg_id in msg_ids))
            for start in range(0, len(unique_ids), batch_size):
                pending_ids = unique_ids[start : start + batch_size]
                if not pending_ids:
                    continue
                attempt_seed = start // batch_size
                for route_idx in range(len(routes)):
                    route_client, route_chat, route_label = routes[
                        (attempt_seed + route_idx) % len(routes)
                    ]
                    try:
                        response = await route_client.get_messages(
                            route_chat, ids=pending_ids
                        )
                    except Exception:
                        if route_idx + 1 >= len(routes):
                            raise
                        continue
                    items = response if isinstance(response, list) else [response]
                    found_ids: set[int] = set()
                    for item in items:
                        if item is None or getattr(item, "id", None) is None:
                            continue
                        msg_id = int(item.id)
                        found_ids.add(msg_id)
                        by_key[(chat_id, msg_id)] = (
                            route_client,
                            route_chat,
                            item,
                            str(route_label),
                        )
                    pending_ids = [
                        msg_id for msg_id in pending_ids if msg_id not in found_ids
                    ]
                    if not pending_ids:
                        break
                if pending_ids:
                    # The chat is reachable but these messages are gone — mark the
                    # parts lost so the object surfaces as "damaged" (vs an offline
                    # account, which has no route at all and is handled below).
                    try:
                        self.repo.mark_messages_lost_refs(
                            [(chat_id, int(msg_id)) for msg_id in pending_ids]
                        )
                    except Exception:
                        logger.exception(
                            "Failed to mark lost parts for chat_id=%s", chat_id
                        )
                    missing_preview = ",".join(
                        str(msg_id) for msg_id in pending_ids[:8]
                    )
                    raise ValueError(
                        f"Missing telegram messages for chat_id={chat_id}: {missing_preview}"
                    )

        if missing_chat_ids:
            missing_preview = ", ".join(sorted(set(missing_chat_ids)))
            raise ValueError(
                "No download routes for part chat_id(s): "
                f"{missing_preview}. Reconnect and verify channel mapping."
            )
        return by_key

    @staticmethod
    async def _fetch_single_message(client, chat, msg_id: int) -> object:
        response = await client.get_messages(chat, ids=[int(msg_id)])
        item = response[0] if isinstance(response, list) else response
        if item is None:
            raise ValueError(f"Message not found for msg_id={msg_id}")
        return item

    async def _download_with_retry(
        self,
        client,
        message,
        target_path: Path,
        progress_callback=None,
        *,
        chat=None,
        msg_id: int | None = None,
        part_concurrency: int = 1,
        stride_streams: int | None = None,
        on_flood_wait=None,
    ) -> dict[str, object]:
        active_message = message
        file_size: int = 0
        if hasattr(active_message, "file") and active_message.file is not None:
            file_size = int(active_message.file.size or 0)
        initial_stride_streams = (
            max(1, int(stride_streams))
            if stride_streams is not None
            else self._effective_stride_streams(part_concurrency)
        )
        effective_stride_streams = int(initial_stride_streams)
        force_linear_mode = False
        effective_msg_id = int(msg_id or getattr(active_message, "id", 0) or 0)
        file_ref_refreshes = 0
        file_ref_refresh_cap = max(1, int(self.config.retry.max_attempts))
        logger.debug(
            "Download request init: msg_id=%s file_size=%d part_concurrency=%d stride_streams=%d target=%s",
            effective_msg_id,
            file_size,
            int(part_concurrency),
            int(effective_stride_streams),
            str(target_path),
        )
        overall_started = time.monotonic()
        flood_wait_count = 0
        flood_wait_seconds = 0.0
        flood_wait_live_recorded = False

        while True:
            try:
                async for attempt in AsyncRetrying(
                    retry=retry_if_exception(_is_retryable_error),
                    wait=wait_fixed(
                        1
                    ),  # fast retry: 5×1s=5s vs 31s exponential (prevents file_ref expiry)
                    stop=stop_after_attempt(self.config.retry.max_attempts),
                    reraise=True,
                ):
                    with attempt:
                        attempt_no = int(attempt.retry_state.attempt_number)
                        logger.debug(
                            "Download attempt=%d msg_id=%s target=%s",
                            attempt_no,
                            effective_msg_id,
                            str(target_path),
                        )
                        await self._get_file_limiter.acquire()
                        await self._download_bandwidth.acquire(int(file_size))
                        target_path.unlink(missing_ok=True)
                        if not hasattr(client, "iter_download"):
                            await client.download_media(
                                active_message,
                                file=str(target_path),
                                progress_callback=progress_callback,
                            )
                            downloaded_bytes = 0
                            try:
                                downloaded_bytes = target_path.stat().st_size
                            except OSError:
                                downloaded_bytes = max(0, int(file_size))
                            elapsed_seconds = max(
                                0.001, time.monotonic() - overall_started
                            )
                            logger.info(
                                (
                                    "Download chunk done: msg_id=%s bytes=%d elapsed=%.3fs "
                                    "streams=%d flood=%d(%.1fs)"
                                ),
                                effective_msg_id,
                                int(downloaded_bytes),
                                elapsed_seconds,
                                1,
                                int(flood_wait_count),
                                float(flood_wait_seconds),
                            )
                            self._get_file_limiter.record_success()
                            return {
                                "downloaded_bytes": int(downloaded_bytes),
                                "elapsed_seconds": elapsed_seconds,
                                "flood_wait_count": int(flood_wait_count),
                                "flood_wait_seconds": float(flood_wait_seconds),
                                "used_stride_streams": 1,
                            }

                        used_streams = 1
                        downloaded_bytes = 0
                        if not force_linear_mode and self._should_use_strided_download(
                            file_size, effective_stride_streams
                        ):
                            logger.debug(
                                "Download msg_id=%s using strided mode streams=%d request_size=%d",
                                effective_msg_id,
                                int(effective_stride_streams),
                                int(self._tg_request_size),
                            )
                            downloaded_bytes = await self._download_strided(
                                client,
                                active_message,
                                target_path,
                                file_size,
                                progress_callback,
                                streams=effective_stride_streams,
                            )
                            used_streams = int(max(1, effective_stride_streams))
                        else:
                            downloaded = 0
                            with open(target_path, "wb") as f:
                                async for chunk in client.iter_download(
                                    active_message, request_size=self._tg_request_size
                                ):
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    if progress_callback:
                                        progress_callback(
                                            downloaded, file_size or downloaded
                                        )
                            downloaded_bytes = int(downloaded)
                        if downloaded_bytes <= 0:
                            try:
                                downloaded_bytes = int(target_path.stat().st_size)
                            except OSError:
                                downloaded_bytes = max(0, int(file_size))
                        elapsed_seconds = max(0.001, time.monotonic() - overall_started)
                        logger.info(
                            (
                                "Download chunk done: msg_id=%s bytes=%d elapsed=%.3fs "
                                "streams=%d flood=%d(%.1fs)"
                            ),
                            effective_msg_id,
                            int(downloaded_bytes),
                            elapsed_seconds,
                            int(used_streams),
                            int(flood_wait_count),
                            float(flood_wait_seconds),
                        )
                        self._get_file_limiter.record_success()
                        return {
                            "downloaded_bytes": int(downloaded_bytes),
                            "elapsed_seconds": elapsed_seconds,
                            "flood_wait_count": int(flood_wait_count),
                            "flood_wait_seconds": float(flood_wait_seconds),
                            "flood_wait_live_recorded": bool(flood_wait_live_recorded),
                            "used_stride_streams": int(used_streams),
                        }
            except (ConnectionError, TimeoutError) as exc:
                # All retries exhausted with a connection error — the proxy may
                # have died. Try the next level in the chain (backup->direct).
                await self._on_persistent_connection_failure(client, exc)
                raise
            except FileReferenceExpiredError:
                if effective_msg_id <= 0:
                    raise
                file_ref_refreshes += 1
                if file_ref_refreshes > file_ref_refresh_cap:
                    logger.error(
                        "Download message refresh exhausted: msg_id=%s attempts=%d",
                        effective_msg_id,
                        file_ref_refreshes,
                    )
                    raise
                refresh_chat = (
                    chat if chat is not None else await self._chat_for_client(client)
                )
                active_message = await self._fetch_single_message(
                    client, refresh_chat, effective_msg_id
                )
                if hasattr(active_message, "file") and active_message.file is not None:
                    file_size = int(active_message.file.size or 0)
                logger.warning(
                    "Download file_reference expired: msg_id=%s refresh=%d/%d",
                    effective_msg_id,
                    file_ref_refreshes,
                    file_ref_refresh_cap,
                )
                continue
            except FloodWaitError as exc:
                wait_seconds = float(max(0, int(getattr(exc, "seconds", 0))))
                self._get_file_limiter.record_flood_wait(wait_seconds)
                flood_wait_count += 1
                flood_wait_seconds += wait_seconds
                previous_streams = int(effective_stride_streams)
                if effective_stride_streams > 1:
                    if flood_wait_count <= 1:
                        effective_stride_streams = max(1, effective_stride_streams - 1)
                    else:
                        effective_stride_streams = max(1, effective_stride_streams // 2)
                if flood_wait_count >= 2 or wait_seconds >= 2.0:
                    force_linear_mode = True
                    effective_stride_streams = 1
                if callable(on_flood_wait):
                    on_flood_wait(wait_seconds)
                    flood_wait_live_recorded = True
                logger.warning(
                    (
                        "Download FloodWait while fetching chunk: msg_id=%s wait=%ss total=%d(%.1fs) "
                        "stride=%d->%d linear=%s"
                    ),
                    effective_msg_id,
                    exc.seconds,
                    flood_wait_count,
                    flood_wait_seconds,
                    previous_streams,
                    int(effective_stride_streams),
                    force_linear_mode,
                )
                await asyncio.sleep(wait_seconds + self._FLOOD_WAIT_BUFFER_SECONDS)

    async def _fetch_prefix_with_retry(
        self,
        client,
        message,
        target_path: Path,
        *,
        start_offset: int,
        end_byte: int,
        msg_id: int,
    ) -> int:
        """Grow the local file ``target_path`` to ``end_byte`` bytes of the
        message's prefix (only for UNencrypted parts — the plaintext matches
        the raw message bytes, so the leading segment can be read without the
        full part). Bytes already present (``start_offset``) are not
        re-downloaded — this uses ``iter_download(offset=..., limit=...)``
        rather than a full ``download_media``. Returns the resulting file size
        on disk."""
        request_size = self._tg_request_size
        aligned_offset = (max(0, start_offset) // request_size) * request_size
        chunks_needed = max(0, -(-(end_byte - aligned_offset) // request_size))
        if chunks_needed <= 0:
            return start_offset

        target_path.parent.mkdir(parents=True, exist_ok=True)
        flood_wait_count = 0
        while True:
            try:
                async for attempt in AsyncRetrying(
                    retry=retry_if_exception(_is_retryable_error),
                    wait=wait_fixed(1),
                    stop=stop_after_attempt(self.config.retry.max_attempts),
                    reraise=True,
                ):
                    with attempt:
                        await self._get_file_limiter.acquire()
                        written = aligned_offset
                        mode = (
                            "r+b"
                            if aligned_offset > 0 and target_path.exists()
                            else "wb"
                        )
                        with open(target_path, mode) as f:
                            f.seek(aligned_offset)
                            f.truncate(aligned_offset)
                            async for chunk in client.iter_download(
                                message,
                                offset=aligned_offset,
                                request_size=request_size,
                                limit=chunks_needed,
                            ):
                                await self._download_bandwidth.acquire(len(chunk))
                                f.write(chunk)
                                written += len(chunk)
                        self._get_file_limiter.record_success()
                        logger.debug(
                            "Prefix fetch done: msg_id=%s bytes=%d..%d target=%s",
                            msg_id,
                            aligned_offset,
                            written,
                            str(target_path),
                        )
                        return written
            except (ConnectionError, TimeoutError) as exc:
                await self._on_persistent_connection_failure(client, exc)
                raise
            except FloodWaitError as exc:
                wait_seconds = float(max(0, int(getattr(exc, "seconds", 0))))
                self._get_file_limiter.record_flood_wait(wait_seconds)
                flood_wait_count += 1
                if flood_wait_count > max(1, int(self.config.retry.max_attempts)):
                    raise
                logger.warning(
                    "Prefix fetch FloodWait: msg_id=%s wait=%ss attempt=%d",
                    msg_id,
                    exc.seconds,
                    flood_wait_count,
                )
                await asyncio.sleep(wait_seconds + self._FLOOD_WAIT_BUFFER_SECONDS)

    async def _download_strided(
        self,
        client,
        message,
        target_path: Path,
        file_size: int,
        progress_callback=None,
        streams: int | None = None,
    ) -> int:
        """Download one Telegram file using N parallel stride streams."""
        n = max(2, int(streams or self._stride_streams))
        stride = n * self._tg_request_size
        msg_id = int(getattr(message, "id", 0) or 0)
        logger.debug(
            "Strided download start: msg_id=%s streams=%d request=%d stride=%d file_size=%d",
            msg_id,
            n,
            int(self._tg_request_size),
            int(stride),
            int(file_size),
        )

        # Pre-allocate file so parallel writers can seek to correct offsets
        await asyncio.to_thread(_preallocate_file, target_path, file_size)

        downloaded_total = 0
        lock = asyncio.Lock()

        async def stream(idx: int) -> None:
            nonlocal downloaded_total
            await self._get_file_limiter.acquire()
            write_offset = idx * self._tg_request_size
            with open(target_path, "r+b") as f:
                async for chunk in client.iter_download(
                    message,
                    offset=idx * self._tg_request_size,
                    stride=stride,
                    request_size=self._tg_request_size,
                    file_size=file_size,
                ):
                    f.seek(write_offset)
                    f.write(chunk)
                    write_offset += stride
                    async with lock:
                        downloaded_total += len(chunk)
                        if progress_callback:
                            progress_callback(downloaded_total, file_size)

        await asyncio.gather(*[asyncio.create_task(stream(i)) for i in range(n)])
        logger.debug(
            "Strided download complete: msg_id=%s streams=%d downloaded=%d",
            msg_id,
            n,
            int(downloaded_total),
        )
        return int(downloaded_total)
