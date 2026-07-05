"""
Simple window for managing Telegram accounts.
A table + management buttons; new accounts are added and authorized via
the built-in AddAccountDialog (no terminal needed).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QInputDialog,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core.types import TelegramAccount
from app.db.repo import DbRepo

logger = logging.getLogger(__name__)

# Keep live prober threads alive so GC doesn't collect them if the dialog is
# closed before the check finishes (the thread isn't parented to the dialog).
_ACTIVE_PROBES: set[QThread] = set()


class _StatusProbe(QThread):
    """Background liveness check for proxies and accounts.

    For each account:
      - proxy: a real TCP connect via python_socks to the Telegram DC
        (resolve_working_proxy) — doesn't touch the session;
      - account: a real connect + is_user_authorized via Telethon.
    The result for each row is delivered to the GUI thread via the row_status signal.
    """

    # Sent by account_id (not row index): if the account list changes while the
    # check is running, the result still lands on the correct row.
    row_status = Signal(int, str, str)  # account_id, proxy_status, account_status
    # Completion is signaled via the built-in QThread.finished — no separate signal
    # is needed (and a "done" name would be confused with QDialog.done).

    _CONNECT_TIMEOUT = 12.0

    def __init__(self, accounts: list[TelegramAccount]) -> None:
        super().__init__()
        self._accounts = list(accounts)

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            for acc in self._accounts:
                if self.isInterruptionRequested():
                    break
                proxy_status, proxy_tuple = self._probe_proxy(acc)
                try:
                    acc_status = loop.run_until_complete(
                        self._probe_account(acc, proxy_tuple)
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Account probe crashed for %s: %s", acc.label, exc)
                    acc_status = self.tr("❌ error")
                if self.isInterruptionRequested():
                    break
                self.row_status.emit(int(acc.id), proxy_status, acc_status)
        finally:
            try:
                loop.close()
            except Exception:
                pass

    @staticmethod
    def _probe_proxy(acc: TelegramAccount) -> tuple[str, tuple | None]:
        from app.core.utils import select_working_proxy_from_chain

        chain = [p for p in (acc.proxy, acc.proxy_backup) if str(p or "").strip()]
        # The primary account always connects directly.
        if acc.is_primary or not chain:
            return (QCoreApplication.translate("_StatusProbe", "— direct"), None)
        proxy, _label, tier = select_working_proxy_from_chain(chain)
        if proxy is None:
            return (
                QCoreApplication.translate("_StatusProbe", "❌ dead → direct"),
                None,
            )
        if tier > 0:
            return (QCoreApplication.translate("_StatusProbe", "✅ backup"), proxy)
        return (QCoreApplication.translate("_StatusProbe", "✅ working"), proxy)

    @staticmethod
    def _session_disk_path(session_path: str) -> str:
        """Actual path to the .session file (Telethon appends the extension itself)."""
        p = str(session_path or "")
        return p if p.endswith(".session") else p + ".session"

    @classmethod
    async def _probe_account(
        cls, acc: TelegramAccount, proxy_tuple: tuple | None
    ) -> str:
        from telethon import TelegramClient

        # Check against a COPY of the session: same auth_key, but writes go to a
        # temporary file — we don't touch the real session of the running app
        # (no races/locks).
        tmp_dir = tempfile.mkdtemp(prefix="tgprobe_")
        tmp_session = os.path.join(tmp_dir, "probe.session")
        disk = cls._session_disk_path(acc.session_path)
        authorized_possible = False
        if os.path.exists(disk):
            try:
                shutil.copy2(disk, tmp_session)
                authorized_possible = True
            except OSError as exc:
                logger.debug("Cannot copy session for '%s': %s", acc.label, exc)
        if not authorized_possible:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return cls.tr("⚠️ no session")

        client = TelegramClient(
            tmp_session,
            acc.tg_api_id,
            acc.tg_api_hash,
            proxy=proxy_tuple if proxy_tuple else None,
        )
        try:
            await asyncio.wait_for(client.connect(), timeout=cls._CONNECT_TIMEOUT)
            if not await client.is_user_authorized():
                return cls.tr("⚠️ not authorized")
            me = await client.get_me()
            uname = getattr(me, "username", None)
            premium = " ⭐" if getattr(me, "premium", False) else ""
            tail = f" @{uname}" if uname else ""
            return f"{cls.tr('✅ online')}{premium}{tail}"
        except asyncio.TimeoutError:
            return cls.tr("❌ timeout")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Account '%s' unreachable: %s", acc.label, exc)
            return cls.tr("❌ unreachable")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            shutil.rmtree(tmp_dir, ignore_errors=True)


class AccountsDialog(QDialog):
    """Account management window."""

    COL_ID = 0
    COL_LABEL = 1
    COL_PHONE = 2
    COL_USERNAME = 3
    COL_CHANNEL = 4
    COL_PRIMARY = 5
    COL_ACTIVE = 6
    COL_PROXY = 7
    COL_PROXY_STATUS = 8
    COL_ACC_STATUS = 9

    def __init__(
        self,
        repo: DbRepo,
        parent=None,
        *,
        default_api_id: int = 0,
        default_api_hash: str = "",
    ):
        super().__init__(parent)
        self.repo = repo
        self._default_api_id = default_api_id
        self._default_api_hash = default_api_hash
        self._accounts: list[TelegramAccount] = []
        self._probe: _StatusProbe | None = None
        self.setWindowTitle(self.tr("Telegram Accounts"))
        self.setMinimumSize(980, 520)
        self.resize(1080, 560)

        self.setStyleSheet("""
            QDialog {
                background-color: #09090b;
            }
            QLabel {
                color: #e4e4e7;
            }
            QTableWidget {
                background-color: #18181b;
                color: #e4e4e7;
                border: 1px solid #27272a;
                border-radius: 8px;
                gridline-color: #27272a;
            }
            QTableWidget::item:selected {
                background-color: #3b0764;
                color: #ffffff;
            }
            QHeaderView::section {
                background-color: #09090b;
                color: #a1a1aa;
                border: none;
                border-bottom: 1px solid #27272a;
                padding: 6px;
                font-weight: bold;
            }
            QTableCornerButton::section {
                background-color: #09090b;
                border: none;
            }
            QPushButton {
                background-color: #27272a;
                color: #e4e4e7;
                border: 1px solid #3f3f46;
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #3f3f46;
                color: #ffffff;
            }
            QPushButton#primaryBtn {
                background-color: #6d28d9;
                border-color: #5b21b6;
                color: #ffffff;
            }
            QPushButton#primaryBtn:hover {
                background-color: #7c3aed;
            }
            QPushButton#removeBtn {
                color: #fca5a5;
                border-color: #7f1d1d;
                background-color: #450a0a;
            }
            QPushButton#removeBtn:hover {
                background-color: #7f1d1d;
                color: #fef2f2;
            }
            QMessageBox {
                background-color: #18181b;
            }
            QMessageBox QLabel {
                color: #e4e4e7;
            }
            QMessageBox QPushButton {
                min-width: 80px;
            }
            QInputDialog {
                background-color: #18181b;
            }
            QInputDialog QLabel {
                color: #e4e4e7;
            }
            QInputDialog QLineEdit {
                background-color: #09090b;
                color: #e4e4e7;
                border: 1px solid #3f3f46;
                border-radius: 4px;
                padding: 4px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        # Title
        header = QLabel(self.tr("Telegram Accounts"))
        header.setStyleSheet("font-size: 18px; font-weight: bold; color: #ffffff;")
        layout.addWidget(header)

        desc = QLabel(
            self.tr(
                "Add and authorize new accounts, choose the primary account, "
                "change the target channel, configure a proxy, or disable an account."
            )
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #a1a1aa; font-size: 13px; line-height: 1.4;")
        layout.addWidget(desc)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(10)
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("ID"),
                self.tr("Label"),
                self.tr("Phone"),
                self.tr("Username"),
                self.tr("Channel"),
                self.tr("Primary"),
                self.tr("Active"),
                self.tr("Proxy"),
                self.tr("Proxy status"),
                self.tr("Account status"),
            ]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        # Account status stretches to fill the remaining width — it has the
        # longest text. Left-align it (header + cells): centering short text
        # in a wide stretched column leaves it stranded in empty space, far
        # from the "Proxy status" column it reads alongside.
        header.setSectionResizeMode(self.COL_ACC_STATUS, QHeaderView.ResizeMode.Stretch)
        acc_status_header = self.table.horizontalHeaderItem(self.COL_ACC_STATUS)
        if acc_status_header is not None:
            acc_status_header.setTextAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

        # Buttons — two rows so translated (longer) labels never get clipped
        # by overflowing a single row's width (hit with ru/uk in practice).
        btn_layout = QVBoxLayout()
        btn_layout.setSpacing(8)
        btn_row1 = QHBoxLayout()
        btn_row1.setSpacing(8)
        btn_row2 = QHBoxLayout()
        btn_row2.setSpacing(8)

        self.add_btn = QPushButton(self.tr("+ Add account"))
        self.add_btn.setObjectName("primaryBtn")
        self.add_btn.clicked.connect(self._on_add)
        self.add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_row1.addWidget(self.add_btn)

        self.primary_btn = QPushButton(self.tr("Set as primary"))
        self.primary_btn.clicked.connect(self._on_set_primary)
        self.primary_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_row1.addWidget(self.primary_btn)

        self.toggle_btn = QPushButton(self.tr("On / Off"))
        self.toggle_btn.clicked.connect(self._on_toggle)
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_row1.addWidget(self.toggle_btn)

        self.channel_btn = QPushButton(self.tr("Change channel"))
        self.channel_btn.clicked.connect(self._on_change_channel)
        self.channel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_row1.addWidget(self.channel_btn)

        self.favorites_btn = QPushButton(self.tr("Use Saved Messages"))
        self.favorites_btn.setToolTip(
            self.tr(
                "Switch this account to store new uploads in Saved Messages "
                "(Favorites) instead of a channel"
            )
        )
        self.favorites_btn.clicked.connect(self._on_use_saved_messages)
        self.favorites_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_row1.addWidget(self.favorites_btn)
        btn_row1.addStretch()

        self.proxy_btn = QPushButton(self.tr("Proxy"))
        self.proxy_btn.clicked.connect(self._on_change_proxy)
        self.proxy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_row2.addWidget(self.proxy_btn)

        self.remove_btn = QPushButton(self.tr("Remove"))
        self.remove_btn.setObjectName("removeBtn")
        self.remove_btn.clicked.connect(self._on_remove)
        self.remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_row2.addWidget(self.remove_btn)

        self.check_btn = QPushButton(self.tr("Check liveness"))
        self.check_btn.clicked.connect(self._start_probe)
        self.check_btn.setToolTip(
            self.tr(
                "Check which proxy and which account are actually alive "
                "(a real connection to Telegram)"
            )
        )
        self.check_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_row2.addWidget(self.check_btn)

        btn_row2.addStretch()

        self.close_btn = QPushButton(self.tr("Close"))
        self.close_btn.clicked.connect(self.accept)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_row2.addWidget(self.close_btn)

        btn_layout.addLayout(btn_row1)
        btn_layout.addLayout(btn_row2)
        layout.addLayout(btn_layout)

        self._load_accounts()

    def _load_accounts(self):
        accounts = self.repo.list_accounts()
        self._accounts = accounts
        self.table.setRowCount(len(accounts))

        for row, acc in enumerate(accounts):
            items = [
                QTableWidgetItem(str(acc.id)),
                QTableWidgetItem(acc.label),
                QTableWidgetItem(acc.phone_masked or "—"),
                QTableWidgetItem(acc.username or "—"),
                QTableWidgetItem(""),  # Channel — replaced by the button below
                QTableWidgetItem(self.tr("Yes") if acc.is_primary else self.tr("No")),
                QTableWidgetItem(self.tr("Yes") if acc.is_active else self.tr("No")),
                QTableWidgetItem(self._short_proxy(acc.proxy)),
                QTableWidgetItem("—"),  # Proxy status (filled in by the check)
                QTableWidgetItem("—"),  # Account status
            ]
            for col, item in enumerate(items):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, col, item)

            # Channel — a "copy link" button instead of a long URL.
            self.table.setCellWidget(
                row, self.COL_CHANNEL, self._make_channel_button(acc.chat_target)
            )
            # Full proxy string goes in the tooltip (login/password are hidden
            # in the table).
            if acc.proxy:
                self.table.item(row, self.COL_PROXY).setToolTip(acc.proxy)

            # Highlight the primary row
            if acc.is_primary:
                from PySide6.QtGui import QColor

                for col in range(self.table.columnCount()):
                    cell = self.table.item(row, col)
                    if (
                        cell is not None
                    ):  # the "Channel" column has a widget, not an item
                        cell.setBackground(QColor("#2e1065"))

        # Automatically check liveness on open/refresh.
        self._start_probe()

    def _start_probe(self) -> None:
        """Start the background liveness check for proxies and accounts."""
        if not self._accounts:
            return
        if self._probe is not None and self._probe.isRunning():
            return  # a check is already running

        for row in range(self.table.rowCount()):
            self._set_status_cell(row, self.COL_PROXY_STATUS, "…")
            self._set_status_cell(row, self.COL_ACC_STATUS, self.tr("checking…"))
        self.check_btn.setEnabled(False)
        self.check_btn.setText(self.tr("Checking…"))

        probe = _StatusProbe(self._accounts)
        probe.row_status.connect(self._on_row_status)
        # Order matters: our slot runs first (resets self._probe), then
        # deleteLater destroys the C++ object. Otherwise done() would touch a
        # deleted object.
        probe.finished.connect(self._on_probe_done)
        probe.finished.connect(probe.deleteLater)
        self._probe = probe
        _ACTIVE_PROBES.add(probe)
        probe.start()

    def _on_row_status(
        self, account_id: int, proxy_status: str, acc_status: str
    ) -> None:
        row = self._row_for_account(account_id)
        if row is None:
            return  # account was deleted / list changed while the check was running
        self._set_status_cell(row, self.COL_PROXY_STATUS, proxy_status)
        self._set_status_cell(row, self.COL_ACC_STATUS, acc_status)

    def _row_for_account(self, account_id: int) -> int | None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, self.COL_ID)
            if item is not None and item.text() == str(account_id):
                return row
        return None

    def _on_probe_done(self) -> None:
        _ACTIVE_PROBES.discard(self._probe)
        # Reset the reference before deleteLater destroys the C++ object, so
        # that done()/a repeated _start_probe don't touch a deleted thread.
        self._probe = None
        self.check_btn.setEnabled(True)
        self.check_btn.setText(self.tr("Check liveness"))

    def _set_status_cell(self, row: int, col: int, text: str) -> None:
        from PySide6.QtGui import QColor

        item = QTableWidgetItem(text)
        if col == self.COL_ACC_STATUS:
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
        else:
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if text.startswith("✅"):
            item.setForeground(QColor("#4ade80"))  # green — alive
        elif text.startswith(("❌", "⚠")):
            item.setForeground(QColor("#fca5a5"))  # red — problem
        elif text.startswith("🔒"):
            item.setForeground(QColor("#fcd34d"))  # yellow — busy/alive
        # Preserve the primary-account row highlight when replacing the cell.
        primary_item = self.table.item(row, self.COL_PRIMARY)
        if primary_item is not None and primary_item.text() == self.tr("Yes"):
            item.setBackground(QColor("#2e1065"))
        self.table.setItem(row, col, item)

    def done(self, result: int) -> None:
        # Stop the background check when the window closes. Guard against the
        # case where the thread's C++ object is already deleted (deleteLater)
        # but the Python reference is still alive.
        probe = self._probe
        if probe is not None:
            try:
                from shiboken6 import isValid

                if isValid(probe) and probe.isRunning():
                    probe.requestInterruption()
            except Exception:
                pass
        super().done(result)

    @staticmethod
    def _short_proxy(raw: str) -> str:
        """Short proxy display: host:port only (no login/password)."""
        raw = str(raw or "").strip()
        if not raw:
            return QCoreApplication.translate("AccountsDialog", "No proxy")
        try:
            from app.core.utils import is_mtproxy, parse_mtproxy, parse_proxy

            if is_mtproxy(raw):
                host, port, _secret = parse_mtproxy(raw)
                return f"mtproxy {host}:{port}"
            host, port, *_ = parse_proxy(raw)
            return f"{host}:{port}"
        except Exception:
            return raw

    @staticmethod
    def _is_saved_messages(chat_target: str) -> bool:
        return str(chat_target or "").strip().lower() in ("me", "self")

    def _make_channel_button(self, chat_target: str) -> QPushButton:
        if self._is_saved_messages(chat_target):
            btn = QPushButton(self.tr("Saved Messages"))
            btn.setToolTip(self.tr("Files are stored in Saved Messages (Favorites)"))
        else:
            btn = QPushButton(self.tr("Link"))
            btn.setToolTip(chat_target or self.tr("Channel not set"))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setEnabled(bool(chat_target))
        btn.clicked.connect(lambda _=False, t=chat_target: self._copy_channel(t))
        return btn

    def _copy_channel(self, chat_target: str) -> None:
        from PySide6.QtWidgets import QApplication

        if not chat_target:
            return
        if self._is_saved_messages(chat_target):
            QMessageBox.information(
                self,
                self.tr("Saved Messages"),
                self.tr(
                    "This account stores files in Saved Messages (Favorites), "
                    "not a separate channel."
                ),
            )
            return
        QApplication.clipboard().setText(chat_target)
        QMessageBox.information(
            self,
            self.tr("Copied"),
            self.tr("Channel link copied:\n\n{0}").format(chat_target),
        )

    def _get_selected_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.warning(
                self, self.tr("Error"), self.tr("Select an account in the table")
            )
            return None
        item = self.table.item(row, 0)
        if item:
            try:
                return int(item.text())
            except ValueError:
                pass
        return None

    def _on_set_primary(self):
        acc_id = self._get_selected_id()
        if acc_id is None:
            return

        # Clear primary from all accounts
        for acc in self.repo.list_accounts():
            self.repo.update_account(acc.id, is_primary=0)

        self.repo.update_account(acc_id, is_primary=1)
        self._load_accounts()
        QMessageBox.information(
            self, self.tr("Done"), self.tr("The account is now primary.")
        )

    def _on_toggle(self):
        acc_id = self._get_selected_id()
        if acc_id is None:
            return

        acc = self.repo.get_account(acc_id)
        if acc:
            new_state = not acc.is_active
            self.repo.update_account(acc_id, is_active=1 if new_state else 0)
            self._load_accounts()
            state_text = self.tr("enabled") if new_state else self.tr("disabled")
            QMessageBox.information(
                self,
                self.tr("Done"),
                self.tr("Account '{0}' {1}").format(acc.label, state_text),
            )

    def _on_change_channel(self):
        acc_id = self._get_selected_id()
        if acc_id is None:
            return

        acc = self.repo.get_account(acc_id)
        if not acc:
            return

        channel, ok = QInputDialog.getText(
            self,
            self.tr("Change channel"),
            self.tr("Current channel: {0}\n\nNew channel:").format(acc.chat_target),
            text=acc.chat_target,
        )
        if ok and channel.strip():
            new_channel = channel.strip()
            # Guard: don't let a proxy accidentally get saved into the channel field.
            if self._looks_like_proxy(new_channel):
                QMessageBox.warning(
                    self,
                    self.tr("Error"),
                    self.tr(
                        "This looks like a proxy (host:port:user:pass), not a "
                        "channel link.\n"
                        "Channel: https://t.me/+xxxxx, @username or username."
                    ),
                )
                return
            self.repo.update_account(acc_id, chat_target=new_channel)
            self._load_accounts()
            QMessageBox.information(
                self,
                self.tr("Done"),
                self.tr("Channel updated: {0}").format(new_channel),
            )

    def _on_use_saved_messages(self):
        acc_id = self._get_selected_id()
        if acc_id is None:
            return

        acc = self.repo.get_account(acc_id)
        if not acc:
            return

        if self._is_saved_messages(acc.chat_target):
            QMessageBox.information(
                self,
                self.tr("Saved Messages"),
                self.tr("This account already stores files in Saved Messages."),
            )
            return

        from app.ui.dialogs import ConfirmDialog

        dialog = ConfirmDialog(
            title=self.tr("Switch to Saved Messages?"),
            message=self.tr(
                "Account '{0}' will now upload new files to Saved Messages "
                "(Favorites) instead of channel '{1}'.\n\n"
                "This only affects new uploads — existing files stay where "
                "they are."
            ).format(acc.label, acc.chat_target),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        self.repo.update_account(acc_id, chat_target="me")
        self._load_accounts()
        QMessageBox.information(
            self,
            self.tr("Done"),
            self.tr("Account '{0}' now stores files in Saved Messages.").format(
                acc.label
            ),
        )

    @staticmethod
    def _looks_like_proxy(value: str) -> bool:
        """True if the string parses as a proxy (socks/http/mtproto)."""
        from app.core.utils import is_mtproxy, parse_mtproxy, parse_proxy

        try:
            if is_mtproxy(value):
                parse_mtproxy(value)
            else:
                parse_proxy(value)
            return True
        except ValueError:
            return False

    def _on_change_proxy(self):
        acc_id = self._get_selected_id()
        if acc_id is None:
            return

        acc = self.repo.get_account(acc_id)
        if not acc:
            return

        proxy, ok = QInputDialog.getText(
            self,
            self.tr("Change proxy"),
            self.tr(
                "Current proxy: {0}\n\n"
                "Formats: IP:PORT:USER:PASS, socks5://…, http://…,\n"
                "MTProto: mtproto://HOST:PORT:SECRET or tg://proxy?…\n"
                "(empty = no proxy):"
            ).format(acc.proxy or self.tr("No proxy")),
            text=acc.proxy,
        )
        if not ok:
            return

        backup, ok_backup = QInputDialog.getText(
            self,
            self.tr("Backup proxy"),
            self.tr(
                "Current backup: {0}\n\n"
                "Used if the primary is unavailable (then falls back to direct).\n"
                "Formats: IP:PORT:USER:PASS, socks5://…, http://…, mtproto://…\n"
                "(empty = no backup):"
            ).format(acc.proxy_backup or self.tr("No backup")),
            text=acc.proxy_backup,
        )
        self.repo.update_account(acc_id, proxy=proxy.strip())
        if ok_backup:
            self.repo.update_account(acc_id, proxy_backup=backup.strip())
        self._load_accounts()
        proxy_text = proxy.strip() if proxy.strip() else self.tr("No proxy")
        backup_text = (
            backup.strip() if ok_backup and backup.strip() else acc.proxy_backup
        ) or self.tr("No backup")
        QMessageBox.information(
            self,
            self.tr("Done"),
            self.tr("Proxy updated: {0}\nBackup: {1}").format(proxy_text, backup_text),
        )

    def _on_remove(self):
        acc_id = self._get_selected_id()
        if acc_id is None:
            return

        acc = self.repo.get_account(acc_id)
        if not acc:
            return

        from app.ui.dialogs import ConfirmDialog

        dialog = ConfirmDialog(
            title=self.tr("Delete account?"),
            message=self.tr(
                "Delete account '{0}'?\nThe session will be deleted."
            ).format(acc.label),
            parent=self,
            is_destructive=True,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        # Delete the session
        session_path = Path(acc.session_path)
        if session_path.exists():
            session_path.unlink(missing_ok=True)

        self.repo.delete_account(acc_id)
        self._load_accounts()
        QMessageBox.information(
            self, self.tr("Done"), self.tr("Account '{0}' deleted").format(acc.label)
        )

    def _on_add(self):
        from app.ui.dialogs._add_account import AddAccountDialog

        dlg = AddAccountDialog(
            self.repo,
            default_api_id=self._default_api_id,
            default_api_hash=self._default_api_hash,
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._load_accounts()
