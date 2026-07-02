"""
Простое окно управления Telegram аккаунтами.
Только таблица + кнопки управления. Авторизация через терминал.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
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

# Держим живые потоки-проберы, чтобы их не собрал GC, если диалог закроют
# до завершения проверки (поток не запарентен к диалогу).
_ACTIVE_PROBES: set[QThread] = set()


class _StatusProbe(QThread):
    """Фоновая проверка живости прокси и аккаунтов.

    Для каждого аккаунта:
      - прокси: реальный TCP-коннект через python_socks до Telegram DC
        (resolve_working_proxy) — сессию не трогает;
      - аккаунт: реальный connect + is_user_authorized через Telethon.
    Результат по каждой строке отдаётся сигналом row_status в GUI-поток.
    """

    # Шлём по account_id (а не по индексу строки): если во время проверки
    # список аккаунтов изменится, результат всё равно ляжет на нужную строку.
    row_status = Signal(int, str, str)  # account_id, proxy_status, account_status
    # Завершение сигнализируется встроенным QThread.finished — отдельный сигнал
    # не нужен (и имя "done" путалось бы с QDialog.done).

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
                    acc_status = "❌ ошибка"
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
        # Основной аккаунт всегда подключается напрямую.
        if acc.is_primary or not chain:
            return ("— прямое", None)
        proxy, _label, tier = select_working_proxy_from_chain(chain)
        if proxy is None:
            return ("❌ мёртв → direct", None)
        if tier > 0:
            return ("✅ резервный", proxy)
        return ("✅ работает", proxy)

    @staticmethod
    def _session_disk_path(session_path: str) -> str:
        """Реальный путь .session-файла (Telethon добавляет расширение сам)."""
        p = str(session_path or "")
        return p if p.endswith(".session") else p + ".session"

    @classmethod
    async def _probe_account(
        cls, acc: TelegramAccount, proxy_tuple: tuple | None
    ) -> str:
        from telethon import TelegramClient

        # Проверяем на КОПИИ сессии: auth_key тот же, но запись идёт во временный
        # файл — реальную сессию работающего приложения не трогаем (нет гонок/локов).
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
            return "⚠️ нет сессии"

        client = TelegramClient(
            tmp_session,
            acc.tg_api_id,
            acc.tg_api_hash,
            proxy=proxy_tuple if proxy_tuple else None,
        )
        try:
            await asyncio.wait_for(client.connect(), timeout=cls._CONNECT_TIMEOUT)
            if not await client.is_user_authorized():
                return "⚠️ не авторизован"
            me = await client.get_me()
            uname = getattr(me, "username", None)
            premium = " ⭐" if getattr(me, "premium", False) else ""
            tail = f" @{uname}" if uname else ""
            return f"✅ онлайн{premium}{tail}"
        except asyncio.TimeoutError:
            return "❌ таймаут"
        except Exception as exc:  # noqa: BLE001
            logger.debug("Account '%s' unreachable: %s", acc.label, exc)
            return "❌ недоступен"
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            shutil.rmtree(tmp_dir, ignore_errors=True)


class AccountsDialog(QDialog):
    """Окно управления аккаунтами."""

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

    def __init__(self, repo: DbRepo, parent=None):
        super().__init__(parent)
        self.repo = repo
        self._accounts: list[TelegramAccount] = []
        self._probe: _StatusProbe | None = None
        self.setWindowTitle("Telegram Аккаунты")
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

        # Заголовок
        header = QLabel("Telegram Аккаунты")
        header.setStyleSheet("font-size: 18px; font-weight: bold; color: #ffffff;")
        layout.addWidget(header)

        desc = QLabel(
            "Для добавления нового аккаунта запустите команду: python scripts/manage_accounts.py\n"
            "Здесь можно выбрать основной аккаунт, изменить целевой канал, настроить прокси или отключить аккаунт."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #a1a1aa; font-size: 13px; line-height: 1.4;")
        layout.addWidget(desc)

        # Таблица
        self.table = QTableWidget()
        self.table.setColumnCount(10)
        self.table.setHorizontalHeaderLabels(
            [
                "ID",
                "Метка",
                "Телефон",
                "Username",
                "Канал",
                "Основной",
                "Активен",
                "Прокси",
                "Статус прокси",
                "Статус акк",
            ]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        # Статус аккаунта тянется на остаток ширины — там самый длинный текст.
        header.setSectionResizeMode(self.COL_ACC_STATUS, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

        # Кнопки
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        self.primary_btn = QPushButton("Сделать основным")
        self.primary_btn.setObjectName("primaryBtn")
        self.primary_btn.clicked.connect(self._on_set_primary)
        self.primary_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_layout.addWidget(self.primary_btn)

        self.toggle_btn = QPushButton("Вкл / Выкл")
        self.toggle_btn.clicked.connect(self._on_toggle)
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_layout.addWidget(self.toggle_btn)

        self.channel_btn = QPushButton("Изменить канал")
        self.channel_btn.clicked.connect(self._on_change_channel)
        self.channel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_layout.addWidget(self.channel_btn)

        self.proxy_btn = QPushButton("Прокси")
        self.proxy_btn.clicked.connect(self._on_change_proxy)
        self.proxy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_layout.addWidget(self.proxy_btn)

        self.remove_btn = QPushButton("Удалить")
        self.remove_btn.setObjectName("removeBtn")
        self.remove_btn.clicked.connect(self._on_remove)
        self.remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_layout.addWidget(self.remove_btn)

        self.check_btn = QPushButton("Проверить живость")
        self.check_btn.clicked.connect(self._start_probe)
        self.check_btn.setToolTip(
            "Проверить, какой прокси и какой аккаунт реально живые "
            "(реальный коннект к Telegram)"
        )
        self.check_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_layout.addWidget(self.check_btn)

        btn_layout.addStretch()

        self.copy_cmd_btn = QPushButton("Копировать команду добавления")
        self.copy_cmd_btn.clicked.connect(self._on_copy_command)
        self.copy_cmd_btn.setToolTip(
            "Скопировать в буфер обмена команду для добавления аккаунта"
        )
        self.copy_cmd_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_layout.addWidget(self.copy_cmd_btn)

        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.accept)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_layout.addWidget(self.close_btn)

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
                QTableWidgetItem(""),  # Канал — заменяется кнопкой ниже
                QTableWidgetItem("Да" if acc.is_primary else "Нет"),
                QTableWidgetItem("Да" if acc.is_active else "Нет"),
                QTableWidgetItem(self._short_proxy(acc.proxy)),
                QTableWidgetItem("—"),  # Статус прокси (заполняется проверкой)
                QTableWidgetItem("—"),  # Статус акк
            ]
            for col, item in enumerate(items):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, col, item)

            # Канал — кнопка «скопировать ссылку» вместо длинного URL.
            self.table.setCellWidget(
                row, self.COL_CHANNEL, self._make_channel_button(acc.chat_target)
            )
            # Полная строка прокси — в подсказке (логин/пароль скрыты в таблице).
            if acc.proxy:
                self.table.item(row, self.COL_PROXY).setToolTip(acc.proxy)

            # Подсветка основного
            if acc.is_primary:
                from PySide6.QtGui import QColor

                for col in range(self.table.columnCount()):
                    cell = self.table.item(row, col)
                    if cell is not None:  # у колонки «Канал» виджет, а не item
                        cell.setBackground(QColor("#2e1065"))

        # Автоматически проверяем живость при открытии/обновлении.
        self._start_probe()

    def _start_probe(self) -> None:
        """Запустить фоновую проверку живости прокси и аккаунтов."""
        if not self._accounts:
            return
        if self._probe is not None and self._probe.isRunning():
            return  # проверка уже идёт

        for row in range(self.table.rowCount()):
            self._set_status_cell(row, self.COL_PROXY_STATUS, "…")
            self._set_status_cell(row, self.COL_ACC_STATUS, "проверка…")
        self.check_btn.setEnabled(False)
        self.check_btn.setText("Проверяю…")

        probe = _StatusProbe(self._accounts)
        probe.row_status.connect(self._on_row_status)
        # Порядок важен: сначала наш слот (сбрасывает self._probe), потом
        # deleteLater удаляет C++-объект. Иначе done() обратится к удалённому.
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
            return  # аккаунт удалён/список изменился, пока шла проверка
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
        # Сбрасываем ссылку до того, как deleteLater уничтожит C++-объект,
        # чтобы done()/повторный _start_probe не трогали удалённый поток.
        self._probe = None
        self.check_btn.setEnabled(True)
        self.check_btn.setText("Проверить живость")

    def _set_status_cell(self, row: int, col: int, text: str) -> None:
        from PySide6.QtGui import QColor

        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if text.startswith("✅"):
            item.setForeground(QColor("#4ade80"))  # зелёный — живой
        elif text.startswith(("❌", "⚠")):
            item.setForeground(QColor("#fca5a5"))  # красный — проблема
        elif text.startswith("🔒"):
            item.setForeground(QColor("#fcd34d"))  # жёлтый — занят/живой
        # Сохраняем подсветку строки основного аккаунта при замене ячейки.
        primary_item = self.table.item(row, self.COL_PRIMARY)
        if primary_item is not None and primary_item.text() == "Да":
            item.setBackground(QColor("#2e1065"))
        self.table.setItem(row, col, item)

    def done(self, result: int) -> None:
        # Останавливаем фоновую проверку при закрытии окна. Защищаемся от случая,
        # когда C++-объект потока уже удалён (deleteLater), а Python-ссылка ещё жива.
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
        """Короткое отображение прокси: только host:port (без логина/пароля)."""
        raw = str(raw or "").strip()
        if not raw:
            return "Без прокси"
        try:
            from app.core.utils import is_mtproxy, parse_mtproxy, parse_proxy

            if is_mtproxy(raw):
                host, port, _secret = parse_mtproxy(raw)
                return f"mtproxy {host}:{port}"
            host, port, *_ = parse_proxy(raw)
            return f"{host}:{port}"
        except Exception:
            return raw

    def _make_channel_button(self, chat_target: str) -> QPushButton:
        btn = QPushButton("📋 Ссылка")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(chat_target or "Канал не задан")
        btn.setEnabled(bool(chat_target))
        btn.clicked.connect(lambda _=False, t=chat_target: self._copy_channel(t))
        return btn

    def _copy_channel(self, chat_target: str) -> None:
        from PySide6.QtWidgets import QApplication

        if not chat_target:
            return
        QApplication.clipboard().setText(chat_target)
        QMessageBox.information(
            self, "Скопировано", f"Ссылка на канал скопирована:\n\n{chat_target}"
        )

    def _get_selected_id(self) -> int | None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "Ошибка", "Выберите аккаунт в таблице")
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

        # Снять primary со всех
        for acc in self.repo.list_accounts():
            self.repo.update_account(acc.id, is_primary=0)

        self.repo.update_account(acc_id, is_primary=1)
        self._load_accounts()
        QMessageBox.information(self, "Готово", "Аккаунт теперь основной! ✨")

    def _on_toggle(self):
        acc_id = self._get_selected_id()
        if acc_id is None:
            return

        acc = self.repo.get_account(acc_id)
        if acc:
            new_state = not acc.is_active
            self.repo.update_account(acc_id, is_active=1 if new_state else 0)
            self._load_accounts()
            state_text = "включён" if new_state else "выключен"
            QMessageBox.information(
                self, "Готово", f"Аккаунт '{acc.label}' {state_text}"
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
            "Изменить канал",
            f"Текущий канал: {acc.chat_target}\n\nНовый канал:",
            text=acc.chat_target,
        )
        if ok and channel.strip():
            new_channel = channel.strip()
            # Защита: не дать случайно сохранить прокси в поле канала.
            if self._looks_like_proxy(new_channel):
                QMessageBox.warning(
                    self,
                    "Ошибка",
                    "Это похоже на прокси (host:port:user:pass), а не на ссылку на канал.\n"
                    "Канал: https://t.me/+xxxxx, @username или username.",
                )
                return
            self.repo.update_account(acc_id, chat_target=new_channel)
            self._load_accounts()
            QMessageBox.information(self, "Готово", f"Канал обновлён: {new_channel}")

    @staticmethod
    def _looks_like_proxy(value: str) -> bool:
        """True, если строка парсится как прокси (socks/http/mtproto)."""
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
            "Изменить прокси",
            f"Текущий прокси: {acc.proxy or 'Без прокси'}\n\n"
            "Форматы: IP:PORT:USER:PASS, socks5://…, http://…,\n"
            "MTProto: mtproto://HOST:PORT:SECRET или tg://proxy?…\n"
            "(пусто = без прокси):",
            text=acc.proxy,
        )
        if not ok:
            return

        backup, ok_backup = QInputDialog.getText(
            self,
            "Резервный прокси",
            f"Текущий резервный: {acc.proxy_backup or 'Без резервного'}\n\n"
            "Используется, если основной недоступен (затем — напрямую).\n"
            "Форматы: IP:PORT:USER:PASS, socks5://…, http://…, mtproto://…\n"
            "(пусто = без резервного):",
            text=acc.proxy_backup,
        )
        self.repo.update_account(acc_id, proxy=proxy.strip())
        if ok_backup:
            self.repo.update_account(acc_id, proxy_backup=backup.strip())
        self._load_accounts()
        proxy_text = proxy.strip() if proxy.strip() else "Без прокси"
        backup_text = (
            backup.strip() if ok_backup and backup.strip() else acc.proxy_backup
        ) or "Без резервного"
        QMessageBox.information(
            self,
            "Готово",
            f"Прокси обновлён: {proxy_text}\nРезервный: {backup_text}",
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
            title="Удалить аккаунт?",
            message=f"Удалить аккаунт '{acc.label}'?\nСессия будет удалена.",
            parent=self,
            is_destructive=True,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        # Удалить session
        session_path = Path(acc.session_path)
        if session_path.exists():
            session_path.unlink(missing_ok=True)

        self.repo.delete_account(acc_id)
        self._load_accounts()
        QMessageBox.information(self, "Готово", f"Аккаунт '{acc.label}' удалён")

    def _on_copy_command(self):
        from PySide6.QtWidgets import QApplication

        cmd = "python scripts/manage_accounts.py"
        clipboard = QApplication.clipboard()
        clipboard.setText(cmd)
        QMessageBox.information(self, "Скопировано", f"Команда скопирована:\n\n{cmd}")
