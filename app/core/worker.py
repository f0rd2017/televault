from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from PySide6.QtCore import QThread, Signal

from app.core.jobs import JobContext, JobManager
from app.core.types import AppConfig, JobEvent, JobStatus, JobType
from app.core.accounts import AccountManager, ConnectedAccount
from app.db.repo import DbRepo
from app.tg.client import TgClientManager
from app.tg.delete import TgDeleter
from app.tg.download import TgDownloader
from app.tg.scan import TgScanner
from app.tg.upload import TgUploader

logger = logging.getLogger(__name__)

# Poster frame offset for not-yet-downloaded videos: skip the very first frame
# (often a black fade-in) and grab one ~1s in. The prefix/head we fetch holds
# many seconds, and extract_video_poster_png falls back to frame 0 for shorter
# clips, so this is safe.
_REMOTE_POSTER_SEEK_SEC = 1.0


class TelegramWorker(QThread):
    job_event = Signal(object)
    ready = Signal()
    fatal_error = Signal(str)
    reconnect_attempt = Signal(
        int
    )  # emitted with attempt number (1-based) before each retry
    account_pool_status = Signal(
        object
    )  # dict: active/total endpoints + degraded accounts
    # Image previews (increment 1b): (folder_path, file_key, temp_image_path)
    thumbnail_ready = Signal(str, str, str)
    thumbnail_failed = Signal(str, str)  # (folder_path, file_key)

    def __init__(self, config: AppConfig, repo: DbRepo, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self.repo = repo
        self._state_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._jobs: JobManager | None = None
        self._scanner: TgScanner | None = None
        self._uploader: TgUploader | None = None
        self._downloader: TgDownloader | None = None
        self._deleter: TgDeleter | None = None
        self._account_manager: AccountManager | None = None
        self._upload_accounts: list[ConnectedAccount] = []
        # (client_by_chat_id, chat_by_chat_id) for blob manifest recovery
        self._recovery_ctx: tuple[dict[str, object], dict[str, object]] | None = None
        self._accepting_jobs = False
        self._stop_requested = False
        self._restart_requested = False
        self._job_persist_progress: dict[int, float] = {}
        self._job_persist_ts: dict[int, float] = {}
        self._running_log_progress: dict[int, float] = {}
        self._running_log_ts: dict[int, float] = {}
        self.finished.connect(self._on_thread_finished)

    async def _initialize_components(
        self,
    ) -> tuple[AccountManager, list[ConnectedAccount], TgClientManager, object | None]:
        """Initialize core components and return them for use in _main."""
        # Try main session (optional — used as fallback when no upload accounts)
        tg_manager = TgClientManager(self.config, skip_bots=True)
        tg_session = None

        # Connect user accounts — these are the PRIMARY upload/scanner mechanism
        account_manager = AccountManager(self.config, self.repo)
        connected_accounts = await account_manager.load_and_connect_all()

        # Extract chat_targets from connected accounts for TgClientManager
        account_targets = [ca.account.chat_target for ca in connected_accounts]

        # Start main session with account targets so it can resolve channels
        if not (self._stop_event and self._stop_event.is_set()):
            try:
                tg_session = await tg_manager.start(account_targets=account_targets)
            except Exception as exc:
                logger.warning("Main session unavailable: %s", exc)

        return account_manager, connected_accounts, tg_manager, tg_session

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        with self._state_lock:
            self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception as e:
            logger.exception("TelegramWorker fatal error: %s", str(e))
        finally:
            self._set_accepting_jobs(False)
            try:
                # Drain pending asyncio tasks to avoid "Task was destroyed but it is pending"
                # and "Event loop is closed" races from background Telethon loops.
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.run_until_complete(loop.shutdown_asyncgens())
                if hasattr(loop, "shutdown_default_executor"):
                    loop.run_until_complete(loop.shutdown_default_executor())
                loop.close()
            finally:
                with self._state_lock:
                    if self._loop is loop:
                        self._loop = None

    async def _main(self) -> None:
        stop_event = asyncio.Event()
        with self._state_lock:
            self._stop_event = stop_event
            if self._stop_requested:
                stop_event.set()

        try:
            (
                account_manager,
                connected_accounts,
                tg_manager,
                tg_session,
            ) = await self._initialize_components()
        except Exception as e:
            logger.exception("Failed to initialize components: %s", e)
            self.fatal_error.emit(f"Initialization failed: {e}")
            return

        if stop_event.is_set():
            await tg_manager.stop()
            return

        upload_accounts = account_manager.get_active_endpoints()

        if not upload_accounts and tg_session is None:
            self.fatal_error.emit(
                "No user accounts configured and main session unavailable. "
                "Add accounts in the app: menu → Accounts → Add account."
            )
            return

        if upload_accounts:
            logger.info(
                "📡 Using %d user account(s) for upload: %s",
                len(upload_accounts),
                ", ".join(
                    f"{ca.account.label}({ca.account.chat_target})"
                    for ca in upload_accounts
                ),
            )
        else:
            logger.warning("⚠️ No upload accounts configured — using main session only")

        # Surface upload-pool health to the UI: if some configured accounts can't
        # see their channel, striping silently degrades (e.g. 1 stream instead of 3).
        try:
            connected = account_manager.get_connected()
            degraded = [
                {
                    "label": str(ca.account.label),
                    "chat_target": str(ca.account.chat_target),
                    "reason": "channel not visible (account has not joined the channel?)",
                }
                for ca in connected
                if ca.chat_obj is None
            ]
            self.account_pool_status.emit(
                {
                    "active": int(len(upload_accounts)),
                    "total": int(len(connected)),
                    "degraded": degraded,
                }
            )
        except Exception as pool_exc:  # never let diagnostics break startup
            logger.debug("account_pool_status emit failed: %s", str(pool_exc))

        with self._state_lock:
            self._account_manager = account_manager
            self._upload_accounts = upload_accounts

        # Transfer limits from first available source
        if upload_accounts:
            from app.core.types import TgTransferLimits

            # Use account is_premium, but default parts for safety if not set
            is_premium = upload_accounts[0].account.is_premium
            transfer_limits = TgTransferLimits(
                is_premium=is_premium,
                request_size_bytes=524288,
                max_fileparts=8000 if is_premium else 4000,
                max_file_size_bytes=(8000 if is_premium else 4000) * 524288,
            )
        else:
            transfer_limits = tg_session.transfer_limits

        # Scanner — channels come exclusively from upload accounts (DB accounts).
        all_scan_chats: dict[int, object] = {}
        all_scan_chat_ids: dict[int, str] = {}
        client_by_chat_id: dict[str, object] = {}

        # Upload accounts provide all scan channels
        if upload_accounts:
            for idx, ca in enumerate(upload_accounts):
                if ca.chat_obj is not None and ca.chat_id:
                    all_scan_chats[idx] = ca.chat_obj
                    all_scan_chat_ids[idx] = ca.chat_id
                    client_by_chat_id[ca.chat_id] = ca.client

        if not all_scan_chats:
            self.fatal_error.emit(
                "No channels available for scanning. "
                "Add user accounts in the app: menu → Accounts → Add account."
            )
            if tg_session:
                await tg_manager.stop()
            return

        logger.info(
            "Scanner: %d channels via clients=%d",
            len(all_scan_chats),
            len(client_by_chat_id),
        )

        scanner = TgScanner(
            config=self.config,
            repo=self.repo,
            client=None,  # Not used in multi-client mode
            chats=list(all_scan_chats.values()),
            chat_ids=list(all_scan_chat_ids.values()),
            client_by_chat_id=client_by_chat_id,
        )

        try:
            logger.info(
                "🚀 Starting automatic reconcile of the database with Telegram..."
            )
            # Pass None, since scanner expects a CancelToken, not an asyncio.Event
            stats = await scanner.reconcile(cancel_token=None)
            logger.info(
                "✅ Reconcile finished: records removed: %d, parts indexed: %d",
                stats.deleted_marked,
                stats.indexed_parts,
            )
        except Exception as e:
            logger.error("Automatic reconcile failed: %s", e)

        chat_by_chat_id: dict[str, object] = {
            all_scan_chat_ids[idx]: all_scan_chats[idx] for idx in all_scan_chats
        }
        with self._state_lock:
            self._recovery_ctx = (dict(client_by_chat_id), chat_by_chat_id)

        # Blobs scanned from Telegram may have no local manifest (fresh
        # database / another machine) — rebuild member lists from the zips
        # themselves so files inside blobs are never tied to one local DB.
        try:
            recovery_stats = await self._run_blob_manifest_recovery()
            if recovery_stats.get("recovered"):
                logger.info(
                    "✅ Blob manifests recovered: blobs=%d files=%d failed=%d",
                    recovery_stats.get("recovered", 0),
                    recovery_stats.get("members", 0),
                    recovery_stats.get("failed", 0),
                )
        except Exception as e:
            logger.error("Blob manifest recovery failed: %s", e)

        # Initialize core components

        # Upload via user accounts
        if upload_accounts:
            from app.tg.client import TgClientEndpoint

            account_endpoints = [
                TgClientEndpoint(
                    client=ca.client,
                    chat=ca.chat_obj,
                    chat_id=ca.chat_id,
                    channel_index=idx,
                    role="account",
                    label=ca.account.label,
                )
                for idx, ca in enumerate(upload_accounts)
            ]
            uploader = TgUploader(
                config=self.config,
                repo=self.repo,
                client=upload_accounts[0].client,
                chat=upload_accounts[0].chat_obj,
                chat_id=upload_accounts[0].chat_id,
                transfer_limits=transfer_limits,
                extra_clients=[],
                upload_endpoints=account_endpoints,
            )
        else:
            uploader = TgUploader(
                config=self.config,
                repo=self.repo,
                client=tg_session.client,
                chat=tg_session.chat,
                chat_id=tg_session.chat_id,
                transfer_limits=transfer_limits,
                extra_clients=[],
                upload_endpoints=list(tg_session.upload_endpoints),
            )

        # Download via user accounts, or the primary account
        download_endpoints = {}
        if upload_accounts:
            from app.tg.client import TgClientEndpoint

            for ca in upload_accounts:
                download_endpoints.setdefault(ca.chat_id, []).append(
                    TgClientEndpoint(
                        client=ca.client,
                        chat=ca.chat_obj,
                        chat_id=ca.chat_id,
                        channel_index=0,
                        role="account",
                        label=ca.account.label,
                    )
                )
        elif tg_session and tg_session.upload_endpoints:
            download_endpoints = {tg_session.chat_id: [tg_session.upload_endpoints[0]]}

        downloader = TgDownloader(
            config=self.config,
            repo=self.repo,
            client=upload_accounts[0].client if upload_accounts else tg_session.client,
            chat=upload_accounts[0].chat_obj if upload_accounts else tg_session.chat,
            chat_id=upload_accounts[0].chat_id
            if upload_accounts
            else tg_session.chat_id,
            transfer_limits=transfer_limits,
            extra_clients=[],
            download_endpoints=download_endpoints,
        )

        # Auto-escalate the proxy mid-session: if all transfer retries are
        # exhausted by a connection error, the client is switched to the next
        # tier of the primary→backup→direct chain (see app.tg.proxy_escalation).
        if account_manager is not None:
            uploader.proxy_escalator = account_manager.escalate_proxy
            downloader.proxy_escalator = account_manager.escalate_proxy

        # Delete/reconciliation via user accounts, or the primary account
        if upload_accounts:
            from app.tg.client import TgClientEndpoint

            # Filter out accounts without chat_obj (chat_obj is required for delete operations)
            valid_upload_accounts = [
                ca for ca in upload_accounts if ca.chat_obj is not None
            ]
            all_delete_endpoints = [
                TgClientEndpoint(
                    client=ca.client,
                    chat=ca.chat_obj,
                    chat_id=ca.chat_id,
                    channel_index=idx,
                    role="account",
                    label=ca.account.label,
                )
                for idx, ca in enumerate(valid_upload_accounts)
            ]
            all_delete_chats = [ca.chat_obj for ca in valid_upload_accounts]
            all_delete_chat_ids = [ca.chat_id for ca in valid_upload_accounts]
        else:
            all_delete_endpoints = list(tg_session.upload_endpoints)
            all_delete_chats = list(tg_session.resolved_chats_by_index)
            all_delete_chat_ids = list(tg_session.chat_ids_by_index)

        deleter = TgDeleter(
            config=self.config,
            repo=self.repo,
            # Use valid_upload_accounts if available, otherwise fall back to tg_session
            client=valid_upload_accounts[0].client
            if valid_upload_accounts
            else (upload_accounts[0].client if upload_accounts else tg_session.client),
            chat=valid_upload_accounts[0].chat_obj
            if valid_upload_accounts
            else (upload_accounts[0].chat_obj if upload_accounts else tg_session.chat),
            chat_id=valid_upload_accounts[0].chat_id
            if valid_upload_accounts
            else (
                upload_accounts[0].chat_id if upload_accounts else tg_session.chat_id
            ),
            chats=all_delete_chats,
            chat_ids=all_delete_chat_ids,
            delete_endpoints=all_delete_endpoints,
        )

        # Validation: make sure at least one valid route is registered
        if not deleter._routes_by_chat_id:
            raise RuntimeError(
                "No valid delete routes configured. "
                "Ensure all Telegram accounts have proper channel access and chat entities are resolved."
            )

        # Log the route count for debugging
        total_routes = sum(
            len(routes) for routes in deleter._routes_by_chat_id.values()
        )
        logger.info(
            "TgDeleter initialized with %d route(s) for %d chat(s)",
            total_routes,
            len(deleter._routes_by_chat_id),
        )
        # Use per-account worker count — 1 upload per account at a time.
        # Cap at a reasonable number to avoid excess threads.
        account_job_limit = max(
            1,
            min(
                len(valid_upload_accounts)
                if valid_upload_accounts
                else (len(upload_accounts) if upload_accounts else 1),
                8,
            ),
        )
        jobs = JobManager(
            parallelism=account_job_limit,
            lane_caps={
                "upload_small": int(self.config.lane_upload_small_max),
                "upload_large": int(self.config.lane_upload_large_max),
                "download": int(self.config.lane_download_max),
                "default": int(self.config.max_active_jobs),
            },
            lane_weights={
                "upload_small": 3,
                "upload_large": 2,
                "download": 2,
                "default": 1,
            },
        )
        jobs.subscribe(self._on_job_event)

        # Local self-heal: if part rows exist but aggregates are stale (e.g., app closed mid-upload),
        # rebuild objects immediately so UI can show files without waiting for network scan.
        try:
            self.repo.rebuild_objects_aggregates()
        except Exception:
            logger.exception("Failed to rebuild local object aggregates at startup")

        with self._state_lock:
            self._jobs = jobs
            self._scanner = scanner
            self._uploader = uploader
            self._downloader = downloader
            self._deleter = deleter
            self._accepting_jobs = True

        self.ready.emit()
        try:
            await stop_event.wait()
        finally:
            self._set_accepting_jobs(False)
            if jobs is not None:
                await jobs.stop()
            await tg_manager.stop()
            # Disconnect multi-accounts
            if account_manager:
                await account_manager.disconnect_all()

    def _on_job_event(self, event: JobEvent) -> None:
        should_emit = True
        should_log = True
        if event.status == JobStatus.RUNNING:
            now = time.monotonic()
            progress = float(event.progress)
            prev_progress = self._running_log_progress.get(event.job_id, -1.0)
            prev_ts = self._running_log_ts.get(event.job_id, 0.0)
            # Rate-limit: at most 10 updates per second (interval >= 100ms)
            # OR a progress change >= 1%
            should_emit = (
                prev_progress < 0.0
                or abs(progress - prev_progress) >= 1.0
                or (now - prev_ts) >= 0.1
                or bool(event.error)
            )
            should_log = (
                prev_progress < 0.0
                or abs(progress - prev_progress) >= 1.0
                or (now - prev_ts) >= 1.0
                or bool(event.error)
            )
            if should_emit:
                self._running_log_progress[event.job_id] = progress
                self._running_log_ts[event.job_id] = now
        else:
            self._running_log_progress.pop(event.job_id, None)
            self._running_log_ts.pop(event.job_id, None)

        if should_log:
            logger.debug(
                "Job event: id=%s type=%s status=%s progress=%.1f message=%s error=%s",
                event.job_id,
                event.job_type,
                event.status.value,
                float(event.progress),
                (event.message or "").strip(),
                event.error,
            )
        if should_emit:
            self._persist_job_event(event)
            self.job_event.emit(event)

    def _persist_job_event(self, event: JobEvent) -> None:
        payload = event.payload or {}
        db_job_id = payload.get("_db_job_id")
        if db_job_id is None:
            return

        status = self._db_status_for_event(event.status)
        progress = float(event.progress)
        error_text = event.error if event.status == JobStatus.ERROR else None

        if not self._should_persist(event, status, progress):
            return

        try:
            self.repo.update_job(
                db_job_id, status=status, progress=progress, error_text=error_text
            )
            if status == JobStatus.RUNNING.value:
                self._job_persist_progress[event.job_id] = progress
                self._job_persist_ts[event.job_id] = time.monotonic()
            else:
                self._job_persist_progress.pop(event.job_id, None)
                self._job_persist_ts.pop(event.job_id, None)
        except Exception as e:
            logger.exception(
                "Failed to persist job state for job #%s: %s", event.job_id, str(e)
            )

    def _should_persist(self, event: JobEvent, status: str, progress: float) -> bool:
        if status != JobStatus.RUNNING.value:
            return True
        now = time.monotonic()
        prev_progress = self._job_persist_progress.get(event.job_id)
        prev_ts = self._job_persist_ts.get(event.job_id, 0.0)
        if prev_progress is None:
            return True
        # Optimization: persist every 25% instead of 10% to reduce load on SQLite
        if abs(progress - prev_progress) >= 25.0:
            return True
        if now - prev_ts >= 3.0:
            return True
        return False

    @staticmethod
    def _db_status_for_event(status: JobStatus) -> str:
        if status == JobStatus.QUEUED:
            return JobStatus.QUEUED.value
        if status in {JobStatus.STARTED, JobStatus.RUNNING}:
            return JobStatus.RUNNING.value
        if status == JobStatus.DONE:
            return JobStatus.DONE.value
        if status == JobStatus.CANCELLED:
            return JobStatus.CANCELLED.value
        if status == JobStatus.ERROR:
            return JobStatus.ERROR.value
        return status.value

    def submit_job(self, job_type: str, payload: dict[str, Any]) -> bool:
        with self._state_lock:
            loop = self._loop
            accepting_jobs = self._accepting_jobs
            jobs_ready = self._jobs is not None
        if (
            loop is None
            or not loop.is_running()
            or not accepting_jobs
            or not jobs_ready
        ):
            logger.warning(
                "Worker cannot accept job '%s': loop_running=%s accepting_jobs=%s jobs_ready=%s",
                job_type,
                bool(loop and loop.is_running()),
                accepting_jobs,
                jobs_ready,
            )
            return False
        coro = self._enqueue(job_type, payload)
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            logger.warning("Worker loop rejected job '%s' submission", job_type)
            return False
        return True

    def assemble_file_blocking(
        self,
        folder_path: str,
        file_key: str,
        dest_dir: str,
        *,
        timeout: float = 1800.0,
    ) -> str | None:
        """Assemble the file from its chunks on disk and return the path (for
        share links, increment 8). BLOCKS the calling thread (the HTTP handler)
        while the assembly runs on the worker's loop. Returns None on
        error/timeout. Not a job — no toast notifications."""
        with self._state_lock:
            loop = self._loop
            downloader = self._downloader
            accepting = self._accepting_jobs
        if loop is None or not loop.is_running() or downloader is None or not accepting:
            return None

        async def _assemble() -> str:
            result = await downloader.chunked_download(
                folder_path,
                file_key,
                allow_incomplete=False,
                integrity_mode="fast",
                dest_root=dest_dir,
            )
            return str(result.get("output_path") or "")

        try:
            future = asyncio.run_coroutine_threadsafe(_assemble(), loop)
            output_path = future.result(timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Share assembly failed: folder=%s key=%s err=%s",
                folder_path,
                file_key,
                exc,
            )
            return None
        return output_path or None

    def fetch_stream_parts_blocking(
        self,
        folder_path: str,
        file_key: str,
        part_indices: list[int],
        cache_dir: str,
        *,
        timeout: float = 600.0,
        prefix_bytes: dict[int, int] | None = None,
    ) -> dict[int, str]:
        """Download+decrypt ONLY the given parts into ``cache_dir`` and return
        ``{part_index: path}`` (streaming without a full download, increment
        9/10). BLOCKS the calling thread (the HTTP handler) while the download
        runs on the worker's loop. Returns an empty dict on
        error/timeout/not-ready — the serving code then falls back to
        assembling the full file.

        ``prefix_bytes`` — see ``TgDownloader.fetch_parts_decrypted``: for
        unencrypted objects, this lets us download just the start of a huge
        part instead of the whole thing, so the player starts faster."""
        with self._state_lock:
            loop = self._loop
            downloader = self._downloader
            accepting = self._accepting_jobs
        if loop is None or not loop.is_running() or downloader is None or not accepting:
            return {}

        async def _fetch() -> dict[int, str]:
            return await downloader.fetch_parts_decrypted(
                folder_path,
                file_key,
                part_indices,
                cache_dir,
                prefix_bytes=prefix_bytes,
            )

        try:
            future = asyncio.run_coroutine_threadsafe(_fetch(), loop)
            return future.result(timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Stream part fetch failed: folder=%s key=%s parts=%s err=%s: %r",
                folder_path,
                file_key,
                part_indices,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return {}

    def fetch_thumbnail(self, folder_path: str, file_key: str, dest_dir: str) -> bool:
        """A lightweight background download of an image into a temp folder for
        preview purposes (increment 1b). Not a job — no toasts/progress. Emits
        thumbnail_ready(folder, key, temp_path) once ready, otherwise
        thumbnail_failed."""
        with self._state_lock:
            loop = self._loop
            downloader = self._downloader
            accepting = self._accepting_jobs
        if loop is None or not loop.is_running() or downloader is None or not accepting:
            self.thumbnail_failed.emit(folder_path, file_key)
            return False
        coro = self._run_thumbnail_fetch(folder_path, file_key, dest_dir)
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            self.thumbnail_failed.emit(folder_path, file_key)
            return False
        return True

    async def _run_thumbnail_fetch(
        self, folder_path: str, file_key: str, dest_dir: str
    ) -> None:
        try:
            result = await self._downloader.chunked_download(
                folder_path,
                file_key,
                allow_incomplete=False,
                integrity_mode="fast",
                dest_root=dest_dir,
            )
            output_path = str(result.get("output_path") or "")
            if output_path:
                self.thumbnail_ready.emit(folder_path, file_key, output_path)
            else:
                self.thumbnail_failed.emit(folder_path, file_key)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Thumbnail fetch failed: folder=%s key=%s err=%s",
                folder_path,
                file_key,
                exc,
            )
            self.thumbnail_failed.emit(folder_path, file_key)

    def build_video_poster(
        self, folder_path: str, file_key: str, src_path: str, dest_dir: str
    ) -> bool:
        """Build a poster frame for an already-DOWNLOADED video via ffmpeg
        (increment 4). Not a job, no network — a local file only. Emits
        thumbnail_ready(folder, key, png_path) once ready (the same path field
        as for images), otherwise thumbnail_failed."""
        with self._state_lock:
            loop = self._loop
            accepting = self._accepting_jobs
        if loop is None or not loop.is_running() or not accepting:
            self.thumbnail_failed.emit(folder_path, file_key)
            return False
        coro = self._run_video_poster(folder_path, file_key, src_path, dest_dir)
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            self.thumbnail_failed.emit(folder_path, file_key)
            return False
        return True

    async def _run_video_poster(
        self, folder_path: str, file_key: str, src_path: str, dest_dir: str
    ) -> None:
        from pathlib import Path

        from app.core.utils import extract_video_poster_png

        try:
            dest = Path(dest_dir)
            dest.mkdir(parents=True, exist_ok=True)
            out_png = str(dest / f"vp_{file_key}.png")
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(
                None, lambda: extract_video_poster_png(src_path, out_png)
            )
            if ok:
                self.thumbnail_ready.emit(folder_path, file_key, out_png)
            else:
                self.thumbnail_failed.emit(folder_path, file_key)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Video poster build failed: folder=%s key=%s err=%s",
                folder_path,
                file_key,
                exc,
            )
            self.thumbnail_failed.emit(folder_path, file_key)

    def fetch_video_poster_remote(
        self, folder_path: str, file_key: str, dest_dir: str
    ) -> bool:
        """Poster preview for a NOT-yet-downloaded video: fetch only the FIRST
        part (the file's prefix) via the streaming infrastructure and grab a
        frame with ffmpeg. Not a job, no toasts. Emits thumbnail_ready(folder,
        key, png_path) once ready (the same path field as for images),
        otherwise thumbnail_failed."""
        with self._state_lock:
            loop = self._loop
            downloader = self._downloader
            accepting = self._accepting_jobs
        if loop is None or not loop.is_running() or downloader is None or not accepting:
            self.thumbnail_failed.emit(folder_path, file_key)
            return False
        coro = self._run_video_poster_remote(folder_path, file_key, dest_dir)
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            self.thumbnail_failed.emit(folder_path, file_key)
            return False
        return True

    async def _run_video_poster_remote(
        self, folder_path: str, file_key: str, dest_dir: str
    ) -> None:
        import shutil
        from pathlib import Path

        from app.core.utils import extract_video_poster_png

        prefix_cache = Path(dest_dir) / ".vidprefix" / file_key
        try:
            part_paths = await self._downloader.fetch_parts_decrypted(
                folder_path, file_key, [0], str(prefix_cache)
            )
            prefix_path = part_paths.get(0)
            if not prefix_path:
                self.thumbnail_failed.emit(folder_path, file_key)
                return
            dest = Path(dest_dir)
            dest.mkdir(parents=True, exist_ok=True)
            out_png = str(dest / f"vp_{file_key}.png")
            loop = asyncio.get_running_loop()
            # Grab a frame ~1s in (not the very first, which is often a black
            # fade-in) — the prefix holds many seconds of video, and
            # extract_video_poster_png falls back to frame 0 for short clips.
            ok = await loop.run_in_executor(
                None,
                lambda: extract_video_poster_png(
                    prefix_path, out_png, seek_sec=_REMOTE_POSTER_SEEK_SEC
                ),
            )
            if not ok:
                # The prefix alone couldn't be decoded — typically a
                # non-faststart MP4 (frequently what a ".avi" actually is) whose
                # moov atom lives at the END of the file. Pull the LAST part too
                # and rebuild a sparse head+tail file so ffmpeg can read moov.
                ok = await self._try_remote_poster_with_tail(
                    folder_path, file_key, prefix_path, str(prefix_cache), out_png
                )
            if ok:
                self.thumbnail_ready.emit(folder_path, file_key, out_png)
            else:
                self.thumbnail_failed.emit(folder_path, file_key)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Remote video poster failed: folder=%s key=%s err=%s",
                folder_path,
                file_key,
                exc,
            )
            self.thumbnail_failed.emit(folder_path, file_key)
        finally:
            try:
                shutil.rmtree(prefix_cache, ignore_errors=True)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Failed to clear thumbnail prefix cache %s: %s",
                    prefix_cache,
                    str(exc),
                )

    async def _try_remote_poster_with_tail(
        self,
        folder_path: str,
        file_key: str,
        prefix_path: str,
        cache_dir: str,
        out_png: str,
    ) -> bool:
        """Fallback for a not-downloaded video whose metadata sits at the end:
        fetch the LAST part and reconstruct a sparse head+tail file so ffmpeg
        can find the moov/index. Returns True if a poster was produced."""
        from pathlib import Path

        from app.core.utils import extract_video_poster_png, write_sparse_head_tail

        # Encryption changes on-disk part sizes vs. the plaintext byte offsets
        # that the container's index expects, so the sparse offsets wouldn't
        # line up. Only attempt this for unencrypted objects (the default).
        if self.config.crypto.enabled:
            return False

        parts = self.repo.get_parts_for_object(
            folder_path=folder_path, file_key=file_key
        )
        if not parts:
            return False
        sizes = {int(p.part_index): int(p.file_size or 0) for p in parts}
        last_idx = max(sizes)
        if last_idx <= 0 or any(sizes[i] <= 0 for i in sizes):
            return False  # single part (already tried) or unknown sizes

        tail_offset = sum(sizes[i] for i in range(last_idx))
        total_size = sum(sizes.values())

        tail_paths = await self._downloader.fetch_parts_decrypted(
            folder_path, file_key, [last_idx], cache_dir
        )
        tail_path = tail_paths.get(last_idx)
        if not tail_path:
            return False

        sparse_path = str(Path(cache_dir) / f"sparse_{file_key}.bin")
        loop = asyncio.get_running_loop()

        def _build_and_extract() -> bool:
            if not write_sparse_head_tail(
                sparse_path, prefix_path, tail_path, tail_offset, total_size
            ):
                return False
            # The ~1s frame's samples sit at the start of mdat (inside the head
            # we fetched), so seeking forward decodes from the sparse file and
            # avoids a black first frame.
            return extract_video_poster_png(
                sparse_path, out_png, seek_sec=_REMOTE_POSTER_SEEK_SEC
            )

        return await loop.run_in_executor(None, _build_and_extract)

    async def _enqueue(self, job_type: str, payload: dict[str, Any]) -> None:
        with self._state_lock:
            jobs = self._jobs
            accepting_jobs = self._accepting_jobs
        if jobs is None or not accepting_jobs:
            logger.warning("Dropping job '%s': worker is not accepting jobs", job_type)
            return
        try:
            db_job_id = self.repo.insert_job(
                job_type, payload, status=JobStatus.QUEUED.value
            )
        except Exception as e:
            logger.exception("Failed to insert job into DB: %s", str(e))
            db_job_id = None

        enriched_payload = {**payload}
        if db_job_id is not None:
            enriched_payload["_db_job_id"] = db_job_id

        try:
            runner = self._build_runner(job_type, payload)
            jobs.enqueue(job_type, enriched_payload, runner)
        except Exception as exc:
            logger.exception("Failed to enqueue job '%s': %s", job_type, str(exc))
            if db_job_id is not None:
                try:
                    self.repo.update_job(
                        db_job_id,
                        status=JobStatus.ERROR.value,
                        progress=0.0,
                        error_text=str(exc),
                    )
                except Exception as e:
                    logger.exception(
                        "Failed to persist enqueue error for job id=%s: %s",
                        db_job_id,
                        str(e),
                    )

    async def _run_blob_manifest_recovery(
        self, cancel_token: Any = None
    ) -> dict[str, int]:
        """Rebuild missing batch-blob manifests from Telegram (see
        app.tg.blob_recovery). No-op when no clients are connected."""
        with self._state_lock:
            ctx = self._recovery_ctx
        if ctx is None:
            return {"orphans": 0, "recovered": 0, "members": 0, "failed": 0}
        client_by_chat_id, chat_by_chat_id = ctx

        from app.tg.blob_recovery import recover_blob_manifests

        return await recover_blob_manifests(
            self.repo,
            self.config,
            client_by_chat_id=client_by_chat_id,
            chat_by_chat_id=chat_by_chat_id,
            cancel_token=cancel_token,
        )

    async def _recover_blob_manifests_safe(self, cancel_token: Any = None) -> None:
        """Recovery variant for scan jobs: never fails the parent job."""
        try:
            stats = await self._run_blob_manifest_recovery(cancel_token)
            if stats.get("recovered"):
                logger.info(
                    "Blob manifests recovered after scan: blobs=%d files=%d failed=%d",
                    stats.get("recovered", 0),
                    stats.get("members", 0),
                    stats.get("failed", 0),
                )
        except Exception as exc:  # noqa: BLE001 — recovery must not break scans
            logger.warning("Blob manifest recovery after scan failed: %s", exc)

    def _build_runner(self, job_type: str, payload: dict[str, Any]):
        if self._downloader is None or self._uploader is None:
            raise RuntimeError(
                "Worker components not initialized — cannot run jobs. "
                "Ensure the worker is fully started before submitting jobs."
            )
        if job_type == JobType.DOWNLOAD.value:
            integrity_mode = (
                str(payload.get("integrity_mode", self.config.download_integrity_mode))
                .strip()
                .lower()
            )

            # Blob download: one job pulls a single batch blob and extracts many
            # of its members (folder downloads use this so N files = few jobs).
            if payload.get("_download_blob"):
                blob_key = str(payload.get("blob_key") or "").strip()
                member_file_keys = [
                    str(k)
                    for k in (payload.get("member_file_keys") or [])
                    if str(k or "").strip()
                ]

                async def blob_download_runner(ctx: JobContext) -> Any:
                    await ctx.log(
                        f"Blob download started: {len(member_file_keys)} file(s)"
                    )

                    async def progress(percent: float, message: str) -> None:
                        await ctx.report_progress(percent, message)

                    result = await self._downloader.download_blob_members(
                        blob_key=blob_key,
                        member_file_keys=member_file_keys,
                        integrity_mode=integrity_mode,
                        cancel_token=ctx.cancel_token,
                        progress_cb=progress,
                    )
                    logger.info(
                        "⬇️ Blob download complete: blob=%s members=%s/%s size=%.1f MB",
                        blob_key,
                        result.get("downloaded_members"),
                        result.get("members_expected"),
                        int(result.get("output_total_bytes", 0)) / (1024 * 1024),
                    )
                    return result

                return blob_download_runner

            folder_path = payload["folder_path"]
            file_key = payload["file_key"]
            allow_incomplete = payload.get("allow_incomplete", False)

            async def download_runner(ctx: JobContext) -> Any:
                await ctx.log(f"Download started: {payload.get('orig_name', file_key)}")

                async def progress(percent: float, message: str) -> None:
                    await ctx.report_progress(percent, message)

                result = await self._downloader.chunked_download(
                    folder_path=folder_path,
                    file_key=file_key,
                    allow_incomplete=allow_incomplete,
                    integrity_mode=integrity_mode,
                    cancel_token=ctx.cancel_token,
                    progress_cb=progress,
                )

                # Log the download result
                analytics = result.get("analytics", {})
                speed = analytics.get("speed_mbps", {})
                transfer_mbps = speed.get("transfer_output", 0)
                total_sec = analytics.get("phase_seconds", {}).get("total", 0)
                file_size = analytics.get("bytes", {}).get("output_total", 0)
                logger.info(
                    "⬇️ Download complete: file='%s' size=%.1f MB speed=%.2f MB/s time=%.1fs integrity=%s",
                    payload.get("orig_name", file_key),
                    file_size / (1024 * 1024),
                    transfer_mbps,
                    total_sec,
                    result.get("integrity_mode", "unknown"),
                )
                return result

            return download_runner

        if job_type == JobType.UPLOAD.value:
            file_path = str(payload.get("file_path") or "").strip()
            raw_file_paths = payload.get("file_paths")
            file_paths: list[str] = []
            if isinstance(raw_file_paths, list):
                for raw in raw_file_paths:
                    candidate = str(raw or "").strip()
                    if candidate:
                        file_paths.append(candidate)
            raw_member_folders = payload.get("member_folder_paths")
            member_folder_paths: list[str] | None = None
            if isinstance(raw_member_folders, list):
                member_folder_paths = [
                    str(raw or "").strip()
                    for raw in raw_member_folders
                    if str(raw or "").strip()
                ]
            folder_path = payload.get("folder_path") or ""
            raw_session_batches = payload.get("batches")
            session_upload = bool(payload.get("_ui_small_session")) and bool(
                isinstance(raw_session_batches, list) and raw_session_batches
            )
            session_batches: list[dict] = (
                [b for b in raw_session_batches if isinstance(b, dict)]
                if session_upload
                else []
            )
            grouped_upload = bool(file_paths and not file_path) and not session_upload
            if session_upload:
                start_label = f"{len(session_batches)} batches"
            elif grouped_upload:
                start_label = f"{len(file_paths)} files"
            else:
                if not file_path:
                    raise ValueError(
                        "Upload payload must include 'file_path' or non-empty 'file_paths'"
                    )
                start_label = file_path

            async def upload_runner(ctx: JobContext) -> Any:
                await ctx.log(f"Upload started: {start_label}")

                async def progress(percent: float, message: str) -> None:
                    await ctx.report_progress(percent, message)

                if session_upload:
                    result = await self._uploader.chunked_upload_session(
                        batches=session_batches,
                        cancel_token=ctx.cancel_token,
                        progress_cb=progress,
                    )
                elif grouped_upload:
                    result = await self._uploader.chunked_upload_group(
                        file_paths=file_paths,
                        folder_path=folder_path,
                        member_folder_paths=member_folder_paths,
                        cancel_token=ctx.cancel_token,
                        progress_cb=progress,
                    )
                else:
                    result = await self._uploader.chunked_upload(
                        file_path=file_path,
                        folder_path=folder_path,
                        cancel_token=ctx.cancel_token,
                        progress_cb=progress,
                    )

                # Log the upload result
                analytics = result.get("analytics", {})
                speed = analytics.get("speed_mbps", {})
                transfer_mbps = speed.get("transfer_payload", 0)
                total_sec = analytics.get("phase_seconds", {}).get("total", 0)
                file_size = analytics.get("bytes", {}).get("source_total", 0)
                channels = result.get("channels_used", [])
                clients = result.get("clients_used", [])
                logger.info(
                    "⬆️ Upload complete: file='%s' folder='%s' size=%.1f MB speed=%.2f MB/s time=%.1fs channels=%s",
                    result.get("orig_name", start_label),
                    result.get("folder_path", folder_path),
                    file_size / (1024 * 1024),
                    transfer_mbps,
                    total_sec,
                    channels,
                )
                if clients:
                    logger.info("  📡 Upload clients: %s", clients)

                # Session = many self-indexed batch blobs; no single object to
                # replace-by-name, so finish here.
                if session_upload:
                    return result

                # Replace-by-name: delete any old object with the same name but different key
                new_key = result["file_key"]
                new_name = result["orig_name"]
                new_folder = result["folder_path"]
                stale = [
                    obj
                    for obj in self.repo.list_objects_by_folder(new_folder)
                    if obj.orig_name == new_name and obj.file_key != new_key
                ]
                for obj in stale:
                    await ctx.log(f"Replacing old version: {obj.orig_name}")
                    await self._deleter.delete_remote(
                        obj.folder_path, obj.file_key, cancel_token=ctx.cancel_token
                    )

                return result

            return upload_runner

        if job_type == JobType.REFRESH.value:

            async def refresh_runner(ctx: JobContext) -> Any:
                await ctx.report_progress(10, "Scanning Telegram history")
                stats = await self._scanner.refresh_incremental(
                    cancel_token=ctx.cancel_token
                )
                await self._recover_blob_manifests_safe(ctx.cancel_token)
                await ctx.report_progress(100, f"Indexed parts: {stats.indexed_parts}")
                return {
                    "processed_messages": stats.processed_messages,
                    "indexed_parts": stats.indexed_parts,
                    "max_msg_id": stats.max_msg_id,
                    "deleted_marked": stats.deleted_marked,
                    "parse_skipped": stats.parse_skipped,
                }

            return refresh_runner

        if job_type == JobType.REINDEX.value:

            async def reindex_runner(ctx: JobContext) -> Any:
                await ctx.report_progress(10, "Scanning Telegram history")
                stats = await self._scanner.refresh_full(cancel_token=ctx.cancel_token)
                await self._recover_blob_manifests_safe(ctx.cancel_token)
                await ctx.report_progress(100, f"Indexed parts: {stats.indexed_parts}")
                return {
                    "processed_messages": stats.processed_messages,
                    "indexed_parts": stats.indexed_parts,
                    "max_msg_id": stats.max_msg_id,
                    "deleted_marked": stats.deleted_marked,
                    "parse_skipped": stats.parse_skipped,
                }

            return reindex_runner

        if job_type == JobType.RECONCILE.value:

            async def reconcile_runner(ctx: JobContext) -> Any:
                await ctx.report_progress(10, "Reconciling index with Telegram")
                stats = await self._scanner.reconcile(cancel_token=ctx.cancel_token)
                await self._recover_blob_manifests_safe(ctx.cancel_token)
                await ctx.report_progress(
                    100,
                    f"Reconcile indexed parts: {stats.indexed_parts}; "
                    f"deleted marked: {stats.deleted_marked}",
                )
                return {
                    "processed_messages": stats.processed_messages,
                    "indexed_parts": stats.indexed_parts,
                    "max_msg_id": stats.max_msg_id,
                    "deleted_marked": stats.deleted_marked,
                    "parse_skipped": stats.parse_skipped,
                }

            return reconcile_runner

        if job_type == JobType.DELETE.value:
            folder_path = payload["folder_path"]
            file_key = payload["file_key"]

            async def delete_runner(ctx: JobContext) -> Any:
                await ctx.log(f"Delete remote started: {file_key}")
                return await self._deleter.delete_remote(
                    folder_path, file_key, cancel_token=ctx.cancel_token
                )

            return delete_runner

        if job_type == JobType.DELETE_FOLDER.value:
            folder_path = payload["folder_path"]

            async def delete_folder_runner(ctx: JobContext) -> Any:
                await ctx.log(f"Delete folder started: {folder_path}")

                async def progress(percent: float, message: str) -> None:
                    await ctx.report_progress(percent, message)

                return await self._deleter.delete_folder(
                    folder_path, progress_cb=progress, cancel_token=ctx.cancel_token
                )

            return delete_folder_runner

        if job_type == JobType.RENAME.value:
            folder_path = payload["folder_path"]
            file_key = payload["file_key"]
            new_name = payload["new_name"]

            async def rename_runner(ctx: JobContext) -> Any:
                await ctx.log(f"Renaming '{file_key}' to '{new_name}' in TG…")

                async def progress(percent: float, message: str) -> None:
                    await ctx.report_progress(percent, message)

                result = await self._deleter.rename_file(
                    folder_path,
                    file_key,
                    new_name,
                    progress_cb=progress,
                    cancel_token=ctx.cancel_token,
                )
                await ctx.log(
                    f"TG captions updated: {result['edited']}/{result['total']}"
                    + (f" ({result['failed']} failed)" if result["failed"] else "")
                )
                return result

            return rename_runner

        raise ValueError(f"Unknown job type: {job_type}")

    def cancel_job(self, job_id: int) -> None:
        if self._jobs is not None:
            self._jobs.cancel(job_id)

    def request_stop(self) -> None:
        with self._state_lock:
            self._accepting_jobs = False
            self._stop_requested = True
            self._restart_requested = False
            loop = self._loop
            stop_event = self._stop_event
        logger.info("Worker stop requested")
        if loop is not None and stop_event is not None and loop.is_running():
            loop.call_soon_threadsafe(stop_event.set)

    def request_restart(self) -> None:
        """Request non-blocking restart (no UI-thread wait)."""
        with self._state_lock:
            self._accepting_jobs = False
            self._stop_requested = True
            self._restart_requested = True
            running = self.isRunning()
            loop = self._loop
            stop_event = self._stop_event

        logger.info("Worker restart requested")
        if running:
            if loop is not None and stop_event is not None and loop.is_running():
                loop.call_soon_threadsafe(stop_event.set)
            return

        with self._state_lock:
            self._restart_requested = False
        self._reset_runtime_refs()
        self.start()

    def _set_accepting_jobs(self, accepting: bool) -> None:
        with self._state_lock:
            self._accepting_jobs = bool(accepting)

    def _reset_runtime_refs(self) -> None:
        with self._state_lock:
            self._loop = None
            self._stop_event = None
            self._jobs = None
            self._scanner = None
            self._uploader = None
            self._downloader = None
            self._deleter = None
            self._account_manager = None
            self._upload_accounts = []
            self._recovery_ctx = None
            self._accepting_jobs = False
            self._stop_requested = False
        self._job_persist_progress.clear()
        self._job_persist_ts.clear()
        self._running_log_progress.clear()
        self._running_log_ts.clear()

    def _on_thread_finished(self) -> None:
        restart = False
        with self._state_lock:
            restart = self._restart_requested
            self._restart_requested = False
        self._reset_runtime_refs()
        if restart:
            logger.info("Worker thread finished, starting reconnect thread")
            self.start()
