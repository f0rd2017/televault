"""Transfer operations mixin: download, delete, progress, busy state."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QMessageBox, QDialog

from app.core.types import JobEvent, JobStatus, JobType, ObjectEntry
from app.core.utils import build_safe_output_path, normalize_folder_path
from app.ui.dialogs import ask_confirm_incomplete_download, ConfirmDialog


class TransferOpsMixin:
    """Methods for download, delete, transfer progress, and busy state."""

    # Окно усреднения скорости/ETA (сек). Скорость = прирост байт за это окно
    # реального времени → устойчиво к пачкам событий и крупным кускам загрузки.
    _ETA_WINDOW_SEC = 6.0
    # Минимальный размах окна, чтобы вообще показывать скорость/ETA (защита от
    # деления на ~0 при двух почти одновременных сэмплах).
    _ETA_MIN_SPAN_SEC = 0.25
    # Лёгкая EMA поверх оконной скорости — сглаживает скачки на краю окна.
    _ETA_SMOOTH_ALPHA = 0.35

    def _on_download(
        self, entry: ObjectEntry | None = None, fast: bool = False
    ) -> None:
        targets = [entry] if entry is not None else self._selected_objects()
        if not targets:
            return
        incomplete_targets = [
            target for target in targets if target.status != "complete"
        ]
        allow_incomplete = False
        if incomplete_targets:
            allow_incomplete = ask_confirm_incomplete_download(self)
            if not allow_incomplete and len(targets) == 1:
                return

        queue_targets = [
            target
            for target in targets
            if target.status == "complete" or allow_incomplete
        ]
        job_count = self._enqueue_download_group(
            queue_targets, fast=fast, allow_incomplete=allow_incomplete
        )
        if job_count > 1:
            self.progress_widget.append_log(
                f"Queued download jobs: {job_count} (from {len(queue_targets)} file(s))"
            )

    def _on_download_folder(
        self, folder_path: str | None = None, fast: bool = False
    ) -> None:
        target_folder = str(folder_path or self.current_folder or "").strip()
        if not target_folder:
            QMessageBox.information(
                self,
                "Скачивание папки",
                "Сначала откройте папку или выберите её в дереве папок.",
            )
            return

        try:
            normalized_folder = normalize_folder_path(target_folder)
        except ValueError as exc:
            QMessageBox.warning(self, "Скачивание папки", str(exc))
            return

        targets = self.repo.list_objects_recursive(normalized_folder)
        if not targets:
            QMessageBox.information(
                self,
                "Скачивание папки",
                f"Папка '{normalized_folder}' пуста.",
            )
            return

        incomplete_targets = [
            target for target in targets if target.status != "complete"
        ]
        allow_incomplete = False
        if incomplete_targets:
            allow_incomplete = ask_confirm_incomplete_download(self)

        queue_targets = [
            target
            for target in targets
            if target.status == "complete" or allow_incomplete
        ]
        if not queue_targets:
            return

        job_count = self._enqueue_download_group(
            queue_targets, fast=fast, allow_incomplete=allow_incomplete
        )
        mode_suffix = " (fast)" if fast else ""
        self.progress_widget.append_log(
            f"Queued folder download{mode_suffix}: {job_count} job(s) for "
            f"{len(queue_targets)} file(s) from '{normalized_folder}'"
        )

    def _enqueue_download_entry(
        self,
        target: ObjectEntry,
        for_export: bool,
        fast: bool = False,
        allow_incomplete_override: bool | None = None,
        batch_id: str | None = None,
    ) -> None:
        allow_incomplete = False
        if target.status != "complete":
            if allow_incomplete_override is None:
                allow_incomplete = ask_confirm_incomplete_download(self)
            else:
                allow_incomplete = bool(allow_incomplete_override)
            if not allow_incomplete:
                return

        payload = {
            "folder_path": target.folder_path,
            "file_key": target.file_key,
            "orig_name": target.orig_name,
            "allow_incomplete": allow_incomplete,
            "for_export": for_export,
            "_ui_total_bytes": int(max(0, int(target.total_size or 0))),
            "_lane": "download",
        }
        if batch_id:
            payload["_ui_batch_id"] = batch_id
        if fast:
            payload["integrity_mode"] = "fast"
        self._enqueue_job(JobType.DOWNLOAD.value, payload)

    def _enqueue_download_group(
        self, queue_targets, *, fast: bool, allow_incomplete: bool
    ) -> int:
        """Enqueue downloads efficiently: regular files = one job each; batch
        members are grouped by their blob so one job pulls a blob and extracts
        all its requested members (avoids 1 job per file → UI flood/freeze).
        Returns the number of jobs enqueued."""
        regular: list = []
        member_groups: dict[str, list] = {}
        for target in queue_targets:
            if getattr(target, "storage_kind", "regular") == "batch_member" and getattr(
                target, "blob_key", None
            ):
                member_groups.setdefault(str(target.blob_key), []).append(target)
            else:
                regular.append(target)

        job_count = len(regular) + len(member_groups)
        batch_id = None
        if job_count > 1:
            batch_id = self._start_batch_tracking(JobType.DOWNLOAD.value, job_count)

        for target in regular:
            self._enqueue_download_entry(
                target,
                for_export=False,
                fast=fast,
                allow_incomplete_override=(
                    allow_incomplete if target.status != "complete" else False
                ),
                batch_id=batch_id,
            )
        for blob_key, members in member_groups.items():
            self._enqueue_blob_download(blob_key, members, fast=fast, batch_id=batch_id)
        return job_count

    def _enqueue_blob_download(
        self, blob_key: str, members: list, *, fast: bool, batch_id: str | None = None
    ) -> None:
        total_bytes = sum(int(getattr(m, "total_size", 0) or 0) for m in members)
        folder = str(getattr(members[0], "folder_path", "") or "") if members else ""
        payload = {
            "_download_blob": True,
            "blob_key": str(blob_key),
            "member_file_keys": [str(m.file_key) for m in members],
            "folder_path": folder,
            "orig_name": f"{len(members)} файл(ов) из пачки",
            "_ui_total_bytes": int(max(0, total_bytes)),
            "_lane": "download",
        }
        if batch_id:
            payload["_ui_batch_id"] = batch_id
        if fast:
            payload["integrity_mode"] = "fast"
        self._enqueue_job(JobType.DOWNLOAD.value, payload)

    def _target_needs_sync(self, target: ObjectEntry) -> bool:
        """A cloud object needs (re)download if its local copy is missing or its
        size differs from the cloud's (i.e. a different/newer version)."""
        try:
            local_path = build_safe_output_path(
                self.config.download_root, target.folder_path, target.orig_name
            )
        except Exception:  # noqa: BLE001
            return False
        if not local_path.exists():
            return True
        if target.total_size is None:
            return False
        try:
            return int(local_path.stat().st_size) != int(target.total_size)
        except OSError:
            return True

    def _sync_folder(self, folder_path: str, *, quiet: bool = False) -> int:
        """Download cloud files in the folder that are missing or changed locally.
        Reuses the blob-grouped download path. Returns the number of jobs queued."""
        try:
            normalized = normalize_folder_path(folder_path)
        except ValueError:
            return 0
        targets = self.repo.list_objects_recursive(normalized)
        needs = [
            t for t in targets if t.status == "complete" and self._target_needs_sync(t)
        ]
        if not needs:
            if not quiet:
                self.progress_widget.append_log(
                    f"Синхронизация '{normalized}': всё актуально"
                )
            return 0
        job_count = self._enqueue_download_group(
            needs, fast=False, allow_incomplete=False
        )
        self.progress_widget.append_log(
            f"Синхронизация '{normalized}': {len(needs)} файл(ов) к загрузке "
            f"(задач: {job_count})"
        )
        return job_count

    def _on_sync_folder(self, folder_path: str) -> None:
        self._sync_folder(folder_path)

    def _on_toggle_folder_sync(self, folder_path: str, enabled: bool) -> None:
        """Enable/disable auto-sync for a folder. Enabling runs a sync right away;
        synced folders are also re-synced on each connect (see _on_worker_ready)."""
        try:
            normalized = normalize_folder_path(folder_path)
        except ValueError:
            return
        self.repo.set_folder_sync(normalized, enabled)
        if enabled:
            self.progress_widget.append_log(
                f"Автосинхронизация включена: '{normalized}'"
            )
            self._sync_folder(normalized, quiet=True)
        else:
            self.progress_widget.append_log(
                f"Автосинхронизация выключена: '{normalized}'"
            )

    def _sync_all_marked_folders(self) -> None:
        """Re-sync every folder marked for auto-sync (called when worker ready)."""
        try:
            folders = self.repo.list_synced_folders()
        except Exception:  # noqa: BLE001
            return
        total_jobs = 0
        for folder in folders:
            total_jobs += self._sync_folder(folder, quiet=True)
        if total_jobs > 0:
            self.progress_widget.append_log(
                f"Автосинхронизация: поставлено задач — {total_jobs}"
            )

    def _on_delete_remote(self) -> None:
        entries = self._selected_objects()
        self._confirm_and_enqueue_delete_files(entries)

    # === Корзина (soft-delete) ===

    def _on_move_to_trash(self) -> None:
        entries = self._selected_objects()
        if not entries:
            return
        moved = 0
        for entry in entries:
            try:
                self.repo.move_to_trash(
                    entry.folder_path,
                    entry.file_key,
                    entry.orig_name,
                    getattr(entry, "storage_kind", "regular"),
                    entry.total_size,
                )
                moved += 1
            except Exception as exc:  # noqa: BLE001
                self.progress_widget.append_log(f"В корзину не удалось: {exc}")
        if moved:
            self.progress_widget.append_log(f"В корзину: {moved} файл(ов)")
            self.reload_items()

    def _on_restore_from_trash(self) -> None:
        entries = self._selected_objects()
        if not entries:
            return
        for entry in entries:
            try:
                self.repo.restore_from_trash(entry.folder_path, entry.file_key)
            except Exception:  # noqa: BLE001
                pass
        self.reload_items()

    def _on_delete_from_trash_forever(self) -> None:
        entries = self._selected_objects()
        if not entries:
            return
        # Реальный remote-delete (с подтверждением), затем убираем из корзины.
        if not self._confirm_and_enqueue_delete_files(entries):
            return
        for entry in entries:
            try:
                self.repo.delete_trash_entry(entry.folder_path, entry.file_key)
            except Exception:  # noqa: BLE001
                pass
        self.reload_items()

    def _on_empty_trash(self) -> None:
        try:
            entries = self.repo.list_trash()
        except Exception:  # noqa: BLE001
            entries = []
        if not entries:
            return
        if not self._confirm_and_enqueue_delete_files(entries):
            return
        for entry in entries:
            try:
                self.repo.delete_trash_entry(entry.folder_path, entry.file_key)
            except Exception:  # noqa: BLE001
                pass
        self.reload_items()

    def _confirm_and_enqueue_delete_files(self, entries: list[ObjectEntry]) -> bool:
        if not entries:
            return False

        if len(entries) == 1:
            dialog = ConfirmDialog(
                title="Удаление из облака",
                message=f"Удалить '{entries[0].orig_name}' из Telegram?",
                parent=self,
                is_destructive=True,
            )
        else:
            dialog = ConfirmDialog(
                title="Удаление из облака",
                message=f"Удалить выбранные файлы ({len(entries)} шт.) из Telegram?",
                parent=self,
                is_destructive=True,
            )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False

        batch_id = None
        if len(entries) > 1:
            batch_id = self._start_batch_tracking(JobType.DELETE.value, len(entries))
        for entry in entries:
            payload = {"folder_path": entry.folder_path, "file_key": entry.file_key}
            if batch_id:
                payload["_ui_batch_id"] = batch_id
            self._enqueue_job(JobType.DELETE.value, payload)
        if len(entries) > 1:
            self.progress_widget.append_log(
                f"Queued remote delete for {len(entries)} file(s)"
            )
        return True

    def _on_delete_local(self) -> None:
        entries = self._selected_objects()
        if not entries:
            return

        if len(entries) > 1:
            dialog = ConfirmDialog(
                title="Удалить локально",
                message=f"Удалить локальные копии для выбранных файлов ({len(entries)} шт.)?",
                parent=self,
                is_destructive=True,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return

        deleted = 0
        missing = 0
        errors: list[str] = []
        cache_root = Path(self.config.download_root).resolve()
        missing_example_path: Path | None = None

        for entry in entries:
            try:
                local_path = build_safe_output_path(
                    self.config.download_root,
                    entry.folder_path,
                    entry.orig_name,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                continue

            if not local_path.exists():
                missing += 1
                if missing_example_path is None:
                    missing_example_path = local_path
                continue

            try:
                local_path.unlink()
                self._invalidate_local_presence_cache(local_path)
                deleted += 1
                self.progress_widget.append_log(f"Deleted local: {local_path}")
                self._cleanup_empty_dirs(local_path.parent, cache_root)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        if len(entries) == 1 and missing:
            local_path = missing_example_path or Path(entries[0].orig_name)
            QMessageBox.information(
                self, "Удаление локального", f"Файл не найден:\n{local_path}"
            )
            return

        if deleted > 0:
            self.progress_widget.append_log(
                f"Deleted local files: {deleted} (missing: {missing})"
            )
            self._refresh_visible_local_presence()
            self._refresh_action_state()

        if errors:
            details = "\n".join(errors[:3])
            if len(errors) > 3:
                details += f"\n... and {len(errors) - 3} more"
            QMessageBox.critical(self, "Удаление локального", details)

    def _on_delete_folder(self, folder_path: str) -> None:
        self._confirm_and_enqueue_delete_folders([folder_path])

    def _confirm_and_enqueue_delete_folders(self, folder_paths: list[str]) -> bool:
        targets = self._normalize_folder_delete_targets(folder_paths)
        if not targets:
            return False

        file_counts = [
            len(self.repo.list_objects_recursive(folder)) for folder in targets
        ]
        total_files = sum(file_counts)
        if len(targets) == 1:
            dialog = ConfirmDialog(
                title="Удаление папки",
                message=f"Удалить все файлы ({total_files} шт.) в папке '{targets[0]}' из Telegram?",
                parent=self,
                is_destructive=True,
            )
        else:
            dialog = ConfirmDialog(
                title="Удаление папок",
                message=f"Удалить все файлы ({total_files} шт.) в выбранных папках ({len(targets)} шт.) из Telegram?",
                parent=self,
                is_destructive=True,
            )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False

        batch_id = None
        if len(targets) > 1:
            batch_id = self._start_batch_tracking(
                JobType.DELETE_FOLDER.value, len(targets)
            )
        for folder in targets:
            payload = {"folder_path": folder}
            if batch_id:
                payload["_ui_batch_id"] = batch_id
            self._enqueue_job(JobType.DELETE_FOLDER.value, payload)
        if len(targets) > 1:
            self.progress_widget.append_log(
                f"Queued remote delete for {len(targets)} folder(s)"
            )
        return True

    def _calc_global_progress(self, current_pct: float | None = None) -> float:
        _ = current_pct
        transfer_done_bytes, transfer_total_bytes = (
            self._global_transfer_progress_bytes()
        )
        if transfer_total_bytes > 0.0:
            return max(
                0.0, min(100.0, (transfer_done_bytes / transfer_total_bytes) * 100.0)
            )

        if not self._active_jobs:
            return 0.0

        weighted_progress = 0.0
        total_weight = 0.0
        for job_id in self._active_jobs:
            progress = max(0.0, min(100.0, float(self._job_progress.get(job_id, 0.0))))
            weight = max(1.0, float(self._job_progress_weight.get(job_id, 1.0)))
            weighted_progress += progress * weight
            total_weight += weight
        if total_weight <= 0.0:
            return 0.0
        return weighted_progress / total_weight

    @staticmethod
    def _is_transfer_job_type(job_type: str) -> bool:
        return str(job_type).strip().lower() in {
            JobType.UPLOAD.value,
            JobType.DOWNLOAD.value,
        }

    def _pending_transfer_total_bytes(self) -> float:
        total = 0.0
        for payload in self._pending_upload_jobs:
            total += max(
                0.0, self._progress_weight_from_payload(JobType.UPLOAD.value, payload)
            )
        for record in self._pending_enqueue_retries.values():
            if not isinstance(record, dict):
                continue
            job_type = str(record.get("job_type") or "").strip().lower()
            if not self._is_transfer_job_type(job_type):
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            total += max(0.0, self._progress_weight_from_payload(job_type, payload))
        return total

    def _accumulate_finished_transfer_job(
        self, event: JobEvent, payload: dict[str, Any]
    ) -> None:
        if event.job_id < 0 or event.job_id in self._finalized_transfer_jobs:
            return
        if not self._is_transfer_job_type(event.job_type):
            return

        weight = float(
            self._job_progress_weight.get(
                event.job_id,
                self._progress_weight_from_payload(event.job_type, payload),
            )
        )
        weight = max(0.0, weight)
        self._finalized_transfer_jobs.add(event.job_id)
        if weight <= 0.0:
            return

        prev_progress = max(
            0.0, min(100.0, float(self._job_progress.get(event.job_id, event.progress)))
        )
        if event.status == JobStatus.DONE:
            final_progress = 100.0
        else:
            final_progress = max(
                prev_progress, max(0.0, min(100.0, float(event.progress)))
            )

        self._finished_transfer_total_bytes += weight
        self._finished_transfer_done_bytes += weight * (final_progress / 100.0)
        if self._finished_transfer_done_bytes > self._finished_transfer_total_bytes:
            self._finished_transfer_done_bytes = self._finished_transfer_total_bytes

    def _global_transfer_progress_bytes(self) -> tuple[float, float]:
        done_bytes = max(0.0, float(self._finished_transfer_done_bytes))
        total_bytes = max(0.0, float(self._finished_transfer_total_bytes))
        for job_id in self._active_jobs:
            job_type = str(self._job_type_by_id.get(job_id, "")).strip().lower()
            if not self._is_transfer_job_type(job_type):
                continue
            weight = max(0.0, float(self._job_progress_weight.get(job_id, 0.0)))
            if weight <= 0.0:
                continue
            progress = max(0.0, min(100.0, float(self._job_progress.get(job_id, 0.0))))
            total_bytes += weight
            done_bytes += weight * (progress / 100.0)
        total_bytes += self._pending_transfer_total_bytes()
        if done_bytes > total_bytes:
            done_bytes = total_bytes
        return done_bytes, total_bytes

    def _build_global_progress_status(self) -> str | None:
        done_bytes, total_bytes = self._global_transfer_progress_bytes()
        if total_bytes <= 0.0:
            self._reset_eta_estimator()
            return None

        active_types = {
            str(self._job_type_by_id.get(job_id, "")).strip().lower()
            for job_id in self._active_jobs
        }
        has_upload = (JobType.UPLOAD.value in active_types) or bool(
            self._pending_upload_jobs
        )
        has_download = JobType.DOWNLOAD.value in active_types
        if has_upload and has_download:
            activity = "Transferring"
        elif has_upload:
            activity = "Uploading"
        elif has_download:
            activity = "Downloading"
        else:
            activity = "Working"

        now = time.monotonic()
        samples = self._eta_samples
        # Регресс done_bytes (сменился активный набор: джоба завершилась/новая
        # добавилась, агрегат пересчитан) — окно больше не валидно, начинаем заново.
        if samples and done_bytes + 1.0 < samples[-1][1]:
            samples.clear()
        samples.append((now, done_bytes))
        # Выкидываем сэмплы старше окна, но оставляем минимум 2 точки для расчёта.
        while len(samples) > 2 and (now - samples[0][0]) > self._ETA_WINDOW_SEC:
            samples.popleft()

        if len(samples) >= 2:
            t0, b0 = samples[0]
            # Знаменатель флорим — защита от деления на ~0, когда два сэмпла
            # пришли почти одновременно (в реале span настоящий и флор не влияет).
            span = max(self._ETA_MIN_SPAN_SEC, now - t0)
            # Средняя скорость за реальное окно — устойчива к пачкам событий и к
            # тому, что часть «падает» целиком за один опрос.
            window_speed = max(0.0, (done_bytes - b0) / span)
            if window_speed > 0.0:
                if self._eta_display_speed_bps <= 0.0:
                    self._eta_display_speed_bps = window_speed
                else:
                    # Лёгкая EMA поверх окна гасит ступеньки при вытеснении
                    # крайнего сэмпла.
                    self._eta_display_speed_bps = (
                        self._ETA_SMOOTH_ALPHA * window_speed
                        + (1.0 - self._ETA_SMOOTH_ALPHA) * self._eta_display_speed_bps
                    )
            elif (now - t0) >= self._ETA_WINDOW_SEC:
                # Прогресса нет всё окно — реальный стопор, честно показываем 0
                # (а не плавно затухаем по числу вызовов).
                self._eta_display_speed_bps = 0.0
            # иначе короткая пауза между кусками — держим прошлую скорость.

        pct = max(0.0, min(100.0, (done_bytes / total_bytes) * 100.0))
        if pct < 1.0:
            percent_text = f"{pct:.1f}%"
        else:
            percent_text = f"{int(pct)}%"

        speed_bps = max(0.0, float(self._eta_display_speed_bps))
        remaining_bytes = max(0.0, total_bytes - done_bytes)
        if speed_bps >= 1.0 and remaining_bytes > 0.0:
            eta_seconds = remaining_bytes / speed_bps
            return (
                f"{activity} {percent_text} | ETA {self._human_eta(eta_seconds)} "
                f"| {self._human_speed(speed_bps)}"
            )
        return f"{activity} {percent_text}"

    def _reset_eta_estimator(self) -> None:
        self._eta_samples.clear()
        self._eta_display_speed_bps = 0.0

    def _reset_global_transfer_window(self) -> None:
        self._finished_transfer_total_bytes = 0.0
        self._finished_transfer_done_bytes = 0.0
        self._finalized_transfer_jobs.clear()
        self._reset_eta_estimator()

    @staticmethod
    def _human_speed(speed_bps: float) -> str:
        speed = max(0.0, float(speed_bps))
        if speed < 1024:
            return f"{speed:.0f} B/s"
        if speed < 1024 * 1024:
            return f"{speed / 1024.0:.0f} KB/s"
        return f"{speed / (1024.0 * 1024.0):.1f} MB/s"

    @staticmethod
    def _human_eta(seconds: float) -> str:
        total = max(0, int(round(float(seconds))))
        # Квантуем по «приятным» шагам, чтобы значение не дёргалось на ±1 при
        # микроколебаниях скорости (14s→13s→15s выглядит нервно).
        if total <= 10:
            pass  # секунды как есть — мелкие значения и так понятны
        elif total < 60:
            total = int(round(total / 5.0)) * 5
        elif total < 600:
            total = int(round(total / 15.0)) * 15
        else:
            total = int(round(total / 30.0)) * 30
        if total < 60:
            return f"{total}s"
        minutes, sec = divmod(total, 60)
        if minutes < 60:
            return f"{minutes}m {sec:02d}s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes:02d}m"

    def _sync_busy_state(self, activity_hint: str | None = None) -> None:
        busy = bool(
            self._inflight_requests
            or self._active_jobs
            or self._pending_upload_jobs
            or self._pending_enqueue_retries
        )
        if busy:
            self.progress_widget.set_busy(True, activity_hint or "Working")
            if not self._watchdog_timer.isActive():
                self._watchdog_timer.start()
        else:
            self.progress_widget.set_busy(False)
            self.progress_widget.set_status_text(None)
            self._watchdog_timer.stop()

    def _set_transfer_state_from_job_payload(
        self,
        job_type: str,
        payload: dict | None,
        state: str | None,
    ) -> None:
        if job_type != JobType.DOWNLOAD.value or not payload:
            return
        folder_path = str(payload.get("folder_path") or "").strip()
        file_key = str(payload.get("file_key") or "").strip()
        if not folder_path or not file_key:
            return
        self.explorer_model.set_transfer_state(folder_path, file_key, state)
        self._sync_loading_badge_timer()

    @staticmethod
    def _activity_for_job(job_type: str) -> str:
        mapping = {
            JobType.UPLOAD.value: "Uploading",
            JobType.DOWNLOAD.value: "Downloading",
            JobType.DELETE.value: "Deleting",
            JobType.DELETE_FOLDER.value: "Deleting folder",
            JobType.RENAME.value: "Renaming in TG",
            JobType.REFRESH.value: "Refreshing",
            JobType.RECONCILE.value: "Reconciling",
            JobType.REINDEX.value: "Reindexing",
        }
        return mapping.get(job_type, "Working")

    def _sync_loading_badge_timer(self) -> None:
        if self.explorer_model.has_active_loading_transfers():
            if not self._loading_badge_timer.isActive():
                self._loading_badge_timer.start()
            return
        self._loading_badge_timer.stop()

    def _advance_loading_badges(self) -> None:
        animated = self.explorer_model.advance_loading_animation()
        if not animated:
            self._loading_badge_timer.stop()
