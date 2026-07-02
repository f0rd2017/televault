from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QPainter, QPdfWriter
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QApplication

from app.ui.media_viewer import PdfViewerWindow

# Держим окна живыми до конца теста — иначе shiboken соберёт их до проверок.
_KEEP_ALIVE: list[PdfViewerWindow] = []


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _write_pdf(path) -> str:
    writer = QPdfWriter(str(path))
    painter = QPainter(writer)
    painter.drawText(100, 100, "pdf viewer test")
    painter.end()
    return str(path)


def _wait_events(ms: int = 700) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def test_pdf_viewer_loads_valid_pdf(tmp_path) -> None:
    _app()
    pdf_path = _write_pdf(tmp_path / "doc.pdf")
    window = PdfViewerWindow(f"file://{pdf_path}", "doc.pdf")
    _KEEP_ALIVE.append(window)
    window.show()
    _wait_events()
    # Регрессия: QBuffer поверх ВРЕМЕННОГО QByteArray терял данные (PySide не
    # держит ссылку) — документ молча грузился в Status.Error, окно было пустым.
    assert window._doc.status() == QPdfDocument.Status.Ready
    assert window._doc.pageCount() == 1
    assert window._status.isHidden()


def test_pdf_viewer_shows_error_for_corrupt_file(tmp_path) -> None:
    _app()
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"this is not a pdf at all")
    window = PdfViewerWindow(f"file://{bad}", "broken.pdf")
    _KEEP_ALIVE.append(window)
    window.show()
    _wait_events()
    assert window._doc.status() != QPdfDocument.Status.Ready
    # Пользователь должен видеть сообщение, а не пустое окно.
    assert not window._status.isHidden()
    assert "PDF" in window._status.text()
