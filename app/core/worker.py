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
    # Превью картинок (инкремент 1b): (folder_path, file_key, temp_image_path)
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
                "Add accounts via: python scripts/manage_accounts.py"
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
        # see their channel, striping silently degrades (e.g. 1 поток вместо 3).
        try:
            connected = account_manager.get_connected()
            degraded = [
                {
                    "label": str(ca.account.label),
                    "chat_target": str(ca.account.chat_target),
                    "reason": "канал не виден (аккаунт не вступил в канал?)",
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
                "Add user accounts via: python scripts/manage_accounts.py"
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
            logger.info("🚀 Запуск автоматической сверки базы данных с Telegram...")
            # Передаем None, так как scanner ожидает CancelToken, а не asyncio.Event
            stats = await scanner.reconcile(cancel_token=None)
            logger.info(
                "✅ Сверка завершена: удалено записей: %d, проиндексировано: %d",
                stats.deleted_marked,
                stats.indexed_parts,
            )
        except Exception as e:
            logger.error("Ошибка при автоматической сверке: %s", e)

        # Initialize core components

        # Upload через user accounts
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

        # Download через user accounts или основной аккаунт
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

        # Delete/реконсиляция через user accounts или основной аккаунт
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

        # Валидация: убедиться что хотя бы один валидный роут зарегистрирован
        if not deleter._routes_by_chat_id:
            raise RuntimeError(
                "No valid delete routes configured. "
                "Ensure all Telegram accounts have proper channel access and chat entities are resolved."
            )

        # Логирование количества роутов для отладки
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
            # Отключаем мультиаккаунты
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
            # Rate-limit: максимум 10 обновлений в секунду (интервал >= 100мс)
            # ИЛИ изменение прогресса >= 1%
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
        # Оптимизация: сохраняем каждые 25% вместо 10% для снижения нагрузки на SQLite
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
        """Собрать файл из чанков на диск и вернуть путь (для шар-ссылок,
        инкремент 8). БЛОКИРУЕТ вызывающий поток (HTTP-обработчик), пока сборка
        идёт в loop воркера. None при ошибке/таймауте. Не джоба — без тостов."""
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
        """Скачать+расшифровать ТОЛЬКО указанные части в ``cache_dir`` и вернуть
        ``{part_index: путь}`` (стрим без полного скачивания, инкремент 9/10).
        БЛОКИРУЕТ вызывающий поток (HTTP-обработчик), пока скачивание идёт в loop
        воркера. Пустой dict при ошибке/таймауте/неготовности — раздача тогда
        откатится на полную сборку файла.

        ``prefix_bytes`` — см. ``TgDownloader.fetch_parts_decrypted``: для
        незашифрованных объектов позволяет скачать только начало огромной части
        вместо неё целиком, чтобы плеер стартовал быстрее."""
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
        """Лёгкая фоновая дозагрузка картинки во временную папку ради превью
        (инкремент 1b). Не джоба — без тостов/прогресса. По готовности эмитит
        thumbnail_ready(folder, key, temp_path), иначе thumbnail_failed."""
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
        """Построить кадр-постер для уже СКАЧАННОГО видео через ffmpeg (инкремент 4).
        Не джоба, без сети — только локальный файл. По готовности эмитит
        thumbnail_ready(folder, key, png_path) (тот же путь, что и у картинок),
        иначе thumbnail_failed."""
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
        """Превью-постер для НЕскачанного видео: тянем только ПЕРВУЮ часть
        (префикс файла) через стрим-инфраструктуру и снимаем кадр ffmpeg.
        Не джоба, без тостов. По готовности эмитит thumbnail_ready(folder, key,
        png_path) (тот же путь, что и у картинок), иначе thumbnail_failed."""
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
            ok = await loop.run_in_executor(
                None,
                lambda: extract_video_poster_png(prefix_path, out_png, seek_sec=0.0),
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

                # Логирование результата download
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

                # Логирование результата upload
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
