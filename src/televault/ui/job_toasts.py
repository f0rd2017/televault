from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from televault.core.types import JobEvent, JobStatus

_CARD_H = 78
# Maximum number of notification cards simultaneously visible in the overlay.
_MAX_VISIBLE_CARDS = 2


class JobToastCard(QWidget):
    """A single process notification card for one job."""

    def __init__(self, job_type: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.job_type = job_type
        self.job_id: int | None = None
        self._cancel_cb = None
        self._global_cancel_fallback = False
        self.is_terminal = False

        self.setFixedHeight(_CARD_H)
        self.setObjectName("jobToastCard")
        self.setStyleSheet("""
            QWidget#jobToastCard {
                background: #27272a;
                border: 1px solid #3f3f46;
                border-radius: 10px;
            }
        """)

        # Title label
        self._title = QLabel(job_type.replace("_", " ").title(), self)
        self._title.setGeometry(12, 8, 220, 18)
        self._title.setStyleSheet(
            "color: #e4e4e7; font-size: 11px; font-weight: 700; background: transparent; border: none;"
        )

        # Status label
        self._status_label = QLabel(self.tr("Queued"), self)
        self._status_label.setGeometry(12, 27, 228, 16)
        self._status_label.setStyleSheet(
            "color: #a1a1aa; font-size: 10px; background: transparent; border: none;"
        )

        # Progress bar
        self._progress_bar = QProgressBar(self)
        self._progress_bar.setGeometry(12, 49, 234, 8)
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setStyleSheet("""
            QProgressBar { 
                background: #18181b; border: none; border-radius: 4px; 
            }
            QProgressBar::chunk { 
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6d28d9, stop:1 #8b5cf6); 
                border-radius: 4px; 
            }
        """)

        # Cancel button
        self._cancel_btn = QPushButton("×", self)
        self._cancel_btn.setGeometry(262, 7, 24, 24)
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.setStyleSheet("""
            QPushButton { color: #a1a1aa; background: transparent; border: none; font-size: 16px; }
            QPushButton:hover { color: #f43f5e; }
        """)
        self._cancel_btn.clicked.connect(self._on_cancel)

    def set_cancel_callback(self, cb) -> None:
        self._cancel_cb = cb

    def set_global_cancel_fallback(self, enabled: bool) -> None:
        self._global_cancel_fallback = bool(enabled)

    def update_event(self, event: JobEvent) -> None:
        if self.job_id is None and event.job_id >= 0:
            self.job_id = event.job_id

        status_text = {
            JobStatus.QUEUED: self.tr("Queued"),
            JobStatus.STARTED: self.tr("Starting…"),
            JobStatus.RUNNING: event.message or self.tr("In progress…"),
            JobStatus.DONE: self.tr("Done"),
            JobStatus.ERROR: self.tr("Error: {0}").format(
                event.error or self.tr("unknown")
            ),
            JobStatus.CANCELLED: self.tr("Cancelled"),
        }.get(event.status, str(event.status))

        self._status_label.setText(status_text)
        self._status_label.setToolTip(status_text)

        if event.status == JobStatus.RUNNING:
            self._progress_bar.setValue(int(event.progress))
        elif event.status == JobStatus.DONE:
            self._progress_bar.setValue(100)
        elif event.status in {JobStatus.ERROR, JobStatus.CANCELLED}:
            self._progress_bar.setValue(0)

        if event.status in {JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED}:
            self.is_terminal = True
            self._cancel_btn.setEnabled(False)
            self._cancel_btn.setStyleSheet(
                "QPushButton { color: #3f3f46; background: transparent; border: none; font-size: 16px; }"
            )

    def _on_cancel(self) -> None:
        if not self._cancel_cb:
            return
        if self.job_id is not None:
            self._cancel_cb(self.job_id)
            return
        if self._global_cancel_fallback:
            self._cancel_cb(None)


class JobToastOverlay(QWidget):
    """
    Dedicated overlay panel for processes/jobs, matching the new logs panel behavior.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("processPanelOverlay")
        self.hide()

        self.setStyleSheet("""
            QWidget#processPanelOverlay {
                background: #18181b;
                border: 1px solid #3f3f46;
                border-radius: 16px;
            }
            QScrollArea {
                border: none;
                background: transparent;
            }
            QScrollBar:vertical {
                border: none;
                background: #18181b;
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #3f3f46;
                min-height: 20px;
                border-radius: 4px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Header with Clear button
        header_layout = QHBoxLayout()
        header_label = QLabel(self.tr("Processes"))
        header_label.setStyleSheet(
            "color: #e4e4e7; font-size: 13px; font-weight: bold; background: transparent;"
        )

        self.clear_btn = QPushButton(self.tr("Clear completed"))
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.setStyleSheet("""
            QPushButton {
                background: #27272a; color: #a1a1aa; border: 1px solid #3f3f46; border-radius: 6px; padding: 4px 8px; font-size: 11px;
            }
            QPushButton:hover {
                background: #3f3f46; color: #e4e4e7;
            }
        """)
        self.clear_btn.clicked.connect(self._clear_completed)

        header_layout.addWidget(header_label)
        header_layout.addStretch()
        header_layout.addWidget(self.clear_btn)

        layout.addLayout(header_layout)

        # Scroll area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_content = QWidget()
        self.scroll_content.setStyleSheet("background: transparent;")
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_layout.setSpacing(8)
        self.scroll_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll_area.setWidget(self.scroll_content)

        layout.addWidget(self.scroll_area)

        self._cards: list[JobToastCard] = []

    def add_toast(self, job_type: str, cancel_cb=None) -> JobToastCard:
        card = JobToastCard(job_type, parent=self.scroll_content)
        card.set_cancel_callback(cancel_cb)
        self.scroll_layout.addWidget(card)
        self._cards.append(card)

        # Enforce the limit on simultaneously visible cards.
        self._enforce_visible_limit()

        # Scroll to bottom slightly after adding
        from PySide6.QtCore import QTimer

        QTimer.singleShot(
            50,
            lambda: self.scroll_area.verticalScrollBar().setValue(
                self.scroll_area.verticalScrollBar().maximum()
            ),
        )

        return card

    @staticmethod
    def is_card_alive(card: JobToastCard | None) -> bool:
        """True if the card still exists (wasn't evicted/removed).

        A card can be evicted by the visible-notifications limit, and
        external code often keeps a reference to it in job_id/request_id
        dicts -- touching a deleted Qt object would crash.
        """
        if card is None:
            return False
        try:
            from shiboken6 import isValid
        except Exception:
            return True
        return bool(isValid(card))

    def _remove_card(self, card: JobToastCard) -> None:
        if card in self._cards:
            self._cards.remove(card)
        self.scroll_layout.removeWidget(card)
        card.deleteLater()

    def _enforce_visible_limit(self) -> None:
        """Keep no more than ``_MAX_VISIBLE_CARDS`` cards.

        We evict the oldest completed notifications first, so active
        processes stay visible; if there are no completed ones, we remove
        the oldest cards.
        """
        while len(self._cards) > _MAX_VISIBLE_CARDS:
            terminal = next((c for c in self._cards if c.is_terminal), None)
            self._remove_card(terminal if terminal is not None else self._cards[0])

    def hide_all(self) -> None:
        """Hide and remove all toast cards (used during shutdown)."""
        for card in tuple(self._cards):
            self.scroll_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

    def _clear_completed(self) -> None:
        for card in tuple(self._cards):
            if card.is_terminal:
                self._cards.remove(card)
                self.scroll_layout.removeWidget(card)
                card.deleteLater()

    def reanchor(self) -> None:
        """Handled by misc.py resizeEvent now."""
        pass
