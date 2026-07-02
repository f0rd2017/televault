from __future__ import annotations


from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from app.ui.dialogs._style import _DIALOG_STYLESHEET


# ═══════════════════════════════════════════════════════
#  Управление аккаунтами Telegram
# ═══════════════════════════════════════════════════════

_ACCOUNT_TABLE_STYLESHEET = """
    QTableWidget {
        background: #0f1420;
        color: #f3f1ff;
        border: 1px solid #2f3850;
        border-radius: 8px;
        gridline-color: #1e2740;
    }
    QTableWidget::item {
        padding: 6px 8px;
    }
    QTableWidget::item:selected {
        background: #1c2638;
        color: #f3f1ff;
    }
    QHeaderView::section {
        background: #151d30;
        color: #9b8fc0;
        border: none;
        border-bottom: 1px solid #2f3850;
        padding: 6px 8px;
        font-weight: 600;
        font-size: 12px;
    }
    QTableWidget QScrollBar:vertical {
        background: #12192a;
        width: 10px;
        margin: 3px 2px 3px 2px;
        border: none;
        border-radius: 5px;
    }
    QTableWidget QScrollBar::handle:vertical {
        background: #4f5f80;
        min-height: 20px;
        border-radius: 5px;
    }
    QTableWidget QScrollBar::handle:vertical:hover {
        background: #6577a3;
    }
    QTableWidget QScrollBar::add-line:vertical,
    QTableWidget QScrollBar::sub-line:vertical,
    QTableWidget QScrollBar::add-page:vertical,
    QTableWidget QScrollBar::sub-page:vertical {
        background: transparent; border: none; height: 0;
    }
"""


# ═══════════════════════════════════════════════════════
#  Свойства файла — «что где лежит»
# ═══════════════════════════════════════════════════════

_OBJECT_STATE_LABELS = {
    "complete": ("Полный", "#7fd88f"),
    "incomplete": ("Не дозалит", "#ffcf66"),
    "offline": ("Аккаунт оффлайн", "#8fb8ff"),
    "damaged": ("Повреждён (часть потеряна)", "#ff7b72"),
}


def _format_size(num_bytes: int | None) -> str:
    if num_bytes is None:
        return "—"
    size = float(num_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024.0 or unit == "ТБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{num_bytes} Б"


class FilePropertiesDialog(QDialog):
    """Полные свойства объекта: какие части на каких аккаунтах/чатах лежат."""

    def __init__(
        self,
        *,
        entry,
        parts,
        connected_labels: dict[str, str],
        expected_sha256: str | None,
        note: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        from app.core.object_state import classify_object_state

        self.setWindowTitle(f"Свойства — {entry.orig_name}")
        self.setStyleSheet(_DIALOG_STYLESHEET + _ACCOUNT_TABLE_STYLESHEET)
        self.setMinimumWidth(620)

        connected_ids = set(connected_labels.keys())
        state = classify_object_state(
            list(parts),
            parts_total=int(entry.parts_total),
            connected_chat_ids=connected_ids,
        )
        state_text, state_color = _OBJECT_STATE_LABELS.get(state, (state, "#d8d0f5"))

        form = QFormLayout()
        form.addRow("Имя:", QLabel(str(entry.orig_name)))
        form.addRow("Папка:", QLabel(str(entry.folder_path)))
        form.addRow("Ключ:", QLabel(str(entry.file_key)))
        form.addRow("Размер:", QLabel(_format_size(entry.total_size)))
        have_parts = len({int(p.part_index) for p in parts})
        form.addRow("Части:", QLabel(f"{have_parts} / {int(entry.parts_total)}"))
        status_label = QLabel(state_text)
        status_label.setStyleSheet(f"color: {state_color}; font-weight: 700;")
        form.addRow("Состояние:", status_label)
        if expected_sha256:
            sha_label = QLabel(str(expected_sha256))
            sha_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            form.addRow("SHA-256:", sha_label)

        table = QTableWidget(len(parts), 5, self)
        table.setHorizontalHeaderLabels(
            ["Часть", "Размер", "Аккаунт / чат", "msg_id", "Состояние"]
        )
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for col in (0, 1, 3, 4):
            table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents
            )

        for row, part in enumerate(sorted(parts, key=lambda p: int(p.part_index))):
            chat_id = str(part.chat_id)
            account = connected_labels.get(chat_id, chat_id)
            if part.lost_ts:
                part_state, color = "потеряна", "#ff7b72"
            elif connected_ids and chat_id not in connected_ids:
                part_state, color = "оффлайн", "#8fb8ff"
            else:
                part_state, color = "ок", "#7fd88f"
            cells = [
                str(int(part.part_index) + 1),
                _format_size(part.file_size),
                account,
                str(int(part.msg_id)),
                part_state,
            ]
            for col, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if col == 4:
                    cell.setForeground(QColor(color))
                table.setItem(row, col, cell)

        # Минипометка — ручной комментарий пользователя.
        self._note_edit = QLineEdit(str(note or ""))
        self._note_edit.setPlaceholderText("Заметка к файлу (минипометка)…")
        note_form = QFormLayout()
        note_form.addRow("Заметка:", self._note_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(
            "Сохранить заметку"
        )
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(table)
        root.addLayout(note_form)
        root.addWidget(buttons)

    @property
    def note_value(self) -> str:
        return self._note_edit.text().strip()


class ShareLinkDialog(QDialog):
    """Создать публичную шар-ссылку на файл: опциональные пароль и срок,
    показ итогового URL с копированием. Запись создаётся напрямую в БД; чтобы
    ссылка реально работала, нужен включённый REST API (см. Настройки)."""

    _EXPIRY_OPTIONS = [
        ("Без срока", 0),
        ("1 час", 3600),
        ("1 день", 86400),
        ("7 дней", 7 * 86400),
        ("30 дней", 30 * 86400),
    ]

    def __init__(self, entry, repo, config, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Поделиться ссылкой")
        self._entry = entry
        self._repo = repo
        self._config = config

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        title = QLabel(f"Файл: {entry.orig_name}")
        title.setWordWrap(True)
        title.setStyleSheet("font-weight: 600;")
        root.addWidget(title)

        api = getattr(config, "api", None)
        api_enabled = bool(getattr(api, "enabled", False)) if api is not None else False
        if not api_enabled:
            warn = QLabel(
                "⚠ REST API выключен — ссылку можно создать, но она заработает "
                "только после включения API в Настройках (вкладка «Расширенные») "
                "и перезапуска."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #ffcf66; font-size: 11px;")
            root.addWidget(warn)

        form = QFormLayout()
        form.setSpacing(8)
        self._password_edit = QLineEdit()
        self._password_edit.setPlaceholderText("необязательно")
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Пароль", self._password_edit)

        self._expiry_combo = QComboBox()
        for label, secs in self._EXPIRY_OPTIONS:
            self._expiry_combo.addItem(label, secs)
        form.addRow("Срок действия", self._expiry_combo)
        root.addLayout(form)

        self._create_btn = QPushButton("Создать ссылку")
        self._create_btn.clicked.connect(self._on_create)
        root.addWidget(self._create_btn)

        self._url_edit = QLineEdit()
        self._url_edit.setReadOnly(True)
        self._url_edit.setPlaceholderText("ссылка появится здесь")
        self._copy_btn = QPushButton("Копировать")
        self._copy_btn.setEnabled(False)
        self._copy_btn.clicked.connect(self._on_copy)
        url_row = QHBoxLayout()
        url_row.addWidget(self._url_edit)
        url_row.addWidget(self._copy_btn)
        root.addLayout(url_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)

        self.setMinimumWidth(460)
        self.setStyleSheet(_DIALOG_STYLESHEET)

    def _share_url(self, token: str) -> str:
        api = getattr(self._config, "api", None)
        if api is None:
            return f"/share/{token}"
        host = str(getattr(api, "host", "127.0.0.1") or "127.0.0.1")
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        return f"http://{host}:{int(getattr(api, 'port', 0))}/share/{token}"

    def _on_create(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        from app.core.sharing import hash_share_password, new_share_token
        from app.core.utils import now_ts

        entry = self._entry
        secs = int(self._expiry_combo.currentData() or 0)
        expires_ts = (now_ts() + secs) if secs > 0 else 0
        token = new_share_token()
        try:
            self._repo.create_share(
                token,
                entry.folder_path,
                entry.file_key,
                entry.orig_name,
                total_size=entry.total_size,
                password_hash=hash_share_password(self._password_edit.text()),
                expires_ts=expires_ts,
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Ошибка", f"Не удалось создать ссылку: {exc}")
            return
        self._url_edit.setText(self._share_url(token))
        self._copy_btn.setEnabled(True)
        self._create_btn.setText("Создать ещё одну")

    def _on_copy(self) -> None:
        from PySide6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._url_edit.text())
            self._copy_btn.setText("Скопировано ✓")


class FolderPropertiesDialog(QDialog):
    """Свойства папки: число файлов/подпапок, суммарный размер, разбивка по
    состоянию и статус автосинхронизации (рекурсивно по поддереву)."""

    def __init__(
        self,
        *,
        folder_path: str,
        name: str,
        file_count: int,
        total_size: int,
        state_counts: dict[str, int],
        direct_subfolders: int,
        total_subfolders: int,
        synced: bool,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Свойства папки — {name}")
        self.setStyleSheet(_DIALOG_STYLESHEET)
        self.setMinimumWidth(460)

        form = QFormLayout()
        form.addRow("Имя:", QLabel(str(name)))
        path_label = QLabel(str(folder_path))
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path_label.setWordWrap(True)
        form.addRow("Путь:", path_label)
        form.addRow(
            "Подпапок:",
            QLabel(f"{int(direct_subfolders)} (всего: {int(total_subfolders)})"),
        )
        form.addRow("Файлов:", QLabel(str(int(file_count))))
        form.addRow("Общий размер:", QLabel(_format_size(total_size)))

        # Разбивка по состоянию — только непустые группы, в понятном порядке.
        order = ["complete", "incomplete", "offline", "damaged"]
        seen = list(order) + [k for k in state_counts if k not in order]
        for key in seen:
            count = int(state_counts.get(key, 0))
            if count <= 0:
                continue
            text, color = _OBJECT_STATE_LABELS.get(key, (key, "#d8d0f5"))
            label = QLabel(f"{count}")
            label.setStyleSheet(f"color: {color}; font-weight: 700;")
            form.addRow(f"{text}:", label)

        sync_label = QLabel("Включена" if synced else "Выключена")
        sync_label.setStyleSheet(
            "color: #7fd88f; font-weight: 700;" if synced else "color: #a1a1aa;"
        )
        form.addRow("Автосинхронизация:", sync_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(
            self.accept
        )

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(buttons)
