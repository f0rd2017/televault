"""Explorer panel mixin: grid logic, selection, context menus, local presence."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QPoint
from PySide6.QtWidgets import QApplication, QMenu

from app.core.types import JobType, ObjectEntry
from app.core.utils import normalize_folder_path
from app.ui.models_qt import ExplorerFileItem, ExplorerFolderItem


class ExplorerPanelMixin:
    """Methods for explorer grid, selection, context menus, local presence."""

    def _selected_item(self) -> ExplorerFolderItem | ExplorerFileItem | None:
        selected = sorted(
            self.explorer_view.selectedIndexes(), key=lambda idx: idx.row()
        )
        if selected:
            return self.explorer_model.item_for_index(selected[0])
        index = self.explorer_view.currentIndex()
        return self.explorer_model.item_for_index(index)

    def _selected_objects(self) -> list[ObjectEntry]:
        result: list[ObjectEntry] = []
        seen: set[tuple[str, str]] = set()

        selected = sorted(
            self.explorer_view.selectedIndexes(), key=lambda idx: idx.row()
        )
        for index in selected:
            item = self.explorer_model.item_for_index(index)
            if not isinstance(item, ExplorerFileItem):
                continue
            key = (item.entry.folder_path, item.entry.file_key)
            if key in seen:
                continue
            seen.add(key)
            result.append(item.entry)

        if result:
            return result

        item = self.explorer_model.item_for_index(self.explorer_view.currentIndex())
        if isinstance(item, ExplorerFileItem):
            return [item.entry]
        return []

    def _selected_folder_paths_from_explorer(self) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()

        selected = sorted(
            self.explorer_view.selectedIndexes(), key=lambda idx: idx.row()
        )
        for index in selected:
            item = self.explorer_model.item_for_index(index)
            if not isinstance(item, ExplorerFolderItem):
                continue
            if item.path in seen:
                continue
            seen.add(item.path)
            result.append(item.path)

        if result:
            return self._normalize_folder_delete_targets(result)

        item = self.explorer_model.item_for_index(self.explorer_view.currentIndex())
        if isinstance(item, ExplorerFolderItem):
            return self._normalize_folder_delete_targets([item.path])
        return []

    def _selected_folder_paths_from_tree(self) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()

        selection_model = self.folder_tree.selectionModel()
        selected_rows = (
            selection_model.selectedRows(0) if selection_model is not None else []
        )
        for index in selected_rows:
            folder = self.folder_model.path_from_index(index)
            if not folder or folder in seen:
                continue
            seen.add(folder)
            result.append(folder)

        if result:
            return self._normalize_folder_delete_targets(result)

        folder = self.folder_model.path_from_index(self.folder_tree.currentIndex())
        if folder:
            return self._normalize_folder_delete_targets([folder])
        return []

    def _normalize_folder_delete_targets(self, folder_paths: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in folder_paths:
            path = normalize_folder_path(str(raw or ""))
            if not path or path in seen:
                continue
            seen.add(path)
            normalized.append(path)
        if not normalized:
            return []

        collapsed: list[str] = []
        for folder in sorted(
            normalized, key=lambda value: (value.count("/"), len(value), value.lower())
        ):
            if any(
                folder == parent or folder.startswith(f"{parent}/")
                for parent in collapsed
            ):
                continue
            collapsed.append(folder)
        if len(collapsed) == len(normalized):
            return normalized
        collapsed_set = set(collapsed)
        return [folder for folder in normalized if folder in collapsed_set]

    def _focused_selection_pane(self) -> str | None:
        focused = QApplication.focusWidget()
        if focused is None:
            return None
        if focused is self.folder_tree or self.folder_tree.isAncestorOf(focused):
            return "folder_tree"
        if focused is self.explorer_view or self.explorer_view.isAncestorOf(focused):
            return "explorer"
        return None

    def _resolve_delete_shortcut_targets(self) -> tuple[list[ObjectEntry], list[str]]:
        pane = self._focused_selection_pane()
        if pane == "folder_tree":
            return [], self._selected_folder_paths_from_tree()
        if pane == "explorer":
            return self._selected_objects(), self._selected_folder_paths_from_explorer()

        file_entries = self._selected_objects()
        folder_paths = self._selected_folder_paths_from_explorer()
        if file_entries or folder_paths:
            return file_entries, folder_paths
        return [], self._selected_folder_paths_from_tree()

    def _selected_object(self) -> ObjectEntry | None:
        selected = self._selected_objects()
        return selected[0] if selected else None

    def _selected_file_items(
        self, index: QModelIndex | None = None
    ) -> list[ExplorerFileItem]:
        from app.ui.models_qt import ExplorerFileItem

        selected_indexes = sorted(
            self.explorer_view.selectedIndexes(), key=lambda idx: idx.row()
        )
        items: list[ExplorerFileItem] = []
        seen: set[tuple[str, str]] = set()
        for selected_index in selected_indexes:
            item = self.explorer_model.item_for_index(selected_index)
            if not isinstance(item, ExplorerFileItem):
                continue
            key = (item.entry.folder_path, item.entry.file_key)
            if key in seen:
                continue
            seen.add(key)
            items.append(item)

        if items:
            return items

        if index is not None and index.isValid():
            item = self.explorer_model.item_for_index(index)
            if isinstance(item, ExplorerFileItem):
                return [item]

        current = self._selected_item()
        if isinstance(current, ExplorerFileItem):
            return [current]
        return []

    def _refresh_action_state(self) -> None:
        has_folder = bool(self.current_folder)
        has_object = bool(self._selected_objects())

        self.action_upload.setEnabled(has_folder)
        self.action_download.setEnabled(has_object)
        self.action_delete_local.setEnabled(has_object)
        self.action_delete.setEnabled(has_object)

        self.nav_back_btn.setEnabled(self._history_index > 0)
        self.nav_forward_btn.setEnabled(self._history_index < len(self._history) - 1)
        self.nav_up_btn.setEnabled(bool(self.current_folder))

    def _on_explorer_context_menu(self, pos: QPoint) -> None:
        from app.ui.models_qt import (
            ExplorerFileItem,
            ExplorerFolderItem,
            is_image_name,
            is_pdf_name,
            is_text_editable_name,
            is_video_name,
        )

        index = self.explorer_view.indexAt(pos)
        menu = QMenu(self)
        global_pos = self.explorer_view.viewport().mapToGlobal(pos)

        if index.isValid():
            item = self.explorer_model.item_for_index(index)
            self.explorer_view.setCurrentIndex(index)
            self._refresh_action_state()

            if isinstance(item, ExplorerFileItem):
                if getattr(self, "_trash_view", False):
                    restore_act = menu.addAction("Восстановить")
                    forever_act = menu.addAction("Удалить навсегда")
                    menu.addSeparator()
                    empty_act = menu.addAction("Очистить корзину")
                    triggered = menu.exec(global_pos)
                    if triggered == restore_act:
                        self._on_restore_from_trash()
                    elif triggered == forever_act:
                        self._on_delete_from_trash_forever()
                    elif triggered == empty_act:
                        self._on_empty_trash()
                    return
                open_stream_act = None
                edit_act = None
                if (
                    is_image_name(item.entry.orig_name)
                    or is_video_name(item.entry.orig_name)
                    or is_pdf_name(item.entry.orig_name)
                ):
                    open_stream_act = menu.addAction("Открыть без скачивания")
                    menu.addSeparator()
                elif is_text_editable_name(item.entry.orig_name):
                    edit_act = menu.addAction("Редактировать")
                    menu.addSeparator()
                menu.addAction(self.action_download)
                menu.addAction(self.action_delete_local)
                menu.addSeparator()
                trash_act = menu.addAction("В корзину")
                menu.addAction(self.action_delete)
                menu.addSeparator()
                share_act = menu.addAction("Поделиться ссылкой")
                rename_act = menu.addAction("Переименовать")
                props_act = menu.addAction("Свойства")
                triggered = menu.exec(global_pos)
                if open_stream_act is not None and triggered == open_stream_act:
                    self._on_open_stream(item.entry)
                elif edit_act is not None and triggered == edit_act:
                    self._on_edit_file(item.entry)
                elif triggered == trash_act:
                    self._on_move_to_trash()
                elif triggered == share_act:
                    self._on_share_file(item.entry)
                elif triggered == rename_act:
                    self._on_rename_file(item.entry)
                elif triggered == props_act:
                    self._on_file_properties(item.entry)
                return

            if isinstance(item, ExplorerFolderItem):
                open_act = menu.addAction(f"Открыть '{item.name}'")
                download_folder_act = menu.addAction(f"Скачать папку '{item.name}'")
                sync_folder_act = menu.addAction(f"Синхронизировать '{item.name}'")
                autosync_act = menu.addAction("Автосинхронизация")
                autosync_act.setCheckable(True)
                autosync_act.setChecked(self.repo.is_folder_synced(item.path))
                menu.addSeparator()
                delete_folder_act = menu.addAction(f"Удалить папку '{item.name}'")
                props_folder_act = menu.addAction("Свойства")
                triggered = menu.exec(global_pos)
                if triggered == open_act:
                    self._set_current_folder(
                        item.path, push_history=True, sync_tree=True
                    )
                elif triggered == download_folder_act:
                    self._on_download_folder(folder_path=item.path)
                elif triggered == sync_folder_act:
                    self._on_sync_folder(item.path)
                elif triggered == autosync_act:
                    self._on_toggle_folder_sync(item.path, autosync_act.isChecked())
                elif triggered == delete_folder_act:
                    self._on_delete_folder(item.path)
                elif triggered == props_folder_act:
                    self._on_folder_properties(item.path)
                return

        # Empty space
        if getattr(self, "_trash_view", False):
            empty_act = menu.addAction("Очистить корзину")
            if menu.exec(global_pos) == empty_act:
                self._on_empty_trash()
            return
        if self.current_folder:
            menu.addAction(self.action_upload)
            menu.addSeparator()
        menu.addAction(self.action_create_folder)
        menu.addSeparator()
        menu.addAction(self.action_refresh)
        menu.exec(global_pos)

    def _on_empty_state_context_menu(self, pos: QPoint) -> None:
        global_pos = self._empty_state_label.mapToGlobal(pos)
        explorer_pos = self.explorer_view.viewport().mapFromGlobal(global_pos)
        self._on_explorer_context_menu(explorer_pos)

    def _connected_account_labels(self) -> dict[str, str]:
        """chat_id -> метка аккаунта для подключённых сейчас аккаунтов (best-effort)."""
        labels: dict[str, str] = {}
        manager = getattr(self.worker, "_account_manager", None)
        if manager is None:
            return labels
        try:
            for ca in manager.get_connected():
                chat_id = str(getattr(ca, "chat_id", "") or "").strip()
                if chat_id:
                    labels[chat_id] = str(getattr(ca.account, "label", chat_id))
        except Exception:
            return {}
        return labels

    def _connected_chat_ids(self) -> set[str]:
        return set(self._connected_account_labels().keys())

    def _on_file_properties(self, entry) -> None:
        from app.tg.parser import parse_caption
        from app.ui.dialogs._properties import FilePropertiesDialog

        try:
            parts = self.repo.get_parts_for_object(entry.folder_path, entry.file_key)
        except Exception:
            parts = []

        prefix = str(getattr(self.config, "caption_prefix", "FC1|") or "FC1|")
        expected_sha256: str | None = None
        for part in parts:
            caption = (part.caption_raw or "").strip()
            if not caption:
                continue
            meta = parse_caption(caption, prefix=prefix)
            if meta is not None and meta.sha256:
                expected_sha256 = meta.sha256
                break

        try:
            current_note = self.repo.get_object_note(entry.folder_path, entry.file_key)
        except Exception:
            current_note = ""

        dialog = FilePropertiesDialog(
            entry=entry,
            parts=parts,
            connected_labels=self._connected_account_labels(),
            expected_sha256=expected_sha256,
            note=current_note,
            parent=self,
        )
        from PySide6.QtWidgets import QDialog

        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_note = dialog.note_value
            if new_note != current_note:
                try:
                    self.repo.set_object_note(
                        entry.folder_path, entry.file_key, new_note
                    )
                    self.reload_items()
                except Exception:
                    pass

    def _on_share_file(self, entry) -> None:
        from app.ui.dialogs._properties import ShareLinkDialog

        dialog = ShareLinkDialog(
            entry=entry, repo=self.repo, config=self.config, parent=self
        )
        dialog.exec()

    def _on_folder_properties(self, folder_path: str) -> None:
        from app.ui.dialogs._properties import FolderPropertiesDialog

        try:
            objects = self.repo.list_objects_recursive(folder_path)
        except Exception:
            objects = []

        file_count = len(objects)
        total_size = sum(int(getattr(o, "total_size", 0) or 0) for o in objects)
        state_counts: dict[str, int] = {}
        for obj in objects:
            key = str(getattr(obj, "status", "") or "incomplete")
            state_counts[key] = state_counts.get(key, 0) + 1

        # Подпапки: прямые дети и всё поддерево (по списку всех папок).
        prefix = f"{folder_path}/"
        try:
            all_folders = [f.folder_path for f in self.repo.list_folders()]
        except Exception:
            all_folders = []
        descendants = [f for f in all_folders if f.startswith(prefix)]
        total_subfolders = len(descendants)
        direct_subfolders = sum(1 for f in descendants if "/" not in f[len(prefix) :])

        try:
            synced = bool(self.repo.is_folder_synced(folder_path))
        except Exception:
            synced = False

        dialog = FolderPropertiesDialog(
            folder_path=folder_path,
            name=folder_path.rsplit("/", 1)[-1],
            file_count=file_count,
            total_size=total_size,
            state_counts=state_counts,
            direct_subfolders=direct_subfolders,
            total_subfolders=total_subfolders,
            synced=synced,
            parent=self,
        )
        dialog.exec()

    def _on_rename_file(self, entry: ObjectEntry) -> None:
        from PySide6.QtWidgets import QDialog, QMessageBox

        from app.ui.dialogs import RenameDialog

        dialog = RenameDialog(entry.orig_name, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_name = dialog.new_name()
        if not new_name or new_name == entry.orig_name:
            return
        try:
            self.repo.rename_object(entry.folder_path, entry.file_key, new_name)
            self.reload_items()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Переименование", str(exc))
            return
        # Update TG message captions in background
        self._enqueue_job(
            JobType.RENAME.value,
            {
                "folder_path": entry.folder_path,
                "file_key": entry.file_key,
                "new_name": new_name,
            },
        )

    def _provide_export_paths_for_drag(
        self, index: QModelIndex | None = None
    ) -> list[str] | None:
        selected_items = self._selected_file_items(index)
        if not selected_items:
            return None

        for selected_item in selected_items:
            self.explorer_model.refresh_local_presence_for_object(
                selected_item.entry.folder_path,
                selected_item.entry.file_key,
            )

        refreshed_items = self._selected_file_items(index)
        ready_paths: list[str] = []
        missing_entries: list[ObjectEntry] = []
        seen_paths: set[str] = set()
        for selected_item in refreshed_items:
            path_obj: Path | None = None
            if selected_item.local_path:
                try:
                    resolved = Path(selected_item.local_path).expanduser().resolve()
                    if resolved.exists() and resolved.is_file():
                        path_obj = resolved
                except Exception:
                    path_obj = None
            if path_obj is not None:
                raw = str(path_obj)
                if raw not in seen_paths:
                    seen_paths.add(raw)
                    ready_paths.append(raw)
                continue
            missing_entries.append(selected_item.entry)

        if (
            missing_entries
            and len(self._inflight_requests) >= self.config.max_active_jobs
        ):
            self.progress_widget.append_log(
                "Some selected files are not cached locally. Finish current jobs or use Download first."
            )
        elif missing_entries:
            batch_id = None
            if len(missing_entries) > 1:
                batch_id = self._start_batch_tracking(
                    JobType.DOWNLOAD.value, len(missing_entries)
                )
            queued = 0
            for entry in missing_entries:
                if batch_id is None:
                    self._enqueue_download_entry(entry, for_export=True)
                else:
                    self._enqueue_download_entry(
                        entry,
                        for_export=True,
                        batch_id=batch_id,
                    )
                queued += 1
            self.progress_widget.append_log(
                (
                    f"Selected files not cached locally: queued {queued} download(s). "
                    "Drag again when downloads finish."
                )
            )

        if ready_paths:
            return ready_paths
        return None

    def _on_export_success(self, index: QModelIndex) -> None:
        item = self.explorer_model.item_for_index(index)
        if not isinstance(item, ExplorerFileItem):
            return
        self.explorer_model.mark_recent_export(
            folder_path=item.entry.folder_path,
            file_key=item.entry.file_key,
            ttl_sec=3.0,
        )

    def _refresh_visible_local_presence(self) -> None:
        row_count = self.explorer_model.rowCount()
        step = 36
        if row_count > self._EAGER_LOCAL_PRESENCE_LIMIT:
            step = 120
        changed = self.explorer_model.refresh_local_presence_step(max_items=step)
        changed = self.explorer_model.cleanup_recent_export_marks() or changed
        # Превью картинок: строим миниатюры из локальных файлов/кэша, а для
        # нескачанных — ставим в очередь фоновую дозагрузку (инкремент 1b).
        if bool(getattr(self.config, "show_thumbnails", True)):
            self.explorer_model.refresh_thumbnails_step(max_items=step)
            self._enqueue_video_posters()
            if bool(getattr(self.config, "fetch_thumbnails", True)):
                self._enqueue_thumbnail_fetches()
                self._enqueue_remote_video_posters()
        if changed:
            self._refresh_action_state()

    def _enqueue_thumbnail_fetches(self) -> None:
        """Поставить фоновую дозагрузку нескачанных картинок ради превью (1b).
        Ограничено по числу одновременных, пропускает уже в работе/упавшие."""
        free = self._THUMB_FETCH_MAX_INFLIGHT - len(self._thumb_fetch_inflight)
        if free <= 0:
            return
        candidates = self.explorer_model.image_rows_needing_fetch(max_items=free * 2)
        for item in candidates:
            if free <= 0:
                break
            key = (item.entry.folder_path, item.entry.file_key)
            if key in self._thumb_fetch_inflight or key in self._thumb_fetch_failed:
                continue
            self._thumb_fetch_inflight.add(key)
            ok = self.worker.fetch_thumbnail(
                item.entry.folder_path, item.entry.file_key, self._thumb_fetch_dir
            )
            if not ok:
                self._thumb_fetch_inflight.discard(key)
                self._thumb_fetch_failed.add(key)
                continue
            free -= 1

    def _enqueue_video_posters(self) -> None:
        """Построить кадры-постеры для локальных видео в фоне через ffmpeg (4).
        Делит лимит одновременных с дозагрузкой картинок; ffmpeg-только-локально."""
        if not bool(getattr(self.config, "show_thumbnails", True)):
            return
        free = self._THUMB_FETCH_MAX_INFLIGHT - len(self._thumb_fetch_inflight)
        if free <= 0:
            return
        candidates = self.explorer_model.video_rows_needing_poster(max_items=free * 2)
        for item in candidates:
            if free <= 0:
                break
            key = (item.entry.folder_path, item.entry.file_key)
            if key in self._thumb_fetch_inflight or key in self._thumb_fetch_failed:
                continue
            if not item.local_path:
                continue
            self._thumb_fetch_inflight.add(key)
            ok = self.worker.build_video_poster(
                item.entry.folder_path,
                item.entry.file_key,
                item.local_path,
                self._thumb_fetch_dir,
            )
            if not ok:
                self._thumb_fetch_inflight.discard(key)
                self._thumb_fetch_failed.add(key)
                continue
            free -= 1

    # Максимальный размер видео, для которого тянем превью НЕскачанного файла
    # (берём только первую часть, но и это ограничиваем порогом).
    _REMOTE_POSTER_MAX_BYTES = 50 * 1024 * 1024 * 1024  # 50 GB

    def _enqueue_remote_video_posters(self) -> None:
        """Фоновое построение постера для НЕскачанных видео (тянем только первую
        часть). Делит лимит одновременных и трекинг с дозагрузкой картинок."""
        if not bool(getattr(self.config, "show_thumbnails", True)):
            return
        free = self._THUMB_FETCH_MAX_INFLIGHT - len(self._thumb_fetch_inflight)
        if free <= 0:
            return
        candidates = self.explorer_model.video_rows_needing_remote_poster(
            max_items=free * 2
        )
        for item in candidates:
            if free <= 0:
                break
            total = int(getattr(item.entry, "total_size", 0) or 0)
            if total > self._REMOTE_POSTER_MAX_BYTES:
                continue
            key = (item.entry.folder_path, item.entry.file_key)
            if key in self._thumb_fetch_inflight or key in self._thumb_fetch_failed:
                continue
            self._thumb_fetch_inflight.add(key)
            ok = self.worker.fetch_video_poster_remote(
                item.entry.folder_path, item.entry.file_key, self._thumb_fetch_dir
            )
            if not ok:
                self._thumb_fetch_inflight.discard(key)
                self._thumb_fetch_failed.add(key)
                continue
            free -= 1

    def _on_thumbnail_ready(
        self, folder_path: str, file_key: str, temp_path: str
    ) -> None:
        self._thumb_fetch_inflight.discard((folder_path, file_key))
        try:
            self.explorer_model.set_thumbnail_from_path(
                folder_path, file_key, temp_path
            )
        finally:
            # Временный полный файл нам не нужен — превью уже построено/закэшировано.
            try:
                from pathlib import Path as _Path

                _Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass

    def _on_thumbnail_failed(self, folder_path: str, file_key: str) -> None:
        key = (folder_path, file_key)
        self._thumb_fetch_inflight.discard(key)
        # Не дёргаем повторно ту же картинку в этой сессии (битая/недоступна).
        self._thumb_fetch_failed.add(key)

    def _on_open_stream(self, entry) -> None:
        """Открыть фото/видео во внешнем приложении БЕЗ полного скачивания —
        через локальный стрим-сервер (HTTP Range тянет только нужные части)."""
        from urllib.parse import quote

        from PySide6.QtWidgets import QMessageBox

        server = getattr(self, "api_server", None)
        info = server.ensure_media_server() if server is not None else None
        if not info:
            QMessageBox.warning(
                self,
                "Просмотр",
                "Не удалось запустить локальный сервер для просмотра без скачивания.",
            )
            return
        base, token = info
        url = (
            f"{base}/api/media?folder={quote(entry.folder_path)}"
            f"&file_key={quote(entry.file_key)}&token={quote(token)}"
        )
        from app.ui.media_viewer import open_media_viewer
        from app.ui.models_qt import is_video_name, is_pdf_name

        name = getattr(entry, "orig_name", "") or ""
        if is_video_name(name):
            viewer_type = "video"
        elif is_pdf_name(name):
            viewer_type = "pdf"
        else:
            viewer_type = "image"

        open_media_viewer(
            self,
            url=url,
            title=name or "Просмотр",
            viewer_type=viewer_type,
        )

    def _on_edit_file(self, entry) -> None:
        """Открыть текстовый/кодовый файл в редакторе. Содержимое тянется через
        локальный стрим-сервер (без полного скачивания), а сохранение
        перезаливает файл в облако под тем же именем (replace-by-name)."""
        from urllib.parse import quote

        from PySide6.QtWidgets import QMessageBox

        server = getattr(self, "api_server", None)
        info = server.ensure_media_server() if server is not None else None
        if not info:
            QMessageBox.warning(
                self,
                "Редактор",
                "Не удалось запустить локальный сервер для открытия файла.",
            )
            return
        base, token = info
        url = (
            f"{base}/api/media?folder={quote(entry.folder_path)}"
            f"&file_key={quote(entry.file_key)}&token={quote(token)}"
        )

        from app.ui.text_editor import open_text_editor

        open_text_editor(
            self,
            url=url,
            title=getattr(entry, "orig_name", "") or "Редактор",
            on_save=lambda data, e=entry: self._save_edited_file(e, data),
        )

    def _save_edited_file(self, entry, data: bytes) -> None:
        """Сохранить отредактированное содержимое: пишем во временный файл с тем
        же именем и ставим upload-джобу — replace-by-name обновит объект."""
        from pathlib import Path as _Path

        from PySide6.QtWidgets import QMessageBox

        from app.core.types import JobType

        cache_dir = str(getattr(self.config, "cache_dir", "") or "")
        if not cache_dir:
            cache_dir = str(getattr(self.config, "download_root", "") or ".")
        edit_dir = _Path(cache_dir) / ".edit_cache" / str(entry.file_key)
        name = getattr(entry, "orig_name", "") or "file.txt"
        try:
            edit_dir.mkdir(parents=True, exist_ok=True)
            out_path = edit_dir / name
            out_path.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Редактор", f"Не удалось сохранить файл:\n{exc}")
            return
        self._enqueue_job(
            JobType.UPLOAD.value,
            {"file_path": str(out_path), "folder_path": entry.folder_path},
        )

    def _check_local_exists_cached(self, local_path: Path) -> bool:
        import time

        key = str(local_path)
        now = time.monotonic()
        cached = self._local_presence_cache.get(key)
        if (
            cached is not None
            and (now - cached[0]) <= self._LOCAL_PRESENCE_CACHE_TTL_SEC
        ):
            return bool(cached[1])

        exists = False
        try:
            exists = local_path.exists()
        except OSError:
            exists = False
        self._local_presence_cache[key] = (now, bool(exists))
        self._prune_local_presence_cache()
        return bool(exists)

    def _invalidate_local_presence_cache(self, local_path: Path | None = None) -> None:
        if local_path is None:
            self._local_presence_cache.clear()
            return
        self._local_presence_cache.pop(str(local_path), None)

    def _prune_local_presence_cache(self) -> None:
        max_size = int(self._LOCAL_PRESENCE_CACHE_MAX)
        if len(self._local_presence_cache) <= max_size:
            return
        # Drop oldest entries first to keep recent checks hot.
        overflow = len(self._local_presence_cache) - max_size
        drop_count = max(overflow, max_size // 8)
        oldest = sorted(
            self._local_presence_cache.items(),
            key=lambda kv: kv[1][0],
        )[:drop_count]
        for key, _ in oldest:
            self._local_presence_cache.pop(key, None)
