from __future__ import annotations

import re

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QProgressBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


_INLINE_PROGRESS_RE = re.compile(r"^\s*([^\|%]+?)\s+\d{1,3}%\s*(\|\s*.+)?\s*$")


class ProgressLogWidget(QWidget):
    cancel_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        self.progress.setObjectName("globalProgressBar")

        self.cancel_button = QPushButton("Отмена", self)
        self.cancel_button.setEnabled(False)
        self.cancel_button.setObjectName("cancelJobButton")

        self.spinner_label = QLabel("", self)
        self.spinner_label.setMinimumWidth(132)
        self.spinner_label.setObjectName("activitySpinner")
        self.spinner_label.hide()

        self._spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._spinner_index = 0
        self._spinner_activity = "Обработка"
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(80)
        self._spinner_timer.timeout.connect(self._tick_spinner)
        self._progress_anim = QPropertyAnimation(self.progress, b"value", self)
        self._progress_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._progress_anim.setDuration(180)
        platform_name = (QGuiApplication.platformName() or "").lower()
        self._animate_progress = platform_name != "offscreen"

        controls = QHBoxLayout()
        controls.setContentsMargins(6, 0, 6, 0)
        controls.setSpacing(10)
        controls.addWidget(self.spinner_label)
        controls.addWidget(self.progress, 1)
        controls.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.addLayout(controls)

        # Log overlay container
        self.logs_container = QWidget()
        self.logs_container.setObjectName("progressWidgetOverlay")
        self.logs_container.hide()

        log_layout = QVBoxLayout(self.logs_container)
        log_layout.setContentsMargins(0, 0, 0, 0)

        self.logs = QTextEdit(self.logs_container)
        self.logs.setReadOnly(True)
        self.logs.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.logs.setMinimumHeight(126)
        self.logs.setPlaceholderText("Журнал событий")
        self.logs.document().setMaximumBlockCount(500)
        self.logs.setObjectName("eventsLog")
        log_layout.addWidget(self.logs)

        self.cancel_button.clicked.connect(self.cancel_requested.emit)

    def set_progress(self, value: float, animated: bool = True) -> None:
        target = int(max(0, min(100, value)))
        if not self._animate_progress or not animated:
            self._progress_anim.stop()
            self.progress.setValue(target)
            return

        current = self.progress.value()
        if current == target:
            return
        if abs(target - current) <= 2:
            self._progress_anim.stop()
            self.progress.setValue(target)
            return

        self._progress_anim.stop()
        self._progress_anim.setStartValue(current)
        self._progress_anim.setEndValue(target)
        self._progress_anim.setDuration(min(280, max(130, abs(target - current) * 6)))
        self._progress_anim.start()

    def set_status_text(self, text: str | None) -> None:
        if text:
            normalized = self._normalize_progress_format(text)
            # QProgressBar format uses '%' placeholders; keep %p% and escape other percent signs.
            token = "__PROGRESS_PLACEHOLDER__"
            escaped = (
                normalized.replace("%p%", token)
                .replace("%", "%%")
                .replace(token, "%p%")
            )
            self.progress.setFormat(escaped)
            return
        self.progress.setFormat("%p%")

    def set_busy(self, busy: bool, activity: str = "Обработка") -> None:
        self.cancel_button.setEnabled(busy)
        if busy:
            self._spinner_activity = activity
            self._spinner_index = 0
            self._tick_spinner()
            self.spinner_label.show()
            if not self._spinner_timer.isActive():
                self._spinner_timer.start()
            return

        self._spinner_timer.stop()
        self.spinner_label.hide()
        self.spinner_label.setText("")
        self.set_status_text(None)

    def append_log(self, line: str) -> None:
        self.logs.append(line)

    def _tick_spinner(self) -> None:
        frame = self._spinner_frames[self._spinner_index % len(self._spinner_frames)]
        self._spinner_index += 1
        self.spinner_label.setText(f"{frame} {self._spinner_activity}")

    @staticmethod
    def _normalize_progress_format(text: str) -> str:
        raw = str(text).strip()
        match = _INLINE_PROGRESS_RE.match(raw)
        if not match:
            return raw
        activity = (match.group(1) or "").strip()
        tail = (match.group(2) or "").strip()
        if tail:
            return f"{activity} %p% {tail}"
        return f"{activity} %p%"


class _ArcSpinner(QWidget):
    """Плавный круговой спиннер, нарисованный кодом (без ассетов)."""

    def __init__(self, parent=None, *, diameter: int = 56) -> None:
        super().__init__(parent)
        self._d = int(diameter)
        self.setFixedSize(self._d, self._d)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(28)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        self._angle = (self._angle + 11) % 360
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(5, 5, self.width() - 10, self.height() - 10)
        pen = QPen()
        pen.setWidth(5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        # Track ring.
        pen.setColor(QColor(255, 255, 255, 26))
        painter.setPen(pen)
        painter.drawArc(rect, 0, 360 * 16)
        # Moving accent arc.
        pen.setColor(QColor("#7c5cff"))
        painter.setPen(pen)
        painter.drawArc(rect, -self._angle * 16, 110 * 16)
        painter.end()


class StartupLoadingOverlay(QWidget):
    """Полноэкранный оверлей загрузки: видно, когда программа ещё подключается
    и когда ей уже можно пользоваться. Привязывается к сигналам воркера."""

    retry_requested = Signal()
    accounts_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("startupOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "QWidget#startupOverlay { background: #0b0b0f; }"
            "QLabel#startupTitle { color: #ffffff; font-size: 20px; font-weight: 800;"
            " letter-spacing: 0.5px; background: transparent; }"
            "QLabel#startupStatus { color: #a1a1aa; font-size: 13px;"
            " background: transparent; }"
            "QPushButton#startupBtn { background: #18181b; color: #e4e4e7;"
            " border: 1px solid #3f3f46; border-radius: 10px; padding: 8px 20px;"
            " font-weight: 600; font-size: 13px; }"
            "QPushButton#startupBtn:hover { background: #27272a; color: #ffffff; }"
            "QPushButton#startupBtnPrimary { background: qlineargradient(x1:0, y1:0,"
            " x2:1, y2:1, stop:0 #6d28d9, stop:1 #4f46e5); color: #ffffff;"
            " border: none; border-radius: 10px; padding: 8px 22px; font-weight: 700;"
            " font-size: 13px; }"
            "QPushButton#startupBtnPrimary:hover { background: qlineargradient(x1:0,"
            " y1:0, x2:1, y2:1, stop:0 #7c3aed, stop:1 #6366f1); }"
        )

        root = QVBoxLayout(self)
        root.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.setSpacing(18)

        self._spinner = _ArcSpinner(self)
        root.addWidget(self._spinner, 0, Qt.AlignmentFlag.AlignHCenter)

        self._title = QLabel("Telegram Cloud Cache Manager", self)
        self._title.setObjectName("startupTitle")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._title)

        self._status = QLabel("Запуск…", self)
        self._status.setObjectName("startupStatus")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        self._buttons = QWidget(self)
        btn_row = QHBoxLayout(self._buttons)
        btn_row.setContentsMargins(0, 8, 0, 0)
        btn_row.setSpacing(10)
        btn_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._retry_btn = QPushButton("Повторить", self._buttons)
        self._retry_btn.setObjectName("startupBtnPrimary")
        self._retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._retry_btn.clicked.connect(self.retry_requested.emit)
        self._accounts_btn = QPushButton("Аккаунты", self._buttons)
        self._accounts_btn.setObjectName("startupBtn")
        self._accounts_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._accounts_btn.clicked.connect(self.accounts_requested.emit)
        btn_row.addWidget(self._retry_btn)
        btn_row.addWidget(self._accounts_btn)
        self._buttons.hide()
        root.addWidget(self._buttons, 0, Qt.AlignmentFlag.AlignHCenter)

    def set_status(self, text: str) -> None:
        self._status.setText(str(text))

    def show_loading(self, text: str = "Подключение к Telegram…") -> None:
        self._buttons.hide()
        self._spinner.show()
        self._spinner.start()
        self.set_status(text)
        self.setGraphicsEffect(None)
        self.show()
        self.raise_()

    def show_error(self, message: str) -> None:
        self._spinner.stop()
        self._spinner.hide()
        self.set_status(f"Не удалось подключиться:\n{message}")
        self._buttons.show()
        self.setGraphicsEffect(None)
        self.show()
        self.raise_()

    def finish(self) -> None:
        """Плавно скрыть оверлей — программа готова к работе."""
        self._spinner.stop()
        effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(280)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        anim.finished.connect(self.hide)
        anim.finished.connect(lambda: self.setGraphicsEffect(None))
        self._fade_anim = anim
        anim.start()
