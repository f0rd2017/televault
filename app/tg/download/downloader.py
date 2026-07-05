from __future__ import annotations

import asyncio
import logging
import shutil
import time


from app.core.jobs import CancelToken
from app.core.rate_limiter import AdaptiveRateLimiter, BandwidthLimiter
from app.tg.adaptive import _AdaptiveDownloadController
from app.core.transfer_progress import TransferProgressAggregator
from app.core.types import AppConfig, PartRecord, TgTransferLimits
from app.core.utils import (
    build_safe_output_path,
    ensure_dir,
    file_key_from_sha256,
    normalize_folder_path,
)
from app.db.repo import DbRepo
from app.tg.download.analytics import _DownloadAnalyticsMixin
from app.tg.download.batch import _DownloadBatchMixin
from app.tg.download.fetch import _DownloadFetchMixin
from app.tg.download.merge import _DownloadMergeMixin
from app.tg.proxy_escalation import ProxyEscalationMixin

logger = logging.getLogger(__name__)


class TgDownloader(
    _DownloadFetchMixin,
    _DownloadMergeMixin,
    _DownloadBatchMixin,
    _DownloadAnalyticsMixin,
    ProxyEscalationMixin,
):
    _MAX_REQUEST_SIZE = 524288
    _MERGE_BUFFER_SIZE = 2 * 1024 * 1024
    _MANIFEST_NAME = "manifest.json"
    _REGULAR_PART_CONCURRENCY_START = 2
    _PREMIUM_PART_CONCURRENCY_START = 3
    _REGULAR_PART_CONCURRENCY_CAP = 6
    _PREMIUM_PART_CONCURRENCY_CAP = 8
    _REGULAR_STRIDE_STREAMS_START = 1
    _PREMIUM_STRIDE_STREAMS_START = 2
    _REGULAR_STRIDE_STREAMS = 2
    _PREMIUM_STRIDE_STREAMS = 2
    _REGULAR_TOTAL_STREAM_BUDGET = 8
    _PREMIUM_TOTAL_STREAM_BUDGET = 16
    _AUTO_BOOST_PART_CONCURRENCY_REGULAR = 5
    _AUTO_BOOST_PART_CONCURRENCY_PREMIUM = 6
    _STRIDE_MIN_REQUEST_MULTIPLIER = 16
    _FLOOD_WAIT_BUFFER_SECONDS = 0.25
    _TELEMETRY_LOG_INTERVAL_SEC = 1.0
    _MANIFEST_WRITE_THROTTLE_SEC = 0.5

    def __init__(
        self,
        config: AppConfig,
        repo: DbRepo,
        client,
        chat,
        chat_id: str,
        transfer_limits: TgTransferLimits | None = None,
        extra_clients=None,
        download_endpoints=None,
    ) -> None:
        self.config = config
        self.repo = repo
        self.client = client
        self.chat = chat
        self.chat_id = chat_id
        self.transfer_limits = transfer_limits or TgTransferLimits()
        if isinstance(download_endpoints, dict):
            flattened_endpoints: list[object] = []
            for endpoints in download_endpoints.values():
                if isinstance(endpoints, list):
                    flattened_endpoints.extend(endpoints)
            self._download_endpoints = flattened_endpoints
        else:
            self._download_endpoints = list(download_endpoints or [])
        self._client_pool = [client] + list(extra_clients or [])
        self._client_chat_cache: dict[int, object] = {}
        self._client_label_cache: dict[int, str] = {id(client): "main"}
        self._client_chat_lock = asyncio.Lock()
        self._download_routes_by_chat_id: dict[
            str, list[tuple[object, object, str]]
        ] = {}
        self._download_route_cursor_by_chat_id: dict[str, int] = {}
        self._register_download_route(str(chat_id), client, chat, "main")
        if self._download_endpoints:
            unique_clients: list[object] = []
            seen_client_ids: set[int] = set()
            for endpoint in self._download_endpoints:
                endpoint_client = getattr(endpoint, "client", None)
                endpoint_chat = getattr(endpoint, "chat", None)
                endpoint_chat_id = str(getattr(endpoint, "chat_id", "") or "").strip()
                if (
                    endpoint_client is None
                    or endpoint_chat is None
                    or not endpoint_chat_id
                ):
                    continue
                label = str(getattr(endpoint, "label", "client") or "client")
                self._register_download_route(
                    endpoint_chat_id, endpoint_client, endpoint_chat, label
                )
                cache_key = id(endpoint_client)
                if cache_key not in seen_client_ids:
                    seen_client_ids.add(cache_key)
                    unique_clients.append(endpoint_client)
            if unique_clients:
                self._client_pool = unique_clients
        requested_size = int(
            self.transfer_limits.request_size_bytes or self._MAX_REQUEST_SIZE
        )
        self._tg_request_size = max(
            64 * 1024, min(self._MAX_REQUEST_SIZE, requested_size)
        )
        if self.transfer_limits.is_premium:
            self._download_part_concurrency_start = self._PREMIUM_PART_CONCURRENCY_START
            self._download_part_concurrency_cap = self._PREMIUM_PART_CONCURRENCY_CAP
            self._download_part_concurrency_autoboost = (
                self._AUTO_BOOST_PART_CONCURRENCY_PREMIUM
            )
            self._stride_streams_start = self._PREMIUM_STRIDE_STREAMS_START
            self._stride_streams = self._PREMIUM_STRIDE_STREAMS
            self._download_total_stream_budget = self._PREMIUM_TOTAL_STREAM_BUDGET
        else:
            self._download_part_concurrency_start = self._REGULAR_PART_CONCURRENCY_START
            self._download_part_concurrency_cap = self._REGULAR_PART_CONCURRENCY_CAP
            self._download_part_concurrency_autoboost = (
                self._AUTO_BOOST_PART_CONCURRENCY_REGULAR
            )
            self._stride_streams_start = self._REGULAR_STRIDE_STREAMS_START
            self._stride_streams = self._REGULAR_STRIDE_STREAMS
            self._download_total_stream_budget = self._REGULAR_TOTAL_STREAM_BUDGET
        get_rate = float(max(0.1, getattr(self.config, "get_file_rate_limit", 16.0)))
        get_window = float(
            max(0.2, getattr(self.config, "perf_telemetry_window_sec", 1.0))
        )
        self._get_file_limiter = AdaptiveRateLimiter(
            initial_rate=get_rate,
            min_rate=max(0.1, get_rate * 0.2),
            max_rate=max(get_rate, get_rate * 4.0),
            window_sec=get_window,
        )
        # Download bandwidth limit (MB/s), shared across the instance. 0 = no limit.
        self._download_bandwidth = BandwidthLimiter(
            float(getattr(self.config, "download_throttle_mbps", 0.0))
        )
        self._blob_cache_locks: dict[str, asyncio.Lock] = {}
        self._stream_prefix_locks: dict[str, asyncio.Lock] = {}

    async def fetch_parts_decrypted(
        self,
        folder_path: str,
        file_key: str,
        part_indices: list[int],
        cache_dir: str,
        *,
        cancel_token: CancelToken | None = None,
        prefix_bytes: dict[int, int] | None = None,
    ) -> dict[int, str]:
        """Download and decrypt ONLY the requested parts into ``cache_dir`` (streaming,
        increment 9/10). Returns ``{part_index: path to the decrypted plaintext part file}``.
        Already-cached parts are reused — seeking within a video doesn't re-download
        them. Reuses the regular route/download/decrypt infrastructure; the transfer
        engine is unchanged.

        ``prefix_bytes`` (optional): ``{part_index: how many bytes from the start
        of the part are actually needed right now}``. Parts can be hundreds of MB —
        without this, the player would wait for the ENTIRE part to download even
        though the current stream window only needs a small chunk at its start.
        This only works for UNencrypted objects (in which case the plaintext
        matches the message's raw bytes, so the prefix can be read without fully
        decrypting the whole part). A segment already downloaded to disk is GROWN
        to the required length (only the missing tail is fetched) rather than
        re-downloaded from scratch. With encryption enabled (or if part_index isn't
        listed in prefix_bytes), the part is downloaded in full, as before."""
        import os
        import uuid
        from pathlib import Path

        token = cancel_token or CancelToken()
        normalized_folder = normalize_folder_path(folder_path)
        wanted = {int(i) for i in part_indices}
        cache = Path(cache_dir).expanduser()
        cache.mkdir(parents=True, exist_ok=True)

        all_parts = self.repo.get_parts_for_object(
            folder_path=normalized_folder, file_key=file_key
        )
        if not all_parts:
            raise ValueError("No parts found for requested object")
        selected = [p for p in all_parts if int(p.part_index) in wanted]
        if not selected:
            return {}

        crypto_key: bytes | None = None
        if self.config.crypto.enabled:
            if not self.config.crypto.key_env:
                raise ValueError(
                    "crypto.key_env is required when encryption is enabled"
                )
            from app.core.utils import load_aesgcm_key_from_env

            crypto_key = load_aesgcm_key_from_env(self.config.crypto.key_env)

        prefix_bytes = prefix_bytes or {}

        result: dict[int, str] = {}
        need_fetch: list[PartRecord] = []
        need_prefix: list[tuple[PartRecord, int]] = []
        for part in selected:
            out_path = cache / f"part_{int(part.part_index):08d}.bin"
            full_len = int(part.file_size or 0)
            try:
                current_size = out_path.stat().st_size if out_path.exists() else 0
            except OSError:
                current_size = 0
            if full_len > 0 and current_size >= full_len:
                result[int(part.part_index)] = str(out_path)
                continue
            wanted_len = full_len
            if (
                crypto_key is None
                and full_len > 0
                and int(part.part_index) in prefix_bytes
            ):
                wanted_len = max(
                    1, min(full_len, int(prefix_bytes[int(part.part_index)]))
                )
            if current_size > 0 and current_size >= wanted_len:
                result[int(part.part_index)] = str(out_path)
            elif wanted_len < full_len:
                need_prefix.append((part, wanted_len))
            else:
                need_fetch.append(part)

        if not need_fetch and not need_prefix:
            return result

        routes = await self._fetch_messages_by_chat(
            need_fetch + [part for part, _ in need_prefix]
        )

        # Download the window's parts in PARALLEL (not one after another) — the
        # player waits for the whole range to land on disk, and sequential
        # downloading directly translated into first-frame delay/buffering. The
        # TG connection budget (normally occupied by parts via part_concurrency
        # during a regular download) is entirely free here — streaming only
        # downloads one window at a time — so we split it across this window's
        # concurrent parts.
        total_jobs = len(need_fetch) + len(need_prefix)
        concurrency = max(1, min(total_jobs, self._download_part_concurrency_cap))
        semaphore = asyncio.Semaphore(concurrency)
        per_part_streams = max(1, self._download_total_stream_budget // concurrency)

        async def _fetch_one(part: PartRecord) -> None:
            async with semaphore:
                token.raise_if_cancelled()
                out_path = cache / f"part_{int(part.part_index):08d}.bin"
                route = routes.get((str(part.chat_id), int(part.msg_id)))
                if route is None:
                    raise ValueError(
                        f"Missing telegram message for part chat_id={part.chat_id} "
                        f"msg_id={part.msg_id}"
                    )
                dl_client, dl_chat, msg, _label = route
                # Unique temp files per request: FFmpeg's parallel Range connections
                # (especially for .avi — header at the start + index at the end) pull
                # the same parts SIMULTANEOUSLY. With shared part_*.bin/.enc names they
                # would clobber each other, and a half-written .bin would look like a
                # valid cache entry. We download into a temp file and publish the
                # finished part atomically via os.replace (last-writer-wins is fine —
                # the content is identical, so there's no real conflict).
                uniq = f"{os.getpid()}_{uuid.uuid4().hex}"
                tmp_bin = cache / f"part_{int(part.part_index):08d}.{uniq}.bin.tmp"
                tmp_enc = cache / f"part_{int(part.part_index):08d}.{uniq}.enc.tmp"
                dl_target = tmp_enc if crypto_key is not None else tmp_bin
                try:
                    await self._download_with_retry(
                        dl_client,
                        msg,
                        dl_target,
                        None,
                        chat=dl_chat,
                        msg_id=int(part.msg_id),
                        part_concurrency=concurrency,
                        stride_streams=per_part_streams,
                    )
                    if crypto_key is not None:
                        await asyncio.to_thread(
                            self._decrypt_file_to_file, tmp_enc, tmp_bin, crypto_key
                        )
                    os.replace(tmp_bin, out_path)
                finally:
                    for _leftover in (tmp_enc, tmp_bin):
                        try:
                            _leftover.unlink(missing_ok=True)
                        except OSError:
                            pass
                result[int(part.part_index)] = str(out_path)

        async def _fetch_prefix_one(part: PartRecord, target_len: int) -> None:
            async with semaphore:
                token.raise_if_cancelled()
                out_path = cache / f"part_{int(part.part_index):08d}.bin"
                route = routes.get((str(part.chat_id), int(part.msg_id)))
                if route is None:
                    raise ValueError(
                        f"Missing telegram message for part chat_id={part.chat_id} "
                        f"msg_id={part.msg_id}"
                    )
                dl_client, _dl_chat, msg, _label = route
                # The same part can grow across several consecutive stream windows
                # (as the player advances through time) — we lock per file so the
                # main request and a background prefetch don't grow the same file
                # in parallel (otherwise their truncate/seek calls would clobber
                # each other).
                lock_key = str(out_path)
                lock = self._stream_prefix_locks.setdefault(lock_key, asyncio.Lock())
                async with lock:
                    try:
                        current_size = (
                            out_path.stat().st_size if out_path.exists() else 0
                        )
                    except OSError:
                        current_size = 0
                    if current_size < target_len:
                        await self._fetch_prefix_with_retry(
                            dl_client,
                            msg,
                            out_path,
                            start_offset=current_size,
                            end_byte=target_len,
                            msg_id=int(part.msg_id),
                            streams=per_part_streams,
                        )
                result[int(part.part_index)] = str(out_path)

        await asyncio.gather(
            *(_fetch_one(part) for part in need_fetch),
            *(_fetch_prefix_one(part, target_len) for part, target_len in need_prefix),
        )
        return result

    async def chunked_download(
        self,
        folder_path: str,
        file_key: str,
        allow_incomplete: bool = False,
        integrity_mode: str | None = None,
        cancel_token: CancelToken | None = None,
        progress_cb=None,
        _storage_override: str | None = None,
        dest_root: str | None = None,
    ) -> dict[str, object]:
        operation_started = time.monotonic()
        token = cancel_token or CancelToken()
        normalized_folder = normalize_folder_path(folder_path)
        resolved_integrity_mode = (
            str(integrity_mode or self.config.download_integrity_mode).strip().lower()
        )
        if resolved_integrity_mode not in {"strict", "fast"}:
            raise ValueError("integrity_mode must be 'strict' or 'fast'")
        storage_kind = (
            str(
                _storage_override
                or self.repo.resolve_object_storage(normalized_folder, file_key)
            )
            .strip()
            .lower()
        )
        if storage_kind == "batch_member":
            return await self._download_batch_member(
                folder_path=normalized_folder,
                file_key=file_key,
                integrity_mode=resolved_integrity_mode,
                cancel_token=token,
                progress_cb=progress_cb,
                dest_root=dest_root,
            )

        parts_fetch_started = time.monotonic()
        parts = self.repo.get_parts_for_object(
            folder_path=normalized_folder, file_key=file_key
        )
        parts_fetch_elapsed = max(0.0, time.monotonic() - parts_fetch_started)
        if not parts:
            raise ValueError("No parts found for requested object")

        expected_sha256 = self._extract_expected_sha256(
            parts, self.config.caption_prefix
        )
        expected_output_size = self._extract_expected_orig_size(
            parts, self.config.caption_prefix
        )
        integrity_result_mode = (
            "fast"
            if resolved_integrity_mode == "fast"
            else ("full_sha256" if expected_sha256 else "strict_fallback")
        )

        parts_total = max(part.parts_total for part in parts)
        if len(parts) != parts_total and not allow_incomplete:
            raise ValueError("Object is incomplete and allow_incomplete is false")

        output_root = dest_root or self.config.download_root
        ensure_dir(output_root)
        output_path = build_safe_output_path(
            output_root, normalized_folder, parts[0].orig_name
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        crypto_key: bytes | None = None
        if self.config.crypto.enabled:
            if not self.config.crypto.key_env:
                raise ValueError(
                    "crypto.key_env is required when encryption is enabled"
                )
            from app.core.utils import load_aesgcm_key_from_env

            crypto_key = load_aesgcm_key_from_env(self.config.crypto.key_env)

        ordered_parts = sorted(parts, key=lambda p: p.part_index)
        allow_zero_size_stub = expected_output_size == 0
        expected_size_by_part: dict[int, int | None] = {
            part.part_index: int(part.file_size) if part.file_size is not None else None
            for part in ordered_parts
        }
        messages_fetch_started = time.monotonic()
        message_routes = await self._fetch_messages_by_chat(ordered_parts)
        messages_fetch_elapsed = max(0.0, time.monotonic() - messages_fetch_started)

        temp_dir = output_path.parent / f".{output_path.name}.{file_key}.parts"
        resume_validate_started = time.monotonic()
        resume_state = self._load_manifest(temp_dir)
        if not temp_dir.exists():
            temp_dir.mkdir(parents=True, exist_ok=True)
        else:
            manifest_ok = self._is_manifest_compatible(
                resume_state,
                file_key=file_key,
                parts_total=parts_total,
            )
            # Keep already downloaded part files even if manifest is missing/corrupted.
            if resume_state is not None and not manifest_ok:
                self._prepare_temp_dir(temp_dir)
                resume_state = None

        completed_parts: set[int] = set()
        resume_completed_parts = 0
        resume_completed_bytes = 0
        if resume_state is not None:
            for part_id in resume_state.get("completed_parts", []):
                if isinstance(part_id, int):
                    completed_parts.add(part_id)

        # Validate existing parts for resume.
        for part in ordered_parts:
            part_id = part.part_index
            part_path = temp_dir / f"part_{part_id:08d}.bin"
            if not part_path.exists():
                completed_parts.discard(part_id)
                continue
            try:
                size = part_path.stat().st_size
            except OSError:
                completed_parts.discard(part_id)
                continue
            if size <= 0:
                part_path.unlink(missing_ok=True)
                completed_parts.discard(part_id)
                continue
            expected_size = expected_size_by_part.get(part_id)
            if (
                expected_size is not None
                and crypto_key is None
                and size != expected_size
            ):
                if allow_zero_size_stub and expected_size == 0 and size > 0:
                    completed_parts.add(part_id)
                    resume_completed_parts += 1
                    resume_completed_bytes += int(size)
                    continue
                part_path.unlink(missing_ok=True)
                completed_parts.discard(part_id)
                continue
            completed_parts.add(part_id)
            resume_completed_parts += 1
            resume_completed_bytes += int(size)

        manifest_write_seconds = 0.0
        manifest_write_started = time.monotonic()
        self._write_manifest(
            temp_dir,
            file_key=file_key,
            parts_total=parts_total,
            completed_parts=completed_parts,
        )
        manifest_write_seconds += max(0.0, time.monotonic() - manifest_write_started)
        resume_validate_elapsed = max(0.0, time.monotonic() - resume_validate_started)

        progress_agg: TransferProgressAggregator | None = None
        if progress_cb is not None:
            progress_agg = TransferProgressAggregator(
                total_parts=len(ordered_parts),
                total_bytes_hint=sum(
                    max(0, int(p.file_size or 0)) for p in ordered_parts
                ),
                emit_interval_ms=300,
                percent_step=2.0,
                activity="Downloading",
            )
            await progress_agg.start(progress_cb)
            for part in ordered_parts:
                if part.part_index not in completed_parts:
                    continue
                expected_size = expected_size_by_part.get(part.part_index)
                if expected_size is None or expected_size <= 0:
                    continue
                progress_agg.on_part_progress(
                    part.part_index, expected_size, expected_size
                )

        def make_part_progress_cb(part_id: int):
            def on_progress(current: int, total: int) -> None:
                if progress_agg is not None:
                    progress_agg.on_part_progress(part_id, int(current), int(total))

            return on_progress

        remaining_parts = [
            p for p in ordered_parts if p.part_index not in completed_parts
        ]

        # Cap part_concurrency by number of unique clients available for this file.
        # If all parts go through 1 client, concurrency=1 avoids FloodWait from
        # multiple concurrent iter_download calls competing for the same rate limit.
        unique_clients_for_file: set[str] = set()
        for part in remaining_parts:
            route = message_routes.get((str(part.chat_id), int(part.msg_id)))
            if route is not None:
                _, _, _, client_label = route
                unique_clients_for_file.add(str(client_label))
        unique_client_count = max(1, len(unique_clients_for_file))

        configured_part_concurrency = max(1, int(self.config.concurrency))
        part_concurrency_target = min(configured_part_concurrency, unique_client_count)
        max_part_concurrency = max(
            1,
            min(
                part_concurrency_target,
                self._download_part_concurrency_cap,
                len(remaining_parts) or 1,
            ),
        )
        initial_part_concurrency = max(
            1,
            min(self._download_part_concurrency_start, max_part_concurrency),
        )
        max_stride_streams = max(
            1,
            min(int(self._stride_streams), int(self._download_total_stream_budget)),
        )
        initial_stride_streams = max(
            1,
            min(int(self._stride_streams_start), int(max_stride_streams)),
        )
        adaptive = _AdaptiveDownloadController(
            initial_part_concurrency=initial_part_concurrency,
            max_part_concurrency=max_part_concurrency,
            initial_stride_streams=initial_stride_streams,
            max_stride_streams=max_stride_streams,
            total_stream_budget=self._download_total_stream_budget,
            is_premium=bool(self.transfer_limits.is_premium),
        )
        initial_part_concurrency, initial_stride_streams = adaptive.snapshot()
        logger.info(
            (
                "Download profile: file_key=%s parts_total=%d remaining=%d integrity=%s "
                "part_concurrency=%d stride=%d request=%d resume_parts=%d resume_bytes=%d "
                "cfg_concurrency=%d auto_part_target=%d"
            ),
            file_key,
            parts_total,
            len(remaining_parts),
            resolved_integrity_mode,
            initial_part_concurrency,
            initial_stride_streams,
            int(self._tg_request_size),
            resume_completed_parts,
            resume_completed_bytes,
            int(self.config.concurrency),
            int(self._download_part_concurrency_autoboost),
        )
        queue: asyncio.Queue[PartRecord] = asyncio.Queue()
        for part in remaining_parts:
            route = message_routes.get((str(part.chat_id), int(part.msg_id)))
            if route is None:
                raise ValueError(
                    f"Missing telegram message for part chat_id={part.chat_id} msg_id={part.msg_id}"
                )
            queue.put_nowait(part)

        transfer_started = time.monotonic()
        downloaded_bytes_total = int(resume_completed_bytes)
        completed_parts_count = int(resume_completed_parts)
        telemetry_last_ts = transfer_started
        telemetry_last_bytes = int(downloaded_bytes_total)

        def log_download_telemetry(*, reason: str, force: bool = False) -> None:
            nonlocal telemetry_last_ts, telemetry_last_bytes
            now = time.monotonic()
            if (
                not force
                and (now - telemetry_last_ts) < self._TELEMETRY_LOG_INTERVAL_SEC
            ):
                return
            elapsed_total = max(0.001, now - transfer_started)
            elapsed_window = max(0.001, now - telemetry_last_ts)
            window_bytes = max(0, int(downloaded_bytes_total - telemetry_last_bytes))
            avg_speed = (
                float(max(0, downloaded_bytes_total))
                / elapsed_total
                / (1024.0 * 1024.0)
            )
            window_speed = float(window_bytes) / elapsed_window / (1024.0 * 1024.0)
            state = adaptive.state()
            adaptive_snapshot = adaptive.summary()
            logger.info(
                (
                    "Download telemetry: file_key=%s reason=%s parts=%d/%d bytes=%d "
                    "window=%.2f MB/s avg=%.2f MB/s queue=%d slots=%d/%d part_target=%d stride=%d flood=%d(%.1fs)"
                ),
                file_key,
                reason,
                completed_parts_count,
                parts_total,
                downloaded_bytes_total,
                window_speed,
                avg_speed,
                queue.qsize(),
                int(state.get("active_slots", 0)),
                int(state.get("max_part_concurrency", 1)),
                int(state.get("target_part_concurrency", 1)),
                int(state.get("effective_stride_streams", 1)),
                int(adaptive_snapshot.get("flood_wait_count", 0)),
                float(adaptive_snapshot.get("flood_wait_seconds", 0.0)),
            )
            telemetry_last_ts = now
            telemetry_last_bytes = int(downloaded_bytes_total)

        manifest_lock = asyncio.Lock()
        telemetry_lock = asyncio.Lock()
        last_manifest_write_ts = 0.0

        async def mark_part_done(part_id: int, *, force: bool = False) -> None:
            # Resume detection re-validates on-disk part files independently of the
            # manifest's completed_parts list, so the per-part disk write can be
            # throttled to avoid stalling the loop on many-part downloads. The set
            # is always updated immediately; a final force-write flushes it.
            nonlocal manifest_write_seconds, last_manifest_write_ts
            async with manifest_lock:
                completed_parts.add(part_id)
                now = time.monotonic()
                if (
                    not force
                    and (now - last_manifest_write_ts)
                    < self._MANIFEST_WRITE_THROTTLE_SEC
                ):
                    return
                write_started = time.monotonic()
                self._write_manifest(
                    temp_dir,
                    file_key=file_key,
                    parts_total=parts_total,
                    completed_parts=completed_parts,
                )
                manifest_write_seconds += max(0.0, time.monotonic() - write_started)
                last_manifest_write_ts = time.monotonic()

        network_download_seconds = 0.0
        decrypt_seconds = 0.0
        channel_payload_bytes: dict[str, int] = {}
        channel_parts_count: dict[str, int] = {}
        clients_used: set[str] = set()

        def make_worker(worker_idx: int):
            _ = worker_idx

            async def worker() -> None:
                nonlocal \
                    network_download_seconds, \
                    decrypt_seconds, \
                    downloaded_bytes_total, \
                    completed_parts_count
                while True:
                    try:
                        part = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    try:
                        token.raise_if_cancelled()
                        part_id = part.part_index
                        part_path = temp_dir / f"part_{part_id:08d}.bin"
                        enc_path = temp_dir / f"part_{part_id:08d}.enc"
                        target_path = enc_path if crypto_key is not None else part_path

                        route = message_routes.get(
                            (str(part.chat_id), int(part.msg_id))
                        )
                        if route is None:
                            raise ValueError(
                                f"Missing telegram message for part chat_id={part.chat_id} msg_id={part.msg_id}"
                            )
                        dl_client, dl_chat, msg, client_label = route
                        logger.debug(
                            "Download part dispatch: part=%d/%d client=%s chat_id=%s msg_id=%d",
                            int(part_id) + 1,
                            int(parts_total),
                            str(client_label),
                            str(part.chat_id),
                            int(part.msg_id),
                        )
                        await adaptive.acquire_slot(token)
                        download_stats: dict[str, object] | None = None
                        try:
                            slot_part_concurrency, slot_stride_streams = (
                                adaptive.snapshot()
                            )
                            download_stats_raw = await self._download_with_retry(
                                dl_client,
                                msg,
                                target_path,
                                make_part_progress_cb(part_id),
                                chat=dl_chat,
                                msg_id=part.msg_id,
                                part_concurrency=slot_part_concurrency,
                                stride_streams=slot_stride_streams,
                                on_flood_wait=adaptive.record_flood_wait,
                            )
                            download_stats = (
                                download_stats_raw
                                if isinstance(download_stats_raw, dict)
                                else {}
                            )
                            network_download_seconds += float(
                                max(
                                    0.0,
                                    float(download_stats.get("elapsed_seconds") or 0.0),
                                )
                            )
                        finally:
                            await adaptive.release_slot()
                        # Do not upscale/downscale from late samples when there is no queued work left.
                        # This keeps reported effective concurrency aligned with actual useful parallelism.
                        if queue.qsize() > 0:
                            adaptive.record_sample(download_stats)
                        if not target_path.exists():
                            raise ValueError(
                                f"Download did not produce chunk file for msg_id={part.msg_id}"
                            )

                        downloaded_size = target_path.stat().st_size
                        expected_size = expected_size_by_part.get(part_id)
                        if (
                            expected_size is not None
                            and crypto_key is None
                            and resolved_integrity_mode == "fast"
                            and downloaded_size != expected_size
                        ):
                            target_path.unlink(missing_ok=True)
                            raise ValueError(
                                f"Part size mismatch for part {part_id}: "
                                f"expected {expected_size}, got {downloaded_size}"
                            )

                        if progress_agg is not None:
                            progress_agg.on_part_progress(
                                part_id, downloaded_size, downloaded_size
                            )

                        if crypto_key is not None:
                            decrypt_started = time.monotonic()
                            await asyncio.to_thread(
                                self._decrypt_file_to_file,
                                enc_path,
                                part_path,
                                crypto_key,
                            )
                            decrypt_seconds += max(
                                0.0, time.monotonic() - decrypt_started
                            )
                            enc_path.unlink(missing_ok=True)
                        await mark_part_done(part_id)
                        async with telemetry_lock:
                            downloaded_bytes_total += int(downloaded_size)
                            completed_parts_count += 1
                            part_chat_id = str(part.chat_id)
                            channel_payload_bytes[part_chat_id] = int(
                                channel_payload_bytes.get(part_chat_id, 0)
                                + int(downloaded_size)
                            )
                            channel_parts_count[part_chat_id] = int(
                                channel_parts_count.get(part_chat_id, 0) + 1
                            )
                            clients_used.add(str(client_label))
                        log_download_telemetry(reason="part")
                    finally:
                        queue.task_done()

            return worker

        tasks = [
            asyncio.create_task(make_worker(i)()) for i in range(max_part_concurrency)
        ]
        log_download_telemetry(reason="start", force=True)
        download_ok = False
        digest: str | None = None
        merged_bytes = 0
        merge_started = False
        merge_seconds = 0.0
        integrity_check_seconds = 0.0
        try:
            await asyncio.gather(*tasks)
            # Manifest writes were throttled during transfer; flush the final state
            # so a later merge failure (with keep_partial_on_failure) can resume.
            async with manifest_lock:
                flush_started = time.monotonic()
                self._write_manifest(
                    temp_dir,
                    file_key=file_key,
                    parts_total=parts_total,
                    completed_parts=completed_parts,
                )
                manifest_write_seconds += max(0.0, time.monotonic() - flush_started)
            merge_order = [part.part_index for part in ordered_parts]
            merge_started = True
            merge_started_at = time.monotonic()
            digest, merged_bytes = await asyncio.to_thread(
                self._merge_parts_with_hash_sync,
                output_path,
                temp_dir,
                merge_order,
                self._MERGE_BUFFER_SIZE,
                token,
                resolved_integrity_mode == "strict",
                expected_total_size=expected_output_size,
            )
            merge_seconds = max(0.0, time.monotonic() - merge_started_at)

            integrity_started = time.monotonic()
            if resolved_integrity_mode == "strict":
                if digest is None:
                    raise ValueError("Strict integrity mode requires SHA-256 digest")
                if expected_sha256 is not None:
                    if digest.lower() != expected_sha256.lower():
                        output_path.unlink(missing_ok=True)
                        raise ValueError(
                            f"Integrity mismatch: expected full SHA-256 "
                            f"{expected_sha256}, got {digest}"
                        )
                    verified = True
                else:
                    if self._looks_like_sha_prefix(file_key):
                        integrity_result_mode = "prefix_fallback"
                        verified = file_key_from_sha256(digest) == file_key
                        if not verified:
                            output_path.unlink(missing_ok=True)
                            raise ValueError(
                                f"Integrity mismatch: expected file_key prefix "
                                f"{file_key}, got {file_key_from_sha256(digest)}"
                            )
                    else:
                        integrity_result_mode = "strict_size_fallback"
                        if expected_output_size is not None and crypto_key is None:
                            expected_total = int(expected_output_size)
                        else:
                            known_sizes = [
                                int(size)
                                for size in expected_size_by_part.values()
                                if size is not None
                            ]
                            expected_total = sum(known_sizes) if known_sizes else None
                        if expected_total is not None and crypto_key is None:
                            if merged_bytes != expected_total:
                                output_path.unlink(missing_ok=True)
                                raise ValueError(
                                    f"Strict size fallback failed: expected size {expected_total}, got {merged_bytes}"
                                )
                        verified = True
            else:
                if expected_output_size is not None and crypto_key is None:
                    expected_total = int(expected_output_size)
                else:
                    known_sizes = [
                        int(size)
                        for size in expected_size_by_part.values()
                        if size is not None
                    ]
                    expected_total = sum(known_sizes) if known_sizes else None
                if expected_total is not None and crypto_key is None:
                    if merged_bytes != expected_total:
                        output_path.unlink(missing_ok=True)
                        raise ValueError(
                            f"Fast integrity check failed: expected size {expected_total}, got {merged_bytes}"
                        )
                verified = True
            integrity_check_seconds = max(0.0, time.monotonic() - integrity_started)

            download_ok = True
        except Exception:
            if merge_started:
                output_path.unlink(missing_ok=True)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            if progress_agg is not None:
                await progress_agg.stop("Download complete" if download_ok else None)
            if download_ok or not self.config.keep_partial_on_failure:
                shutil.rmtree(temp_dir, ignore_errors=True)
        log_download_telemetry(reason="final", force=True)

        transfer_elapsed = max(0.001, time.monotonic() - transfer_started)
        speed_mbps = float(merged_bytes) / transfer_elapsed / (1024.0 * 1024.0)
        total_elapsed = max(0.001, time.monotonic() - operation_started)
        total_speed_mbps = float(merged_bytes) / total_elapsed / (1024.0 * 1024.0)
        adaptive_summary = adaptive.summary()
        analytics = self._build_download_analytics(
            phase_seconds={
                "parts_fetch": parts_fetch_elapsed,
                "messages_fetch": messages_fetch_elapsed,
                "resume_validate": resume_validate_elapsed,
                "network_download": network_download_seconds,
                "decrypt": decrypt_seconds,
                "manifest_write": manifest_write_seconds,
                "merge": merge_seconds,
                "integrity_check": integrity_check_seconds,
                "transfer": transfer_elapsed,
                "total": total_elapsed,
            },
            output_total_bytes=merged_bytes,
            resume_completed_bytes=resume_completed_bytes,
            transfer_elapsed=transfer_elapsed,
            total_elapsed=total_elapsed,
            download_profile={
                "channels_used": sorted(channel_payload_bytes.keys()),
                "parts_by_channel": {k: int(v) for k, v in channel_parts_count.items()},
                "clients_used": sorted(clients_used),
                "cross_channel_parts": bool(len(channel_payload_bytes) > 1),
            },
            requests_per_file=float(max(1, len(remaining_parts)))
            / float(max(1, len(ordered_parts))),
            batch_hit_ratio=0.0,
            blob_reuse_ratio=0.0,
            effective_part_concurrency=int(
                adaptive_summary.get("final_part_concurrency")
                or initial_part_concurrency
            ),
            effective_stride_streams=int(
                adaptive_summary.get("effective_stride_streams")
                or initial_stride_streams
            ),
            adaptive=adaptive_summary,
            payload_by_channel=channel_payload_bytes,
            resume={
                "completed_parts": int(resume_completed_parts),
                "remaining_parts": int(len(remaining_parts)),
                "parts_total": int(parts_total),
            },
        )
        logger.info(
            "Download finished: file_key=%s parts=%d bytes=%d mode=%s speed=%.2f MB/s",
            file_key,
            len(ordered_parts),
            merged_bytes,
            resolved_integrity_mode,
            speed_mbps,
        )
        logger.info(
            (
                "Download analytics: file_key=%s fetch=%.3fs resume=%.3fs net=%.3fs "
                "merge=%.3fs integrity=%.3fs total=%.3fs total_speed=%.2f MB/s"
            ),
            file_key,
            (parts_fetch_elapsed + messages_fetch_elapsed),
            resume_validate_elapsed,
            network_download_seconds,
            merge_seconds,
            integrity_check_seconds,
            total_elapsed,
            total_speed_mbps,
        )

        return {
            "output_path": str(output_path),
            "sha256": digest,
            "verified": verified,
            "expected_sha256": expected_sha256,
            "integrity_mode": integrity_result_mode,
            "integrity_error": None,
            "parts_downloaded": len(ordered_parts),
            "parts_expected": parts_total,
            "channels_used": sorted(channel_payload_bytes.keys()),
            "clients_used": sorted(clients_used),
            "cross_channel_parts": bool(len(channel_payload_bytes) > 1),
            "analytics": analytics,
        }

    async def _chat_for_client(self, tg_client):
        if tg_client is self.client:
            return self.chat

        cache_key = id(tg_client)
        cached = self._client_chat_cache.get(cache_key)
        if cached is not None:
            return cached

        async with self._client_chat_lock:
            cached = self._client_chat_cache.get(cache_key)
            if cached is not None:
                return cached

            # Resolve chat via primary account's chat_target (no config.tg_chat fallback)
            try:
                primary_chat = self.chat
                if primary_chat is not None:
                    chat_identifier = primary_chat
                elif self.chat_id:
                    fallback = str(self.chat_id).strip()
                    if fallback.lstrip("-").isdigit():
                        chat_identifier = int(fallback)
                    else:
                        chat_identifier = fallback
                else:
                    raise RuntimeError(
                        "Unable to resolve target chat for download client: "
                        "primary account has no chat_target or chat_id configured."
                    )
                resolved = await tg_client.get_entity(chat_identifier)
            except Exception as exc:
                raise RuntimeError(
                    "Unable to resolve target chat for download client. "
                    "Ensure each bot has access to the channel configured on the primary account."
                ) from exc
            self._client_chat_cache[cache_key] = resolved
            self._client_label_cache.setdefault(cache_key, "client")
            return resolved

    def _register_download_route(self, chat_id: str, client, chat, label: str) -> None:
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return
        routes = self._download_routes_by_chat_id.setdefault(normalized_chat_id, [])
        for existing_client, _, _ in routes:
            if existing_client is client:
                return
        routes.append((client, chat, str(label or "client")))
        self._client_chat_cache[id(client)] = chat
        self._client_label_cache[id(client)] = str(label or "client")

    async def _resolve_route_for_part(
        self,
        *,
        part_chat_id: str,
        seed: int = 0,
    ) -> tuple[object, object, str]:
        normalized_chat_id = str(part_chat_id or "").strip()
        if not normalized_chat_id:
            normalized_chat_id = str(self.chat_id)
        routes = self._download_routes_by_chat_id.get(normalized_chat_id, [])
        if not routes:
            if normalized_chat_id == str(self.chat_id):
                routes = [(self.client, self.chat, "main")]
            else:
                raise ValueError(
                    f"No download client route for chat_id={normalized_chat_id}. "
                    "Ensure this channel is accessible via the configured accounts."
                )
        cursor = int(self._download_route_cursor_by_chat_id.get(normalized_chat_id, 0))
        pick = (cursor + int(seed)) % len(routes)
        self._download_route_cursor_by_chat_id[normalized_chat_id] = (pick + 1) % len(
            routes
        )
        return routes[pick]

    def _effective_stride_streams(self, part_concurrency: int) -> int:
        active_part_workers = max(1, int(part_concurrency))
        budgeted_streams = max(
            1, int(self._download_total_stream_budget) // active_part_workers
        )
        return max(1, min(int(self._stride_streams), budgeted_streams))

    def _should_use_strided_download(self, file_size: int, stride_streams: int) -> bool:
        if file_size <= 0 or stride_streams <= 1:
            return False
        stride_min_size = self._STRIDE_MIN_REQUEST_MULTIPLIER * self._tg_request_size
        return file_size >= stride_min_size
