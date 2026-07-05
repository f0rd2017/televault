"""Keyboard shortcuts reference dialog."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.ui.dialogs._style import _DIALOG_STYLESHEET

_KEY_CHIP_STYLESHEET = (
    "background: #27272a; color: #e4e4e7; border: 1px solid #3f3f46;"
    "border-radius: 4px; padding: 2px 7px; font-family: monospace; font-size: 12px;"
)


class HotkeysDialog(QDialog):
    """Shows every keyboard shortcut registered on the main window, grouped by
    category. Entries are passed in pre-translated so the caller controls
    wording/ordering; this dialog is purely presentational."""

    def __init__(self, entries: list[tuple[str, list[str], str]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Keyboard shortcuts"))
        self.setStyleSheet(_DIALOG_STYLESHEET)
        self.setMinimumSize(540, 580)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(2, 2, 8, 2)
        content_layout.setSpacing(16)

        grouped: dict[str, list[tuple[list[str], str]]] = {}
        order: list[str] = []
        for category, keys, description in entries:
            if category not in grouped:
                grouped[category] = []
                order.append(category)
            grouped[category].append((keys, description))

        for category in order:
            header = QLabel(category)
            header.setStyleSheet("color: #a78bfa; font-weight: 700; font-size: 13px;")
            content_layout.addWidget(header)

            grid = QGridLayout()
            grid.setHorizontalSpacing(14)
            grid.setVerticalSpacing(8)
            grid.setColumnStretch(1, 1)
            for row, (keys, description) in enumerate(grouped[category]):
                keys_row = QHBoxLayout()
                keys_row.setSpacing(4)
                for i, key in enumerate(keys):
                    if i:
                        sep = QLabel("/")
                        sep.setStyleSheet("color: #52525b;")
                        keys_row.addWidget(sep)
                    chip = QLabel(key)
                    chip.setStyleSheet(_KEY_CHIP_STYLESHEET)
                    keys_row.addWidget(chip)
                keys_row.addStretch(1)
                keys_host = QWidget()
                keys_host.setLayout(keys_row)
                keys_host.setMinimumWidth(160)
                grid.addWidget(keys_host, row, 0)

                desc_label = QLabel(description)
                desc_label.setWordWrap(True)
                grid.addWidget(desc_label, row, 1)
            content_layout.addLayout(grid)

        content_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(
            self.accept
        )
        root.addWidget(buttons)
