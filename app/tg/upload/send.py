from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telethon.errors import FilePartsInvalidError, FloodWaitError
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.core.jobs import CancelToken
from app.tg.upload._common import _is_retryable_error

logger = logging.getLogger(__name__)


class _UploadSendMixin:
    async def _send_with_retry(
        self,
        payload: bytes,
        caption: str,
        file_name: str,
        progress_callback=None,
        stats: dict[str, float | int] | None = None,
        cancel_token: CancelToken | None = None,
        client=None,
    ):
        effective_client = client or self.client
        target_chat = await self._chat_for_client(effective_client)
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
                        await self._send_media_limiter.acquire()
                        await self._upload_bandwidth.acquire(len(payload))
                        if stats is not None:
                            stats["request_count"] = (
                                int(stats.get("request_count", 0)) + 1
                            )
                        attempt_no = int(attempt.retry_state.attempt_number)
                        logger.debug(
                            "send_file payload attempt=%d name=%s bytes=%d",
                            attempt_no,
                            file_name,
                            len(payload),
                        )
                        message = await effective_client.send_file(
                            target_chat,
                            file=payload,
                            caption=caption,
                            file_name=file_name,
                            force_document=True,
                            progress_callback=progress_callback,
                        )
                        self._send_media_limiter.record_success()
                        return message
            except (ConnectionError, TimeoutError) as exc:
                # All retries exhausted with a connection error — the proxy may
                # have died. Try the next level in the chain (backup->direct).
                await self._on_persistent_connection_failure(effective_client, exc)
                raise
            except FloodWaitError as exc:
                self._send_media_limiter.record_flood_wait(
                    float(max(0, int(exc.seconds)))
                )
                if stats is not None:
                    stats["flood_wait_count"] = (
                        int(stats.get("flood_wait_count", 0)) + 1
                    )
                    stats["flood_wait_seconds"] = float(
                        stats.get("flood_wait_seconds", 0.0)
                    ) + float(max(0, int(exc.seconds)))
                cumulative_count = (
                    int(stats.get("flood_wait_count", 0)) if stats is not None else 0
                )
                cumulative_seconds = (
                    float(stats.get("flood_wait_seconds", 0.0))
                    if stats is not None
                    else 0.0
                )
                logger.warning(
                    "Upload FloodWait for payload chunk: file=%s wait=%ss cumulative=%d(%.1fs)",
                    file_name,
                    exc.seconds,
                    cumulative_count,
                    cumulative_seconds,
                )
                await self._sleep_with_cancel(
                    float(exc.seconds) + self._FLOOD_WAIT_BUFFER_SECONDS,
                    cancel_token=cancel_token,
                )

    async def _send_uploaded_file_with_retry(
        self,
        file_obj,
        *,
        caption: str,
        file_name: str,
        progress_callback=None,
        stats: dict[str, float | int] | None = None,
        cancel_token: CancelToken | None = None,
        client=None,
    ):
        effective_client = client or self.client
        target_chat = await self._chat_for_client(effective_client)
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
                        await self._send_media_limiter.acquire()
                        if stats is not None:
                            stats["request_count"] = (
                                int(stats.get("request_count", 0)) + 1
                            )
                        attempt_no = int(attempt.retry_state.attempt_number)
                        logger.debug(
                            "send_file uploaded-file attempt=%d name=%s",
                            attempt_no,
                            file_name,
                        )
                        message = await effective_client.send_file(
                            target_chat,
                            file=file_obj,
                            caption=caption,
                            file_name=file_name,
                            force_document=True,
                            progress_callback=progress_callback,
                        )
                        self._send_media_limiter.record_success()
                        return message
            except (ConnectionError, TimeoutError) as exc:
                await self._on_persistent_connection_failure(effective_client, exc)
                raise
            except FloodWaitError as exc:
                self._send_media_limiter.record_flood_wait(
                    float(max(0, int(exc.seconds)))
                )
                if stats is not None:
                    stats["flood_wait_count"] = (
                        int(stats.get("flood_wait_count", 0)) + 1
                    )
                    stats["flood_wait_seconds"] = float(
                        stats.get("flood_wait_seconds", 0.0)
                    ) + float(max(0, int(exc.seconds)))
                logger.warning(
                    "Upload FloodWait for uploaded-file send: file=%s wait=%ss",
                    file_name,
                    exc.seconds,
                )
                await self._sleep_with_cancel(
                    float(exc.seconds) + self._FLOOD_WAIT_BUFFER_SECONDS,
                    cancel_token=cancel_token,
                )

    async def _send_path_with_retry(
        self,
        file_path: Path,
        caption: str,
        file_name: str,
        progress_callback=None,
        stats: dict[str, float | int] | None = None,
        cancel_token: CancelToken | None = None,
        client=None,
        expected_size: int | None = None,
    ):
        effective_client = client or self.client
        target_chat = await self._chat_for_client(effective_client)
        payload_fallback_attempted = False
        while True:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            try:
                live_size = int(file_path.stat().st_size)
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"File not found: {file_path}") from exc
            except OSError as exc:
                raise RuntimeError(f"Failed to stat upload file: {file_path}") from exc

            if live_size <= 0:
                if expected_size is not None and int(expected_size) > 0:
                    raise RuntimeError(
                        "File changed during upload: expected "
                        f"{int(expected_size)} bytes, got {live_size} bytes ({file_path})"
                    )
                logger.info(
                    "Upload empty-file fallback: sending stub payload for %s",
                    file_name,
                )
                return await self._send_with_retry(
                    payload=self._EMPTY_FILE_STUB_PAYLOAD,
                    caption=caption,
                    file_name=file_name,
                    progress_callback=progress_callback,
                    stats=stats,
                    cancel_token=cancel_token,
                    client=effective_client,
                )
            if expected_size is not None and int(expected_size) != live_size:
                raise RuntimeError(
                    "File changed during upload: expected "
                    f"{int(expected_size)} bytes, got {live_size} bytes ({file_path})"
                )
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
                        await self._send_media_limiter.acquire()
                        await self._upload_bandwidth.acquire(int(live_size))
                        if stats is not None:
                            stats["request_count"] = (
                                int(stats.get("request_count", 0)) + 1
                            )
                        attempt_no = int(attempt.retry_state.attempt_number)
                        logger.debug(
                            "send_file path attempt=%d name=%s path=%s",
                            attempt_no,
                            file_name,
                            str(file_path),
                        )
                        message = await effective_client.send_file(
                            target_chat,
                            file=str(file_path),
                            caption=caption,
                            file_name=file_name,
                            force_document=True,
                            progress_callback=progress_callback,
                        )
                        self._send_media_limiter.record_success()
                        return message
            except (ConnectionError, TimeoutError) as exc:
                await self._on_persistent_connection_failure(effective_client, exc)
                raise
            except FilePartsInvalidError:
                if payload_fallback_attempted:
                    raise
                payload_fallback_attempted = True
                if live_size <= self._DIRECT_PARALLEL_BIGFILE_MIN_BYTES:
                    logger.warning(
                        "Upload path got FilePartsInvalid; retrying as in-memory payload: file=%s bytes=%d",
                        file_name,
                        live_size,
                    )
                    payload = await asyncio.to_thread(file_path.read_bytes)
                    if len(payload) != live_size:
                        raise RuntimeError(
                            "File changed during upload fallback: "
                            f"expected {live_size} bytes, got {len(payload)} bytes ({file_path})"
                        )
                    return await self._send_with_retry(
                        payload=payload,
                        caption=caption,
                        file_name=file_name,
                        progress_callback=progress_callback,
                        stats=stats,
                        cancel_token=cancel_token,
                        client=effective_client,
                    )
                raise
            except FloodWaitError as exc:
                self._send_media_limiter.record_flood_wait(
                    float(max(0, int(exc.seconds)))
                )
                if stats is not None:
                    stats["flood_wait_count"] = (
                        int(stats.get("flood_wait_count", 0)) + 1
                    )
                    stats["flood_wait_seconds"] = float(
                        stats.get("flood_wait_seconds", 0.0)
                    ) + float(max(0, int(exc.seconds)))
                logger.warning(
                    "Upload FloodWait for direct file upload: file=%s wait=%ss",
                    file_name,
                    exc.seconds,
                )
                await self._sleep_with_cancel(
                    float(exc.seconds) + self._FLOOD_WAIT_BUFFER_SECONDS,
                    cancel_token=cancel_token,
                )
