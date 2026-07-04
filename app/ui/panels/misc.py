"""Misc mixin: create folder, settings, accounts, shortcuts, drag events, cleanup, reload scheduling."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

from app.core.types import JobType
from app.core.utils import has_cryptg
from app.ui.theme import _MAIN_WINDOW_STYLESHEET

if TYPE_CHECKING:
    pass


class MiscMixin:
    """Methods for misc operations: shortcuts, drag events, cleanup, reload scheduling."""

    def _on_create_folder(self, parent_folder: str | None = None) -> None:
        from app.ui.dialogs import CreateFolderDialog

        base = (
            parent_folder if parent_folder is not None else (self.current_folder or "")
        )
        dialog = CreateFolderDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.folder_name()
        if not name:
            return
        full_path = f"{base}/{name}" if base else name
        try:
            self.repo.upsert_folder(full_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, self.tr("Create folder"), str(exc))
            return
        self.reload_all()

    def _on_settings(self) -> None:
        from app.ui.dialogs import SettingsDialog

        initial = self.config.as_public_dict()
        initial["_accounts_repo"] = self.repo
        dialog = SettingsDialog(initial=initial, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            new_config = dialog.to_public_config()
            self.save_config_callback(new_config)

            # Apply icon size dynamically without restart
            new_icon_sz = new_config.get("ui_icon_size", 56)
            if self.explorer_view.iconSize().width() != new_icon_sz:
                import dataclasses
                from PySide6.QtCore import QSize

                self.config = dataclasses.replace(self.config, ui_icon_size=new_icon_sz)
                # Update the model — it rebuilds icons and refreshes SizeHintRole
                self.explorer_model.set_icon_size(new_icon_sz)
                # Update the view
                self.explorer_view.setGridSize(
                    QSize(new_icon_sz + 44, new_icon_sz + 54)
                )
                self.explorer_view.setIconSize(QSize(new_icon_sz, new_icon_sz))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, self.tr("Settings"), str(exc))
            return

    def _on_accounts(self) -> None:
        """Open the account management window."""
        from app.ui.dialogs._accounts import AccountsDialog

        before = self._accounts_signature()
        dlg = AccountsDialog(self.repo, parent=self)
        dlg.exec()
        after = self._accounts_signature()
        if before == after:
            return
        # The worker reads accounts once at startup — without a reconnect,
        # channel/proxy edits won't take effect (this is exactly how files used
        # to end up in only 1 channel).
        choice = QMessageBox.question(
            self,
            self.tr("Apply changes"),
            self.tr(
                "The account list has changed. Changes will only take effect "
                "after reconnecting to Telegram.\n\nReconnect now?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if choice == QMessageBox.StandardButton.Yes:
            self._on_reconnect()

    def _accounts_signature(self) -> tuple:
        """Snapshot of the account set, used to detect changes (channel/proxy/active)."""
        try:
            accounts = self.repo.list_accounts()
        except Exception:
            return ()
        return tuple(
            sorted(
                (
                    str(a.label),
                    str(a.chat_target),
                    str(a.proxy),
                    str(getattr(a, "proxy_backup", "")),
                    bool(a.is_active),
                )
                for a in accounts
            )
        )

    def _on_reconnect(self) -> None:
        self._set_reconnect_enabled(False)
        self.statusBar().showMessage(self.tr("Restarting Telegram connection"))
        self.progress_widget.append_log(self.tr("Restarting Telegram connection…"))
        if hasattr(self, "_startup_overlay"):
            self._startup_overlay.show_loading(
                self.tr("Restarting the Telegram connection…")
            )
        self.worker.request_restart()

    def _on_worker_ready(self) -> None:
        from PySide6.QtCore import QTimer

        # The Telegram connection is ready, but the data (folder tree/index)
        # hasn't been loaded yet — the initial reconciliation runs as a
        # background job. Previously the overlay hid right here, and the user
        # would see "loading finished" while the app was still loading things.
        # Keep the loading screen up until the initial reconciliation completes
        # (see _finish_startup_overlay, triggered via _ui_initial_load).
        if hasattr(self, "_startup_overlay"):
            # Reset the flag (in case of a reconnect) and show the data-loading
            # screen instead of hiding immediately.
            self._startup_overlay_done = False
            self._startup_overlay.show_loading(self.tr("Loading data…"))
            # Safety timeout: if the reconciliation hangs/never finishes, don't
            # leave the user on the loading screen forever.
            QTimer.singleShot(15000, self._finish_startup_overlay)
        self._set_reconnect_enabled(False)
        self.statusBar().showMessage(self.tr("Telegram connected"))
        self.progress_widget.append_log(self.tr("Telegram connected and ready"))
        self._process_pending_enqueue_retries(force=True)
        account_channels = self._get_account_channels()
        if account_channels:
            self.progress_widget.append_log(
                "Channels: "
                + ", ".join(
                    f"#{idx + 1}={chat}" for idx, chat in enumerate(account_channels)
                )
            )
        self.progress_widget.append_log(
            "Routing: "
            f"mode={self.config.channel_sharding_mode}, "
            f"main->ch{int(self.config.main_channel_index) + 1}"
        )
        QTimer.singleShot(120, self._trigger_initial_refresh)
        # Auto-sync folders the user marked (download missing/changed files).
        QTimer.singleShot(900, self._sync_all_marked_folders)
        if not has_cryptg():
            self.progress_widget.append_log(
                "Warning: 'cryptg' is not installed. "
                "Telegram transfer speed can be much lower. Install with: pip install cryptg"
            )
        # Run cache cleanup in background after connect
        if self.config.cache_max_size_mb > 0:
            repo = self.repo
            cache_dir = self.config.cache_dir
            cache_max_bytes = self.config.cache_max_size_mb * 1024 * 1024
            threading.Thread(
                target=self._run_cache_cleanup,
                args=(repo, cache_dir, cache_max_bytes),
                daemon=True,
            ).start()

    def _finish_startup_overlay(self) -> None:
        """Hide the startup loading screen once (idempotent).

        Called when the initial reconciliation finishes (a DONE/ERROR job
        marked with _ui_initial_load), or by the safety timeout."""
        if getattr(self, "_startup_overlay_done", False):
            return
        self._startup_overlay_done = True
        if hasattr(self, "_startup_overlay"):
            self._startup_overlay.finish()

    def _get_account_channels(self) -> list[str]:
        """Read active account chat_targets from the database."""
        try:
            accounts = self.repo.list_accounts()
            return [
                a.chat_target for a in accounts if a.is_active and a.chat_target.strip()
            ]
        except Exception:
            return []

    @staticmethod
    def _run_cache_cleanup(repo, cache_dir: str, cache_max_bytes: int) -> None:
        """Background thread callback for cache cleanup with active job awareness."""
        from app.core.cache import CacheManager, get_active_download_keys_from_repo

        active_keys = get_active_download_keys_from_repo(repo)
        CacheManager().cleanup(
            cache_dir, cache_max_bytes, active_download_keys=active_keys
        )

    def _on_worker_fatal_error(self, message: str) -> None:
        self._set_reconnect_enabled(True)
        self.statusBar().showMessage(self.tr("Telegram disconnected"))
        self.progress_widget.append_log(
            self.tr("Connection error: {0}").format(message)
        )
        if hasattr(self, "_startup_overlay"):
            self._startup_overlay.show_error(message)
        else:
            QMessageBox.critical(self, self.tr("Telegram connection error"), message)

    def _on_worker_reconnect_attempt(self, attempt: int) -> None:
        self.statusBar().showMessage(self.tr("Reconnecting ({0}/4)").format(attempt))
        self.progress_widget.append_log(
            self.tr("Reconnecting (attempt {0}/4)…").format(attempt)
        )
        if hasattr(self, "_startup_overlay"):
            self._startup_overlay.show_loading(
                self.tr("Reconnecting to Telegram ({0}/4)…").format(attempt)
            )

    def _on_account_pool_status(self, status: object) -> None:
        """Show upload-pool health: how many accounts are actually working.
        When some drop out (channel not visible), striping silently collapses,
        so we warn explicitly rather than only logging it."""
        if not isinstance(status, dict):
            return
        active = int(status.get("active", 0))
        total = int(status.get("total", 0))
        degraded = status.get("degraded") or []

        if total <= 1 and not degraded:
            return

        if not degraded:
            self.progress_widget.append_log(
                self.tr(
                    "Accounts active for uploading: {active}/{total} — uploading in "
                    "parallel across {active}."
                ).format(active=active, total=total)
            )
            return

        names = ", ".join(
            f"{d.get('label')} → {d.get('chat_target')}" for d in degraded
        )
        self.statusBar().showMessage(
            self.tr("⚠ Upload is using {active} of {total} accounts").format(
                active=active, total=total
            )
        )
        self.progress_widget.append_log(
            self.tr(
                "⚠ {active}/{total} accounts active. Can't see their channel: {names}. "
                "These accounts' channels aren't scanned: their files won't appear in "
                "the list, and uploads run with fewer threads. Check whether these "
                "accounts have joined their channels."
            ).format(active=active, total=total, names=names)
        )
        QMessageBox.warning(
            self,
            self.tr("Not all accounts are being used"),
            self.tr(
                "{active} of {total} accounts are active for uploading.\n\n"
                "Can't see their channel:\n{names}\n\n"
                "Reason: the account isn't a member of the specified channel. "
                "While the channel is unreachable, it isn't scanned: files already "
                "uploaded to it are temporarily NOT shown in the list (they aren't "
                "deleted — they'll come back once access is restored). Uploads run "
                "with fewer threads.\n\n"
                "Sign in to these channels with these accounts (or auto-join via the "
                "invite link will kick in on the next reconnect)."
            ).format(active=active, total=total, names=names),
        )

    def _set_reconnect_enabled(self, enabled: bool) -> None:
        self.btn_reconnect.setEnabled(bool(enabled))
        self.action_reconnect.setEnabled(bool(enabled))

    def _trigger_initial_refresh(self) -> None:
        # Avoid expensive startup full-scan competing with immediate transfers.
        # _ui_initial_load marker: once this job finishes, we hide the startup
        # loading screen (data has been loaded/reconciled).
        self._enqueue_job(
            JobType.REFRESH.value,
            {"mode": "incremental", "_ui_initial_load": True},
        )

    def _apply_dark_theme(self) -> None:
        self.setStyleSheet(_MAIN_WINDOW_STYLESHEET)

    def _configure_shortcuts(self) -> None:
        # Keep shortcuts explicit and local to this window for predictable behavior.
        self._shortcuts.clear()

        self.action_refresh.setShortcut(QKeySequence("Ctrl+R"))
        self.action_reindex.setShortcut(QKeySequence("F5"))
        self.action_upload.setShortcut(QKeySequence("Ctrl+U"))
        self.action_download.setShortcut(QKeySequence("Ctrl+D"))
        for action in (
            self.action_refresh,
            self.action_reindex,
            self.action_upload,
            self.action_download,
        ):
            self.addAction(action)

        focus_search = QShortcut(QKeySequence("Ctrl+F"), self)
        focus_search.activated.connect(self._focus_search)
        self._shortcuts.append(focus_search)

        nav_back = QShortcut(QKeySequence("Alt+Left"), self)
        nav_back.activated.connect(self._on_nav_back)
        self._shortcuts.append(nav_back)

        nav_forward = QShortcut(QKeySequence("Alt+Right"), self)
        nav_forward.activated.connect(self._on_nav_forward)
        self._shortcuts.append(nav_forward)

        nav_up = QShortcut(QKeySequence("Alt+Up"), self)
        nav_up.activated.connect(self._on_nav_up_shortcut)
        self._shortcuts.append(nav_up)

        backspace_delete_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Backspace), self)
        backspace_delete_shortcut.activated.connect(self._on_delete_shortcut)
        self._shortcuts.append(backspace_delete_shortcut)

        delete_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), self)
        delete_shortcut.activated.connect(self._on_delete_shortcut)
        self._shortcuts.append(delete_shortcut)

        # Download selected file(s) with D — and Cyrillic в/В (same physical key).
        for seq in ("D", "в", "В"):
            download_shortcut = QShortcut(QKeySequence(seq), self)
            download_shortcut.activated.connect(self._on_download_shortcut)
            self._shortcuts.append(download_shortcut)

    def _focus_search(self) -> None:
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def _is_text_input_focused(self) -> bool:
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit):
            return not focused.isReadOnly()
        return False

    def _on_download_shortcut(self) -> None:
        if self._is_text_input_focused():
            return
        if not self._selected_objects():
            return
        self._on_download()

    def _on_delete_shortcut(self) -> None:
        if self._is_text_input_focused():
            return
        file_entries, folder_paths = self._resolve_delete_shortcut_targets()
        if not file_entries and not folder_paths:
            return
        if file_entries and not folder_paths:
            self._on_delete_remote()
            return
        if folder_paths and not file_entries:
            self._confirm_and_enqueue_delete_folders(folder_paths)
            return
        if file_entries:
            self._confirm_and_enqueue_delete_files(file_entries)
        if folder_paths:
            self._confirm_and_enqueue_delete_folders(folder_paths)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls() and any(
            url.isLocalFile() for url in event.mimeData().urls()
        ):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls() and any(
            url.isLocalFile() for url in event.mimeData().urls()
        ):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            paths = [
                url.toLocalFile()
                for url in event.mimeData().urls()
                if url.isLocalFile()
            ]
            if paths:
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
                self._on_files_dropped(paths)
                return
        super().dropEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)

        cw = self.centralWidget()
        margin = 20

        if (
            hasattr(self, "_startup_overlay")
            and self._startup_overlay.parentWidget() == cw
        ):
            self._startup_overlay.setGeometry(cw.rect())

        if hasattr(self, "_toast_overlay") and self._toast_overlay.parentWidget() == cw:
            w = 340
            h = min(500, cw.height() - 100)
            x = cw.width() - w - margin
            y = cw.height() - h - margin
            self._toast_overlay.setGeometry(x, y, w, h)

        if hasattr(self, "progress_widget") and hasattr(
            self.progress_widget, "logs_container"
        ):
            if self.progress_widget.logs_container.parentWidget() == cw:
                w = cw.width() - margin * 2
                h = 196
                y = cw.height() - h - 10
                self.progress_widget.logs_container.setGeometry(margin, y, w, h)

    @staticmethod
    def _cleanup_empty_dirs(start: Path, stop_at: Path) -> None:
        current = start.resolve()
        stop = stop_at.resolve()
        while current != stop and stop in current.parents:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    def _schedule_reload_all(self) -> None:
        self._reload_requested = True
        if not self._reload_debounce_timer.isActive():
            self._reload_debounce_timer.start()

    def _perform_scheduled_reload(self) -> None:
        if not self._reload_requested:
            return
        self._reload_requested = False
        self.reload_all()

    def _queue_error_dialog(
        self, job_id: int, job_type: str, error_message: str
    ) -> None:
        message = str(error_message or "Unknown error").strip()
        self._pending_error_events.append((int(job_id), str(job_type), message))
        if not self._error_dialog_timer.isActive():
            self._error_dialog_timer.start()

    def _flush_error_dialogs(self) -> None:
        if not self._pending_error_events:
            return
        pending = list(self._pending_error_events)
        self._pending_error_events.clear()
        if len(pending) == 1:
            job_id, job_type, message = pending[0]
            QMessageBox.critical(
                self,
                self.tr("Operation error"),
                self.tr("Task #{0} ({1}) failed with an error:\n{2}").format(
                    job_id, job_type, message
                ),
            )
            return

        summary_lines = [self.tr("{0} operations failed:").format(len(pending))]
        for job_id, job_type, message in pending[:4]:
            summary_lines.append(f"- #{job_id} ({job_type}): {message}")
        if len(pending) > 4:
            summary_lines.append(self.tr("... and {0} more").format(len(pending) - 4))
        QMessageBox.critical(self, self.tr("Multiple errors"), "\n".join(summary_lines))

    def _on_folder_context_menu(self, pos) -> None:

        index = self.folder_tree.indexAt(pos)
        menu = QMenu(self)
        global_pos = self.folder_tree.viewport().mapToGlobal(pos)

        if index.isValid():
            folder = self.folder_model.path_from_index(index)
            if folder:
                short = folder.rsplit("/", 1)[-1]
                menu.addAction(
                    self.tr("Create subfolder in '{0}'").format(short),
                    lambda f=folder: self._on_create_folder(f),
                )
                menu.addAction(
                    self.tr("Download folder '{0}'").format(short),
                    lambda f=folder: self._on_download_folder(folder_path=f),
                )
                menu.addAction(
                    self.tr("Sync '{0}'").format(short),
                    lambda f=folder: self._on_sync_folder(f),
                )
                autosync_act = menu.addAction(self.tr("Auto-sync"))
                autosync_act.setCheckable(True)
                autosync_act.setChecked(self.repo.is_folder_synced(folder))
                autosync_act.toggled.connect(
                    lambda checked, f=folder: self._on_toggle_folder_sync(f, checked)
                )
                menu.addSeparator()
                menu.addAction(self.action_refresh)
                menu.addAction(self.action_reconcile)
                menu.addAction(self.action_reindex)
                menu.addSeparator()
                delete_act = menu.addAction(
                    self.tr("Delete folder '{0}'").format(short)
                )
                triggered = menu.exec(global_pos)
                if triggered == delete_act:
                    self._on_delete_folder(folder)
                return

        # Empty space in folder tree
        menu.addAction(self.action_create_folder)
        menu.addSeparator()
        menu.addAction(self.action_refresh)
        menu.addAction(self.action_reconcile)
        menu.addAction(self.action_reindex)
        menu.addSeparator()
        menu.addAction(self.action_settings)
        menu.exec(global_pos)

    def _enqueue_refresh(self, full: bool) -> None:
        payload = {"mode": "full" if full else "incremental"}
        job_type = JobType.REINDEX.value if full else JobType.REFRESH.value
        self._enqueue_job(job_type, payload)

    def _enqueue_reconcile(self) -> None:
        self._enqueue_job(JobType.RECONCILE.value, {"mode": "reconcile"})

    def _on_tray_activated(self, reason) -> None:

        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show()
            self.raise_()
            self.activateWindow()
