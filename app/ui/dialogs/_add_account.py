"""Add-account wizard: authorize a new Telegram account entirely from the GUI.

Replaces the old console flow (scripts/manage_accounts.py): the user enters
label / phone / API credentials / channel / optional proxy, the dialog sends
the login code, asks for the code (and the 2FA password if needed) and stores
the authorized account in the DB.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import re
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.core.types import TelegramAccount
from app.core.utils import build_telethon_proxy, ensure_dir
from app.db.repo import DbRepo

logger = logging.getLogger(__name__)

_SESSIONS_DIR = Path("var/data/account_sessions")
_PHONE_RE = re.compile(r"^\+?[1-9]\d{6,14}$")
_CHANNEL_RE = re.compile(
    r"^(https://t\.me/(\+|joinchat/)?[\w\-]+|@[\w\d_]+|-100\d+|[\w\d_]+)$"
)

# Keep live auth threads referenced so GC doesn't collect them if the dialog
# is destroyed before the worker finishes.
_ACTIVE_AUTH: set[QThread] = set()


class _AuthWorker(QThread):
    """Runs the Telethon authorization flow off the GUI thread.

    User input (code / 2FA password) is requested via signals; the worker
    blocks on an internal queue until the GUI submits a value (None = cancel).
    """

    code_requested = Signal()
    password_requested = Signal()
    auth_ok = Signal(dict)
    auth_failed = Signal(str)  # empty string = silent cancel

    def __init__(
        self,
        phone: str,
        api_id: int,
        api_hash: str,
        session_path: str,
        proxy: str = "",
    ) -> None:
        super().__init__()
        self._phone = phone
        self._api_id = api_id
        self._api_hash = api_hash
        self._session_path = session_path
        self._proxy = proxy
        self._inputs: queue.Queue[str | None] = queue.Queue()

    def submit(self, value: str | None) -> None:
        """Deliver the code/password typed by the user (None cancels)."""
        self._inputs.put(value)

    def cancel(self) -> None:
        self.requestInterruption()
        self._inputs.put(None)

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._authorize())
        except Exception as exc:  # noqa: BLE001
            logger.exception("Account authorization failed")
            self.auth_failed.emit(str(exc))
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _authorize(self) -> None:
        from telethon import TelegramClient
        from telethon.errors import FloodWaitError, SessionPasswordNeededError

        proxy_obj = build_telethon_proxy(self._proxy) if self._proxy else None
        client = TelegramClient(
            self._session_path, self._api_id, self._api_hash, proxy=proxy_obj
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                try:
                    await client.send_code_request(self._phone)
                except FloodWaitError as exc:
                    self.auth_failed.emit(
                        self.tr(
                            "Telegram rate limit (FloodWait): retry in {0} s"
                        ).format(exc.seconds)
                    )
                    return

                self.code_requested.emit()
                code = self._inputs.get()
                if self.isInterruptionRequested() or not code:
                    self.auth_failed.emit("")
                    return

                try:
                    await client.sign_in(self._phone, code)
                except SessionPasswordNeededError:
                    self.password_requested.emit()
                    password = self._inputs.get()
                    if self.isInterruptionRequested() or not password:
                        self.auth_failed.emit("")
                        return
                    await client.sign_in(password=password)

                if not await client.is_user_authorized():
                    self.auth_failed.emit(self.tr("Authorization did not complete"))
                    return

            me = await client.get_me()
            self.auth_ok.emit(
                {
                    "id": int(getattr(me, "id", 0) or 0),
                    "username": str(getattr(me, "username", "") or ""),
                    "phone": str(getattr(me, "phone", "") or self._phone),
                    "premium": bool(getattr(me, "premium", False)),
                }
            )
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


class AddAccountDialog(QDialog):
    """Dialog that adds and authorizes a new upload account."""

    def __init__(
        self,
        repo: DbRepo,
        default_api_id: int = 0,
        default_api_hash: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.repo = repo
        self._worker: _AuthWorker | None = None
        self._pending: dict | None = None  # form values captured at start

        self.setWindowTitle(self.tr("Add Telegram Account"))
        self.setMinimumWidth(520)

        self.setStyleSheet("""
            QDialog { background-color: #09090b; }
            QLabel { color: #e4e4e7; }
            QLineEdit {
                background-color: #18181b;
                color: #e4e4e7;
                border: 1px solid #3f3f46;
                border-radius: 6px;
                padding: 6px 8px;
            }
            QLineEdit:disabled { color: #71717a; }
            QCheckBox { color: #e4e4e7; spacing: 8px; }
            QCheckBox:disabled { color: #71717a; }
            QPushButton {
                background-color: #27272a;
                color: #e4e4e7;
                border: 1px solid #3f3f46;
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: 500;
            }
            QPushButton:hover { background-color: #3f3f46; color: #ffffff; }
            QPushButton#startBtn {
                background-color: #6d28d9;
                border-color: #5b21b6;
                color: #ffffff;
            }
            QPushButton#startBtn:hover { background-color: #7c3aed; }
            QPushButton#startBtn:disabled {
                background-color: #3f3f46;
                border-color: #3f3f46;
                color: #a1a1aa;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel(self.tr("Add Telegram Account"))
        title.setStyleSheet("font-size: 17px; font-weight: bold; color: #ffffff;")
        layout.addWidget(title)

        hint = QLabel(
            self.tr(
                "The account is authorized right here: a login code will be "
                "sent to the phone number via Telegram."
            )
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #a1a1aa; font-size: 12px;")
        layout.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(8)

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText(self.tr("e.g. Account 2"))
        form.addRow(self.tr("Label:"), self.label_edit)

        self.phone_edit = QLineEdit()
        self.phone_edit.setPlaceholderText("+79991234567")
        form.addRow(self.tr("Phone:"), self.phone_edit)

        self.api_id_edit = QLineEdit()
        if default_api_id:
            self.api_id_edit.setText(str(default_api_id))
        self.api_id_edit.setPlaceholderText(self.tr("from my.telegram.org"))
        form.addRow(self.tr("API ID:"), self.api_id_edit)

        self.api_hash_edit = QLineEdit()
        if default_api_hash:
            self.api_hash_edit.setText(default_api_hash)
        self.api_hash_edit.setPlaceholderText(self.tr("from my.telegram.org"))
        form.addRow(self.tr("API Hash:"), self.api_hash_edit)

        self.channel_edit = QLineEdit()
        self.channel_edit.setPlaceholderText("https://t.me/+xxxxx / @username")
        form.addRow(self.tr("Channel:"), self.channel_edit)

        self.saved_messages_check = QCheckBox(
            self.tr("Use Saved Messages (Favorites) instead of a channel")
        )
        self.saved_messages_check.setToolTip(
            self.tr(
                "Store uploads in this account's own Saved Messages chat — "
                "no channel needed"
            )
        )
        self.saved_messages_check.toggled.connect(self._on_saved_messages_toggled)
        form.addRow("", self.saved_messages_check)

        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText(
            self.tr("optional: host:port:user:pass, socks5://…, http://…")
        )
        form.addRow(self.tr("Proxy:"), self.proxy_edit)

        layout.addLayout(form)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #a78bfa; font-size: 12px;")
        layout.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self.start_btn = QPushButton(self.tr("Send code and sign in"))
        self.start_btn.setObjectName("startBtn")
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.clicked.connect(self._on_start)
        btn_row.addWidget(self.start_btn)

        self.cancel_btn = QPushButton(self.tr("Cancel"))
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)

        layout.addLayout(btn_row)

    def _on_saved_messages_toggled(self, checked: bool) -> None:
        self.channel_edit.setEnabled(not checked)
        if checked:
            self.channel_edit.setPlaceholderText(
                self.tr("Not needed — using Saved Messages")
            )
        else:
            self.channel_edit.setPlaceholderText("https://t.me/+xxxxx / @username")

    # ----- form validation -------------------------------------------------

    def _validated_form(self) -> dict | None:
        label = self.label_edit.text().strip()
        if not label:
            self._warn(self.tr("Label is required"))
            return None

        phone = self.phone_edit.text().strip().replace(" ", "").replace("-", "")
        if not _PHONE_RE.match(phone):
            self._warn(self.tr("Invalid phone number format (e.g. +79991234567)"))
            return None

        try:
            api_id = int(self.api_id_edit.text().strip())
            if api_id <= 0:
                raise ValueError
        except ValueError:
            self._warn(self.tr("API ID must be a positive number"))
            return None

        api_hash = self.api_hash_edit.text().strip()
        if not api_hash:
            self._warn(self.tr("API Hash is required"))
            return None

        use_saved_messages = self.saved_messages_check.isChecked()
        if use_saved_messages:
            channel = "me"
        else:
            channel = self.channel_edit.text().strip()
            if not _CHANNEL_RE.match(channel):
                self._warn(
                    self.tr(
                        "Invalid channel format. Use https://t.me/+xxxxx, "
                        "@username or a -100… id"
                    )
                )
                return None

        proxy = self.proxy_edit.text().strip()
        if proxy:
            from app.core.utils import is_mtproxy, parse_mtproxy, parse_proxy

            try:
                if is_mtproxy(proxy):
                    parse_mtproxy(proxy)
                else:
                    parse_proxy(proxy)
            except ValueError as exc:
                self._warn(self.tr("Invalid proxy format: {0}").format(exc))
                return None

        phone_digits = re.sub(r"[^0-9]", "", phone)
        # phone_masked stores the number without its last 4 digits, so compare
        # against the same prefix.
        for acc in self.repo.list_accounts():
            acc_digits = re.sub(r"[^0-9]", "", acc.phone_masked or "")
            if acc_digits and phone_digits.startswith(acc_digits):
                self._warn(
                    self.tr("An account with this phone already exists: '{0}'").format(
                        acc.label
                    )
                )
                return None

        return {
            "label": label,
            "phone": phone,
            "phone_digits": phone_digits,
            "api_id": api_id,
            "api_hash": api_hash,
            "channel": channel,
            "proxy": proxy,
        }

    def _warn(self, text: str) -> None:
        QMessageBox.warning(self, self.tr("Add account"), text)

    # ----- authorization flow ----------------------------------------------

    def _on_start(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        form = self._validated_form()
        if form is None:
            return

        ensure_dir(_SESSIONS_DIR)
        session_path = str(_SESSIONS_DIR / f"acc_{form['phone_digits']}.session")
        form["session_path"] = session_path
        self._pending = form

        self._set_form_enabled(False)
        self.status_label.setText(self.tr("Connecting to Telegram…"))

        worker = _AuthWorker(
            phone=form["phone"],
            api_id=form["api_id"],
            api_hash=form["api_hash"],
            session_path=session_path,
            proxy=form["proxy"],
        )
        worker.code_requested.connect(self._on_code_requested)
        worker.password_requested.connect(self._on_password_requested)
        worker.auth_ok.connect(self._on_auth_ok)
        worker.auth_failed.connect(self._on_auth_failed)
        worker.finished.connect(lambda: _ACTIVE_AUTH.discard(worker))
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        _ACTIVE_AUTH.add(worker)
        worker.start()

    def _set_form_enabled(self, enabled: bool) -> None:
        for widget in (
            self.label_edit,
            self.phone_edit,
            self.api_id_edit,
            self.api_hash_edit,
            self.channel_edit,
            self.saved_messages_check,
            self.proxy_edit,
            self.start_btn,
        ):
            widget.setEnabled(enabled)
        if enabled:
            # Re-applying the checkbox state: the channel field must stay
            # disabled if "Saved Messages" is checked, not just blanket-enabled.
            self.channel_edit.setEnabled(not self.saved_messages_check.isChecked())

    def _on_code_requested(self) -> None:
        self.status_label.setText(self.tr("Code sent — check your Telegram app."))
        code, ok = QInputDialog.getText(
            self,
            self.tr("Confirmation code"),
            self.tr("Enter the code from Telegram:"),
        )
        worker = self._worker
        if worker is not None:
            worker.submit(code.strip() if ok and code.strip() else None)

    def _on_password_requested(self) -> None:
        password, ok = QInputDialog.getText(
            self,
            self.tr("Two-factor authentication"),
            self.tr("Enter your 2FA password:"),
            QLineEdit.EchoMode.Password,
        )
        worker = self._worker
        if worker is not None:
            worker.submit(password if ok and password else None)

    def _on_auth_ok(self, info: dict) -> None:
        form = self._pending
        self._worker = None
        if form is None:
            return

        phone = str(info.get("phone") or form["phone"])
        is_primary = len(self.repo.list_accounts()) == 0
        account = TelegramAccount(
            id=0,
            label=form["label"],
            session_path=form["session_path"],
            tg_api_id=form["api_id"],
            tg_api_hash=form["api_hash"],
            chat_target=form["channel"],
            is_active=True,
            is_primary=is_primary,
            proxy=form["proxy"],
            phone_masked=phone[:-4] + "****" if len(phone) > 4 else "****",
            user_id=int(info.get("id") or 0),
            username=str(info.get("username") or ""),
            is_premium=bool(info.get("premium", False)),
        )
        self.repo.insert_account(account)

        who = account.username or f"ID {account.user_id}"
        extra = self.tr("\nThis is the primary account.") if is_primary else ""
        QMessageBox.information(
            self,
            self.tr("Account added"),
            self.tr("Account '{0}' authorized as {1}.{2}").format(
                account.label, who, extra
            ),
        )
        self.accept()

    def _on_auth_failed(self, message: str) -> None:
        self._worker = None
        self._set_form_enabled(True)
        self.status_label.setText("")
        if message:
            QMessageBox.warning(self, self.tr("Authorization error"), message)

    def reject(self) -> None:
        worker = self._worker
        if worker is not None:
            try:
                from shiboken6 import isValid

                if isValid(worker) and worker.isRunning():
                    worker.cancel()
            except Exception:
                pass
            self._worker = None
        super().reject()
