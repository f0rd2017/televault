from __future__ import annotations

import asyncio
import logging
import math
import os
from pathlib import Path
import threading
import time

import psutil

from televault.core.jobs import CancelToken
from televault.core.rate_limiter import AdaptiveRateLimiter, BandwidthLimiter
from televault.tg import compression, partition
from televault.core.transfer_progress import TransferProgressAggregator
from televault.core.types import (
    AppConfig,
    PartMeta,
    PartRecord,
    TgTransferLimits,
)
from televault.core.utils import (
    file_key_from_sha256,
    load_aesgcm_key_from_env,
    now_ts,
    normalize_folder_path,
    random_file_key,
    sanitize_filename,
    sha256_file,
)
from televault.db.repo import DbRepo
from televault.tg.parser import build_caption
from televault.tg.upload.analytics import _UploadAnalyticsMixin
from televault.tg.upload.batch import _SmallBatchMixin
from televault.tg.upload.multipart import _MultipartUploadMixin
from televault.tg.upload.parallel import _ParallelUploadMixin
from televault.tg.upload.records import _UploadRecordBuffer
from televault.tg.upload.resume import (
    clear_resume_file,
    existing_completed_parts,
    load_resume_file_key,
    source_signature,
    write_resume_file,
)
from televault.tg.proxy_escalation import ProxyEscalationMixin
from televault.tg.upload.send import _UploadSendMixin
from televault.tg.upload.single import _SinglePartUploadMixin

logger = logging.getLogger(__name__)


class TgUploader(
    _UploadSendMixin,
    _ParallelUploadMixin,
    _MultipartUploadMixin,
    _SinglePartUploadMixin,
    _SmallBatchMixin,
    _UploadAnalyticsMixin,
    ProxyEscalationMixin,
):
    _TG_REQUEST_SIZE = 524288
    _PREHASH_CHUNK_SIZE = 16 * 1024 * 1024
    _DB_UPSERT_BATCH = 16
    _DB_REBUILD_THROTTLE_SEC = 0.8
    _UPLOAD_QUEUE_FACTOR = 3
    _AUTO_BOOST_MIN_FILE_SIZE = 32 * 1024 * 1024
    _AUTO_BOOST_CONCURRENCY_REGULAR = 8
    _AUTO_BOOST_CONCURRENCY_PREMIUM = 12
    _MAX_UPLOAD_CONCURRENCY = 16
    _MIN_ADAPTIVE_CHUNK_SIZE = 8 * 1024 * 1024
    _TARGET_PARTS_PER_WORKER = 4
    _INNER_PARALLEL_WORKERS_PER_CLIENT_REGULAR = 4
    _INNER_PARALLEL_WORKERS_PER_CLIENT_PREMIUM = 8
    _REGULAR_CONCURRENCY_CAP = 12
    _PREMIUM_CONCURRENCY_CAP = 16
    _DIRECT_UPLOAD_CONFIG_THRESHOLD_MB = 32
    _DIRECT_PARALLEL_BIGFILE_MIN_BYTES = 8 * 1024 * 1024
    _PARALLEL_CHUNK_LOGICAL_CONCURRENCY_REGULAR = 3
    _PARALLEL_CHUNK_LOGICAL_CONCURRENCY_PREMIUM = 6
    _FLOOD_WAIT_BUFFER_SECONDS = 0.2
    _TELEMETRY_LOG_INTERVAL_SEC = 1.0
    _INLINE_PAYLOAD_UPLOAD_MAX_BYTES = 2 * 1024 * 1024
    _IN_MEMORY_PART_MAX_BYTES = 256 * 1024 * 1024
    _DISK_PART_COPY_BUFFER = 4 * 1024 * 1024
    _DISK_PART_UPLOAD_WORKERS_REGULAR = 2
    _DISK_PART_UPLOAD_WORKERS_PREMIUM = 3
    _DISK_PART_UPLOAD_QUEUE_FACTOR = 2
    _MULTI_CLIENT_FORCE_MULTIPART_MIN_BYTES = 1 * 1024 * 1024
    _AUTO_POOL_MULTIPART_MIN_BYTES = 1 * 1024 * 1024
    _EMPTY_FILE_STUB_PAYLOAD = b"\x00"
    _FORCE_POOL_MULTIPART_ENV = "TELEVAULT_FORCE_POOL_MULTIPART"

    def __init__(
        self,
        config: AppConfig,
        repo: DbRepo,
        client,
        chat,
        chat_id: str,
        transfer_limits: TgTransferLimits | None = None,
        extra_clients=None,
        upload_endpoints=None,
    ) -> None:
        self.config = config
        self.repo = repo
        self.client = client
        self.chat = chat
        self.chat_id = chat_id
        self.transfer_limits = transfer_limits or TgTransferLimits()
        self._client_pool = [client] + list(extra_clients or [])
        self._upload_endpoints = list(upload_endpoints or [])
        self._client_chat_cache: dict[int, object] = {id(client): chat}
        self._client_chat_id_cache: dict[int, str] = {id(client): str(chat_id)}
        self._client_label_cache: dict[int, str] = {id(client): "main"}
        if self._upload_endpoints:
            self._client_pool = [endpoint.client for endpoint in self._upload_endpoints]
            self._client_chat_cache = {
                id(endpoint.client): endpoint.chat
                for endpoint in self._upload_endpoints
            }
            self._client_chat_id_cache = {
                id(endpoint.client): str(endpoint.chat_id)
                for endpoint in self._upload_endpoints
            }
            self._client_label_cache = {
                id(endpoint.client): str(getattr(endpoint, "label", "client"))
                for endpoint in self._upload_endpoints
            }
        self._client_chat_lock = asyncio.Lock()
        self._pool_rr_lock = threading.Lock()
        self._pool_rr_cursor = 0
        # Dynamic load-aware account picking: track in-flight count + measured
        # throughput (EWMA MB/s) per account so whole-file uploads go to the
        # free/fastest account instead of blind round-robin. Keyed by id(client).
        self._lb_lock = threading.Lock()
        self._account_inflight: dict[int, int] = {}
        self._account_ewma_mbps: dict[int, float] = {}
        send_rate = float(max(0.1, getattr(self.config, "send_media_rate_limit", 6.0)))
        send_window = float(
            max(0.2, getattr(self.config, "perf_telemetry_window_sec", 1.0))
        )
        self._send_media_limiter = AdaptiveRateLimiter(
            initial_rate=send_rate,
            min_rate=max(0.1, send_rate * 0.2),
            max_rate=max(send_rate, send_rate * 4.0),
            window_sec=send_window,
        )
        # Upload bandwidth limit (MB/s), shared across the instance — parallel
        # parts split the budget. 0 = no limit (acquire is a no-op).
        self._upload_bandwidth = BandwidthLimiter(
            float(getattr(self.config, "upload_throttle_mbps", 0.0))
        )
        force_pool_multipart_raw = (
            str(os.getenv(self._FORCE_POOL_MULTIPART_ENV, "")).strip().lower()
        )
        self._force_pool_multipart = force_pool_multipart_raw in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _next_pool_offset(self) -> int:
        with self._pool_rr_lock:
            pool_size = max(1, len(self._client_pool))
            offset = int(self._pool_rr_cursor % pool_size)
            self._pool_rr_cursor = (self._pool_rr_cursor + 1) % pool_size
            return offset

    def _pool_client(self, part_index: int, base_offset: int = 0):
        """Select a client from the pool round-robin by part index."""
        idx = (int(part_index) + int(base_offset)) % len(self._client_pool)
        return self._client_pool[idx]

    def _reserve_pool_account(self) -> int:
        """Dynamically pick the best account for a whole-file upload: fewest
        in-flight uploads first, then fastest measured throughput. Unknown speed
        sorts first so every account gets sampled early. Reserves the slot."""
        pool = self._client_pool
        if len(pool) <= 1:
            return 0
        with self._lb_lock:
            best_idx = 0
            best_key: tuple[int, float, int] | None = None
            for idx, client in enumerate(pool):
                cid = id(client)
                inflight = int(self._account_inflight.get(cid, 0))
                ewma = float(self._account_ewma_mbps.get(cid, 0.0))
                # Unknown (0) → treat as fastest so it gets sampled.
                speed = float("inf") if ewma <= 0.0 else ewma
                key = (inflight, -speed, idx)
                if best_key is None or key < best_key:
                    best_key, best_idx = key, idx
            cid = id(pool[best_idx])
            self._account_inflight[cid] = int(self._account_inflight.get(cid, 0)) + 1
            return best_idx

    def _release_pool_account(
        self, offset: int, *, bytes_uploaded: int, elapsed: float
    ) -> None:
        """Release a reserved account and fold the observed speed into its EWMA."""
        if not (0 <= int(offset) < len(self._client_pool)):
            return
        cid = id(self._client_pool[int(offset)])
        with self._lb_lock:
            self._account_inflight[cid] = max(
                0, int(self._account_inflight.get(cid, 0)) - 1
            )
            if elapsed > 0 and bytes_uploaded > 0:
                mbps = float(bytes_uploaded) / float(elapsed) / (1024.0 * 1024.0)
                prev = float(self._account_ewma_mbps.get(cid, 0.0))
                self._account_ewma_mbps[cid] = (
                    mbps if prev <= 0.0 else 0.5 * mbps + 0.5 * prev
                )

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
                        "Unable to resolve target chat for upload client: "
                        "primary account has no chat_target or chat_id configured."
                    )
                resolved = await tg_client.get_entity(chat_identifier)
            except Exception as exc:
                raise RuntimeError(
                    "Unable to resolve target chat for upload client. "
                    "Ensure each bot has access to the channel configured on the primary account."
                ) from exc
            self._client_chat_cache[cache_key] = resolved
            resolved_chat_id = str(getattr(resolved, "id", self.chat_id))
            self._client_chat_id_cache[cache_key] = resolved_chat_id
            return resolved

    def _chat_id_for_client(self, tg_client) -> str:
        return str(self._client_chat_id_cache.get(id(tg_client), self.chat_id))

    def _client_label(self, tg_client) -> str:
        return str(self._client_label_cache.get(id(tg_client), "client"))

    async def chunked_upload(
        self,
        file_path: str,
        folder_path: str,
        cancel_token: CancelToken | None = None,
        progress_cb=None,
    ) -> dict[str, object]:
        """Public entry: dynamically route a whole-file upload to the best
        account (load-aware), then delegate to the upload pipeline. Files large
        enough to stripe across the whole pool use plain round-robin instead."""
        try:
            total_size = int(Path(file_path).stat().st_size)
        except OSError:
            total_size = 0
        shard_min_bytes = int(self.config.multi_client_shard_min_mb) * 1024 * 1024
        single_account = (
            len(self._client_pool) > 1 and 0 < total_size <= shard_min_bytes
        )
        if single_account:
            client_offset = self._reserve_pool_account()
        else:
            client_offset = self._next_pool_offset()
        started = time.monotonic()
        try:
            return await self._chunked_upload_impl(
                file_path,
                folder_path,
                cancel_token=cancel_token,
                progress_cb=progress_cb,
                client_offset=client_offset,
            )
        finally:
            if single_account:
                self._release_pool_account(
                    client_offset,
                    bytes_uploaded=total_size,
                    elapsed=time.monotonic() - started,
                )

    async def _chunked_upload_impl(
        self,
        file_path: str,
        folder_path: str,
        cancel_token: CancelToken | None = None,
        progress_cb=None,
        *,
        client_offset: int,
    ) -> dict[str, object]:
        operation_started = time.monotonic()
        token = cancel_token or CancelToken()
        normalized_folder = normalize_folder_path(folder_path)
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")
        source_stat = path.stat()
        total_size = int(source_stat.st_size)
        source_mtime_ns = int(source_stat.st_mtime_ns)

        pool_labels = [self._client_label(c) for c in self._client_pool]
        logger.info(
            "Upload client pool dispatch: pool_size=%d labels=%s start_offset=%d file=%s size=%dMB",
            len(self._client_pool),
            pool_labels,
            int(client_offset),
            path.name,
            total_size // (1024 * 1024),
        )

        original_size = total_size
        original_name = sanitize_filename(path.name)
        file_name = original_name
        safety_limit_bytes = self._safe_upload_limit_bytes()
        compression_mode = str(self.config.upload_compression_mode).strip().lower()
        compression_used = False
        compression_seconds = 0.0
        compression_ratio: float | None = None
        temp_upload_path: Path | None = None

        crypto_key: bytes | None = None
        if self.config.crypto.enabled:
            if not self.config.crypto.key_env:
                raise ValueError(
                    "crypto.key_env is required when encryption is enabled"
                )
            crypto_key = load_aesgcm_key_from_env(self.config.crypto.key_env)

        if compression.should_attempt_fast_compression(
            source_path=path,
            source_size=original_size,
            safe_limit_bytes=safety_limit_bytes,
            mode=compression_mode,
        ):
            compress_started = time.monotonic()
            temp_upload_path = await asyncio.to_thread(
                compression.compress_file_to_temp_zip,
                path,
                original_name,
                token,
            )
            compression_seconds = max(0.0, time.monotonic() - compress_started)
            compressed_size = int(temp_upload_path.stat().st_size)
            compression_ratio = float(compressed_size) / float(max(1, original_size))
            if compression.should_use_compressed_payload(
                source_size=original_size,
                compressed_size=compressed_size,
                safe_limit_bytes=safety_limit_bytes,
                mode=compression_mode,
            ):
                logger.info(
                    "Upload compression enabled: %s -> %s bytes (ratio=%.3f, mode=%s)",
                    original_size,
                    compressed_size,
                    compression_ratio,
                    compression_mode,
                )
                path = temp_upload_path
                total_size = compressed_size
                file_name = sanitize_filename(f"{original_name}.zip")
                compression_used = True
            else:
                logger.info(
                    "Upload compression skipped after trial: %s -> %s bytes (mode=%s)",
                    original_size,
                    compressed_size,
                    compression_mode,
                )
                temp_upload_path.unlink(missing_ok=True)
                temp_upload_path = None

        prehash_started = time.monotonic()
        file_digest = await asyncio.to_thread(
            sha256_file, path, self._PREHASH_CHUNK_SIZE
        )
        prehash_elapsed = max(0.0, time.monotonic() - prehash_started)
        resume_signature = source_signature(
            Path(file_path), size=original_size, mtime_ns=source_mtime_ns
        )
        if self.config.use_sha_as_key:
            file_key = file_key_from_sha256(file_digest)
        else:
            # Reuse a prior random key for the same source so an interrupted
            # upload can resume instead of restarting under a fresh key.
            file_key = load_resume_file_key(
                self.config.cache_dir,
                signature=resume_signature,
                payload_sha256=file_digest,
            ) or await self._allocate_random_file_key(normalized_folder)

        pool_size = max(1, len(self._client_pool))
        logical_part_limit_bytes = partition.logical_part_limit_bytes(
            safety_limit_bytes
        )
        base_parts = partition.base_logical_parts(
            total_size=total_size,
            pool_size=pool_size,
            shard_min_bytes=int(self.config.multi_client_shard_min_mb) * 1024 * 1024,
        )
        planned_parts = partition.plan_logical_parts(
            total_size=total_size,
            base_parts=base_parts,
            part_limit_bytes=logical_part_limit_bytes,
        )
        planned_parts = partition.rebalance_multi_client_parts(
            total_size=total_size,
            planned_parts=planned_parts,
            pool_size=pool_size,
            part_limit_bytes=logical_part_limit_bytes,
        )
        single_part_candidate = (
            crypto_key is None
            and planned_parts == 1
            and total_size <= safety_limit_bytes
        )

        if planned_parts > 1:
            logger.info(
                "Upload sharding plan: file=%s size=%d planned_parts=%d clients=%d part_limit=%d",
                path.name,
                total_size,
                planned_parts,
                pool_size,
                logical_part_limit_bytes,
            )

        if single_part_candidate:
            try:
                return await self._single_part_upload(
                    path=path,
                    total_size=total_size,
                    file_digest=file_digest,
                    file_key=file_key,
                    file_name=file_name,
                    normalized_folder=normalized_folder,
                    operation_started=operation_started,
                    prehash_elapsed=prehash_elapsed,
                    progress_cb=progress_cb,
                    cancel_token=token,
                    original_size=original_size,
                    compression_used=compression_used,
                    compression_seconds=compression_seconds,
                    compression_ratio=compression_ratio,
                    safe_limit_bytes=safety_limit_bytes,
                    client_offset=client_offset,
                )
            finally:
                if temp_upload_path is not None:
                    temp_upload_path.unlink(missing_ok=True)

        balanced_target_chunk = None
        tg_chunk_limit = max(self._MIN_ADAPTIVE_CHUNK_SIZE, int(safety_limit_bytes))
        parts_total = int(planned_parts)
        raw_part_sizes = partition.build_even_part_sizes(total_size, parts_total)
        chunk_size = max(1, max(int(size) for size in raw_part_sizes.values()))

        encryption_overhead = 32 if crypto_key is not None else 0
        part_raw_limit = max(1, tg_chunk_limit - encryption_overhead - 64)
        if chunk_size > part_raw_limit:
            parts_total = partition.plan_logical_parts(
                total_size=total_size,
                base_parts=base_parts,
                part_limit_bytes=part_raw_limit,
            )
            parts_total = partition.rebalance_multi_client_parts(
                total_size=total_size,
                planned_parts=parts_total,
                pool_size=pool_size,
                part_limit_bytes=part_raw_limit,
            )
            raw_part_sizes = partition.build_even_part_sizes(total_size, parts_total)
            chunk_size = max(1, max(int(size) for size in raw_part_sizes.values()))

        if crypto_key is None and chunk_size > self._IN_MEMORY_PART_MAX_BYTES:
            try:
                return await self._multipart_upload_from_disk(
                    source_path=path,
                    total_size=total_size,
                    original_size=original_size,
                    file_digest=file_digest,
                    file_key=file_key,
                    file_name=file_name,
                    normalized_folder=normalized_folder,
                    operation_started=operation_started,
                    prehash_elapsed=prehash_elapsed,
                    progress_cb=progress_cb,
                    cancel_token=token,
                    compression_mode=compression_mode,
                    compression_used=compression_used,
                    compression_seconds=compression_seconds,
                    compression_ratio=compression_ratio,
                    safe_limit_bytes=safety_limit_bytes,
                    part_size_bytes=int(chunk_size),
                    balanced_target_chunk=balanced_target_chunk,
                    client_offset=client_offset,
                )
            finally:
                if temp_upload_path is not None:
                    temp_upload_path.unlink(missing_ok=True)

        payload_part_sizes = {
            idx: raw_part_sizes[idx] + encryption_overhead for idx in range(parts_total)
        }
        total_payload_size = sum(payload_part_sizes.values())

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
        if not self.config.use_sha_as_key:
            write_resume_file(
                self.config.cache_dir,
                signature=resume_signature,
                file_key=file_key,
                parts_total=parts_total,
                payload_sha256=file_digest,
                orig_name=original_name,
            )
        if resume_completed_parts:
            logger.info(
                "Upload resume: file_key=%s skipping %d/%d already-uploaded parts",
                file_key,
                len(resume_completed_parts),
                parts_total,
            )

        self.repo.upsert_folder(normalized_folder)

        # One worker per account — each account uploads 1 chunk at a time.
        # Workers run in parallel: 2 accounts = 2 concurrent uploads.
        pool_size = len(self._client_pool)
        worker_tasks_count = pool_size

        # Concurrency in this path is fixed at one worker per account (no slot
        # gating), so there is no adaptive controller here. Inner per-part
        # parallelism (_upload_bytes_big_file_parallel) has its own live adaptive
        # controller. The summary below reflects the real, fixed concurrency.

        # Per-account queues: each account gets its own parts via round-robin
        account_queues: list[asyncio.Queue[tuple[int, bytes] | None]] = [
            asyncio.Queue(maxsize=max(4, parts_total)) for _ in range(pool_size)
        ]
        logger.info(
            "Upload profile: file=%s size=%d chunk_size=%d parts=%d accounts=%d mode=1-per-account",
            path.name,
            total_size,
            chunk_size,
            parts_total,
            pool_size,
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

        part_progress_bytes: dict[int, int] = {}

        def make_part_progress_callback(part_idx: int):
            def on_progress(current: int, total: int) -> None:
                part_total = int(payload_part_sizes.get(part_idx, max(0, int(total))))
                progress_now = int(max(0, int(current)))
                if part_total > 0:
                    progress_now = min(progress_now, part_total)
                part_progress_bytes[part_idx] = progress_now
                if progress_agg is None:
                    return
                progress_agg.on_part_progress(part_idx, int(current), int(total))

            return on_progress

        record_buffer = _UploadRecordBuffer(
            repo=self.repo,
            chat_id=self.chat_id,
            folder_path=normalized_folder,
            file_key=file_key,
            batch_size=self._DB_UPSERT_BATCH,
            rebuild_throttle_sec=self._DB_REBUILD_THROTTLE_SEC,
        )
        read_seconds = 0.0
        encrypt_seconds = 0.0
        network_send_seconds = 0.0
        flood_wait_count = 0
        flood_wait_seconds = 0.0
        parallel_chunk_workers_hint = partition.default_inner_upload_workers(chunk_size)
        parallel_chunk_workers_samples = 0
        parallel_chunk_workers_total = 0
        completed_parts_count = 0
        channel_payload_bytes: dict[str, int] = {}
        channel_parts_count: dict[str, int] = {}
        clients_used: set[str] = set()

        # Seed counters/progress from parts already uploaded in a previous run.
        for resumed_index in sorted(resume_completed_parts):
            resumed_payload = int(payload_part_sizes.get(resumed_index, 0))
            part_progress_bytes[resumed_index] = resumed_payload
            completed_parts_count += 1
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

        transfer_started = time.monotonic()
        telemetry_last_ts = transfer_started
        telemetry_last_bytes = 0

        def log_upload_telemetry(*, reason: str, force: bool = False) -> None:
            nonlocal telemetry_last_ts, telemetry_last_bytes
            now = time.monotonic()
            if (
                not force
                and (now - telemetry_last_ts) < self._TELEMETRY_LOG_INTERVAL_SEC
            ):
                return
            total_elapsed = max(0.001, now - transfer_started)
            window_elapsed = max(0.001, now - telemetry_last_ts)
            payload_progress_bytes = int(
                sum(max(0, int(v)) for v in part_progress_bytes.values())
            )
            window_bytes = max(0, int(payload_progress_bytes - telemetry_last_bytes))
            avg_speed = (
                float(max(0, payload_progress_bytes))
                / total_elapsed
                / (1024.0 * 1024.0)
            )
            window_speed = float(window_bytes) / window_elapsed / (1024.0 * 1024.0)
            state = {
                "target_concurrency": worker_tasks_count,
                "active_slots": 0,
                "max_concurrency": worker_tasks_count,
            }

            # System metrics every 5 seconds
            sys_info = ""
            if force or (now - telemetry_last_ts) >= 5.0:
                try:
                    _proc_mem = psutil.Process().memory_info().rss / (1024 * 1024)
                    # Non-blocking: percentage since the previous call. Avoids a 50ms
                    # event-loop stall (interval=0.05 sleeps, freezing all uploads).
                    _cpu = psutil.cpu_percent(interval=None)
                    sys_info = f" pid_mem={_proc_mem:.0f}MB cpu={_cpu:.0f}%"
                except Exception:
                    pass

            logger.info(
                (
                    "Upload telemetry: file=%s reason=%s parts=%d/%d payload=%d/%d "
                    "window=%.2f MB/s avg=%.2f MB/s queue=%d slots=%d/%d target=%d flood=%d(%.1fs) hint=%d%s"
                ),
                path.name,
                reason,
                completed_parts_count,
                parts_total,
                payload_progress_bytes,
                total_payload_size,
                window_speed,
                avg_speed,
                sum(q.qsize() for q in account_queues),
                int(state.get("active_slots", 0)),
                int(state.get("max_concurrency", 1)),
                int(state.get("target_concurrency", 1)),
                flood_wait_count,
                flood_wait_seconds,
                parallel_chunk_workers_hint,
                sys_info,
            )
            telemetry_last_ts = now
            telemetry_last_bytes = int(payload_progress_bytes)

        # Per-account worker: each uploads parts from its own queue, 1 at a time.
        async def account_worker(account_index: int) -> None:
            nonlocal encrypt_seconds
            nonlocal network_send_seconds
            nonlocal flood_wait_count
            nonlocal flood_wait_seconds
            nonlocal parallel_chunk_workers_samples
            nonlocal parallel_chunk_workers_total
            nonlocal completed_parts_count

            upload_client = self._client_pool[account_index]
            upload_chat_id = self._chat_id_for_client(upload_client)
            upload_client_label = self._client_label(upload_client)
            queue = account_queues[account_index]

            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    return

                part_index, raw_chunk = item
                logger.debug(
                    "Upload part dispatch: part=%d/%d account=%s client=%s chat_id=%s",
                    int(part_index) + 1,
                    int(parts_total),
                    upload_client_label,
                    upload_client_label,
                    upload_chat_id,
                )
                try:
                    token.raise_if_cancelled()
                    payload = raw_chunk
                    if crypto_key is not None:
                        from televault.core.utils import encrypt_bytes

                        encrypt_started = time.monotonic()
                        payload = encrypt_bytes(raw_chunk, crypto_key)
                        encrypt_seconds += max(0.0, time.monotonic() - encrypt_started)
                    if len(payload) > safety_limit_bytes:
                        raise ValueError(
                            "Chunk exceeds Telegram per-file limit "
                            f"({safety_limit_bytes} bytes with safety reserve)"
                        )

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
                            "part_size": len(payload),
                            "enc": crypto_key is not None,
                        },
                    )

                    send_started = time.monotonic()
                    send_stats = {"flood_wait_count": 0, "flood_wait_seconds": 0.0}
                    try:
                        if len(payload) >= self._DIRECT_PARALLEL_BIGFILE_MIN_BYTES:
                            part_progress_lock = asyncio.Lock()

                            async def on_part_progress(
                                current: int, total: int
                            ) -> None:
                                cb = make_part_progress_callback(part_index)
                                cb(int(current), int(total))

                            (
                                uploaded_file,
                                _part_send_seconds,
                                part_profile,
                            ) = await self._upload_bytes_big_file_parallel(
                                payload=payload,
                                file_name=f"{file_name}.part{part_index:04d}",
                                cancel_token=token,
                                progress_cb=on_part_progress,
                                progress_lock=part_progress_lock,
                                workers_hint=parallel_chunk_workers_hint,
                                client=upload_client,
                            )
                            message = await self._send_uploaded_file_with_retry(
                                uploaded_file,
                                caption=caption,
                                file_name=f"{file_name}.part{part_index:04d}",
                                stats=send_stats,
                                cancel_token=token,
                                client=upload_client,
                            )
                            send_stats["flood_wait_count"] = int(
                                send_stats.get("flood_wait_count", 0)
                            ) + int(part_profile.get("flood_wait_count", 0))
                            send_stats["flood_wait_seconds"] = float(
                                send_stats.get("flood_wait_seconds", 0.0)
                            ) + float(part_profile.get("flood_wait_seconds", 0.0))
                            parallel_chunk_workers_samples += 1
                            parallel_chunk_workers_total += int(
                                part_profile.get(
                                    "effective_workers", parallel_chunk_workers_hint
                                )
                            )
                            send_elapsed = max(0.0, time.monotonic() - send_started)
                        else:
                            message = await self._send_with_retry(
                                payload=payload,
                                caption=caption,
                                file_name=f"{file_name}.part{part_index:04d}",
                                progress_callback=make_part_progress_callback(
                                    part_index
                                ),
                                stats=send_stats,
                                cancel_token=token,
                                client=upload_client,
                            )
                            send_elapsed = max(0.0, time.monotonic() - send_started)
                    finally:
                        pass  # No slot release needed — 1 per account

                    part_speed = len(payload) / max(0.001, send_elapsed) / (1024 * 1024)
                    logger.info(
                        "Upload part done: part=%d/%d account=%s chat_id=%s size=%.1fMB speed=%.1fMB/s time=%.2fs",
                        int(part_index) + 1,
                        int(parts_total),
                        upload_client_label,
                        upload_chat_id,
                        len(payload) / (1024 * 1024),
                        part_speed,
                        send_elapsed,
                    )
                    network_send_seconds += send_elapsed
                    flood_wait_count += int(send_stats["flood_wait_count"])
                    flood_wait_seconds += float(send_stats["flood_wait_seconds"])
                    part_progress_bytes[part_index] = int(
                        payload_part_sizes.get(part_index, len(payload))
                    )
                    completed_parts_count += 1
                    clients_used.add(upload_client_label)
                    channel_payload_bytes[upload_chat_id] = int(
                        channel_payload_bytes.get(upload_chat_id, 0) + len(payload)
                    )
                    channel_parts_count[upload_chat_id] = int(
                        channel_parts_count.get(upload_chat_id, 0) + 1
                    )
                    log_upload_telemetry(reason="part")
                    if progress_agg is not None:
                        full_size = payload_part_sizes[part_index]
                        progress_agg.on_part_progress(part_index, full_size, full_size)

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
                            file_size=len(payload),
                            caption_raw=caption,
                            date_ts=msg_date,
                        )
                    )
                finally:
                    queue.task_done()

        # Create 1 worker per account
        worker_tasks = [
            asyncio.create_task(account_worker(i)) for i in range(pool_size)
        ]
        upload_ok = False
        log_upload_telemetry(reason="start", force=True)

        def raise_consumer_failures() -> None:
            for task in worker_tasks:
                if not task.done() or task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    raise exc

        async def put_with_backpressure(
            account_index: int, part_index: int, raw_chunk: bytes
        ) -> None:
            # Avoid producer deadlock when all consumers fail and queue gets full.
            while True:
                raise_consumer_failures()
                token.raise_if_cancelled()
                try:
                    await asyncio.wait_for(
                        account_queues[account_index].put((part_index, raw_chunk)),
                        timeout=0.25,
                    )
                    return
                except asyncio.TimeoutError:
                    log_upload_telemetry(reason="backpressure")
                    continue

        try:
            with path.open("rb") as src:
                for part_index in range(parts_total):
                    raise_consumer_failures()
                    token.raise_if_cancelled()
                    if part_index in resume_completed_parts:
                        # Already uploaded in a previous run — skip its bytes.
                        src.seek(min((part_index + 1) * chunk_size, total_size))
                        continue
                    read_started = time.monotonic()
                    raw_chunk = await asyncio.to_thread(src.read, chunk_size)
                    read_seconds += max(0.0, time.monotonic() - read_started)
                    if not raw_chunk:
                        # Allow uploading a real empty file as a single logical part.
                        if not (total_size == 0 and part_index == 0):
                            break
                    # Round-robin: distribute parts across accounts
                    account_index = part_index % pool_size
                    await put_with_backpressure(account_index, part_index, raw_chunk)

            # Send None sentinel to each account worker
            for i in range(pool_size):
                await account_queues[i].put(None)

            await asyncio.gather(*worker_tasks)
            await record_buffer.flush(force=True)
            upload_ok = True
            if not self.config.use_sha_as_key:
                clear_resume_file(self.config.cache_dir, signature=resume_signature)
        except asyncio.CancelledError:
            for task in worker_tasks:
                task.cancel()
            await asyncio.gather(*worker_tasks, return_exceptions=True)
            try:
                await record_buffer.flush(force=True)
            except Exception:
                logger.exception(
                    "Failed to flush pending upload records after cancellation"
                )
            raise
        except Exception:
            for task in worker_tasks:
                task.cancel()
            await asyncio.gather(*worker_tasks, return_exceptions=True)
            try:
                await record_buffer.flush(force=True)
            except Exception:
                logger.exception(
                    "Failed to flush pending upload records after interruption"
                )
            raise
        finally:
            if progress_agg is not None:
                await progress_agg.stop("Upload complete" if upload_ok else None)
        log_upload_telemetry(reason="final", force=True)

        transfer_elapsed = max(0.001, time.monotonic() - transfer_started)
        speed_mbps = float(total_payload_size) / transfer_elapsed / (1024.0 * 1024.0)

        # Per-account breakdown
        acct_details = []
        for acct_chat_id, acct_bytes in channel_payload_bytes.items():
            acct_parts = channel_parts_count.get(acct_chat_id, 0)
            # Find label for this chat_id
            acct_label = acct_chat_id
            for c in self._client_pool:
                if str(self._chat_id_for_client(c)) == str(acct_chat_id):
                    acct_label = self._client_label(c)
                    break
            acct_details.append(
                f"{acct_label}={acct_parts}parts({acct_bytes / (1024 * 1024):.0f}MB)"
            )
        logger.info(
            "Upload finished: file_key=%s parts=%d size=%d bytes speed=%.2f MB/s accounts=[%s]",
            file_key,
            parts_total,
            total_payload_size,
            speed_mbps,
            ", ".join(acct_details),
        )

        rebuild_started = time.monotonic()
        self.repo.rebuild_object_aggregate(self.chat_id, normalized_folder, file_key)
        record_buffer.db_rebuild_seconds += max(0.0, time.monotonic() - rebuild_started)
        total_elapsed = max(0.001, time.monotonic() - operation_started)
        total_payload_speed_mbps = (
            float(total_payload_size) / total_elapsed / (1024.0 * 1024.0)
        )
        total_source_speed_mbps = float(total_size) / total_elapsed / (1024.0 * 1024.0)
        adaptive_summary = {
            "initial_concurrency": int(worker_tasks_count),
            "min_concurrency": int(worker_tasks_count),
            "final_concurrency": int(worker_tasks_count),
            "max_concurrency": int(worker_tasks_count),
            "samples": 0,
            "ema_speed_mbps": float(speed_mbps),
            "flood_wait_count": int(flood_wait_count),
            "flood_wait_seconds": float(flood_wait_seconds),
            "adjustments": [],
        }
        upload_profile = {
            "chunk_size": int(chunk_size),
            "concurrency": int(worker_tasks_count),
            "max_adaptive_concurrency": int(worker_tasks_count),
            "effective_concurrency": int(
                adaptive_summary.get("final_concurrency") or worker_tasks_count
            ),
            "inner_workers": int(parallel_chunk_workers_hint),
            "parts_total": int(parts_total),
            "disk_backed_parts": False,
            "balanced_part_sizing": bool(balanced_target_chunk is not None),
            "balanced_target_chunk": (
                int(balanced_target_chunk)
                if balanced_target_chunk is not None
                else None
            ),
            "parallel_chunk_upload": bool(parallel_chunk_workers_hint > 1),
            "parallel_chunk_workers_hint_final": int(parallel_chunk_workers_hint),
            "parallel_chunk_workers_avg": (
                float(parallel_chunk_workers_total)
                / float(parallel_chunk_workers_samples)
                if parallel_chunk_workers_samples > 0
                else None
            ),
            "adaptive": adaptive_summary,
            "channels_used": sorted(
                str(chat_id) for chat_id in channel_parts_count.keys()
            ),
            "parts_by_channel": {
                str(chat_id): int(value)
                for chat_id, value in channel_parts_count.items()
            },
            "clients_used": sorted(str(label) for label in clients_used),
            "cross_channel_parts": bool(len(channel_parts_count) > 1),
        }
        analytics = self._build_upload_analytics(
            phase_seconds={
                "prehash": prehash_elapsed,
                "read": read_seconds,
                "encrypt": encrypt_seconds,
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
            safe_limit_bytes=safety_limit_bytes,
            flood_wait_count=flood_wait_count,
            flood_wait_seconds=flood_wait_seconds,
            adaptive_block={
                "initial_part_concurrency": int(worker_tasks_count),
                "final_part_concurrency": int(
                    adaptive_summary.get("final_concurrency") or worker_tasks_count
                ),
                "flood_wait_count": int(flood_wait_count),
            },
            upload_profile=upload_profile,
            payload_by_channel=channel_payload_bytes,
        )
        logger.info(
            (
                "Upload analytics: file_key=%s prehash=%.3fs read=%.3fs net=%.3fs "
                "db_upsert=%.3fs rebuild=%.3fs total=%.3fs total_payload_speed=%.2f MB/s "
                "total_source_speed=%.2f MB/s"
            ),
            file_key,
            prehash_elapsed,
            read_seconds,
            network_send_seconds,
            record_buffer.db_upsert_seconds,
            record_buffer.db_rebuild_seconds,
            total_elapsed,
            total_payload_speed_mbps,
            total_source_speed_mbps,
        )

        if temp_upload_path is not None:
            temp_upload_path.unlink(missing_ok=True)

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

    def _balanced_target_part_size_bytes(
        self,
        *,
        total_size: int,
        safe_limit_bytes: int,
        crypto_enabled: bool,
    ) -> int | None:
        if crypto_enabled:
            return None
        if not bool(self.config.balanced_part_sizing_enabled):
            return None
        if total_size <= int(safe_limit_bytes):
            return None
        min_file_size = max(1, int(self.config.balanced_part_min_file_mb)) * 1024 * 1024
        if total_size < min_file_size:
            return None

        target_mb = (
            int(self.config.balanced_part_target_premium_mb)
            if self.transfer_limits.is_premium
            else int(self.config.balanced_part_target_regular_mb)
        )
        target_bytes = max(1, target_mb) * 1024 * 1024
        hard_cap = max(
            self._MIN_ADAPTIVE_CHUNK_SIZE,
            int(safe_limit_bytes) - (8 * 1024 * 1024),
        )
        bounded = max(
            self._MIN_ADAPTIVE_CHUNK_SIZE, min(int(target_bytes), int(hard_cap))
        )
        aligned = max(
            self._TG_REQUEST_SIZE,
            (bounded // self._TG_REQUEST_SIZE) * self._TG_REQUEST_SIZE,
        )
        return int(aligned)

    async def _sleep_with_cancel(
        self,
        seconds: float,
        cancel_token: CancelToken | None = None,
    ) -> None:
        delay = max(0.0, float(seconds))
        if delay <= 0.0:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            return
        deadline = time.monotonic() + delay
        while True:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.25, remaining))

    def _safe_upload_limit_bytes(self) -> int:
        hard_limit = max(1, int(self.transfer_limits.max_file_size_bytes or 0))
        reserve = max(0, int(self.config.upload_limit_safety_mb)) * 1024 * 1024
        safe_limit = hard_limit - reserve
        floor = min(hard_limit, max(self._TG_REQUEST_SIZE * 4, hard_limit // 4))
        if safe_limit < floor:
            safe_limit = floor
        return max(1, min(hard_limit, int(safe_limit)))

    def _auto_boost_concurrency_target(self) -> int:
        base = (
            self._AUTO_BOOST_CONCURRENCY_PREMIUM
            if self.transfer_limits.is_premium
            else self._AUTO_BOOST_CONCURRENCY_REGULAR
        )
        cpu_threads = max(1, int(os.cpu_count() or 1))
        cpu_target = max(
            2, min(self._MAX_UPLOAD_CONCURRENCY, int(math.ceil(cpu_threads * 0.75)))
        )
        pool_target = max(1, len(self._client_pool))
        if self.transfer_limits.is_premium:
            pool_target *= 2
        return int(max(base, cpu_target, pool_target))

    async def _allocate_random_file_key(
        self, folder_path: str, max_attempts: int = 64
    ) -> str:
        for _ in range(max_attempts):
            candidate = random_file_key(12)
            existing = self.repo.get_parts_for_object(
                folder_path=folder_path, file_key=candidate
            )
            if not existing:
                return candidate
        raise RuntimeError("Failed to allocate unique random file_key")
