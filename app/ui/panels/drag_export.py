"""Drag & drop export: ExplorerListView and ExplorerDropFrame."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QMimeData, QModelIndex, QPoint, Qt, QUrl, Signal
from PySide6.QtGui import QCursor, QDrag
from PySide6.QtWidgets import QApplication, QFrame, QListView


class ExplorerListView(QListView):
    files_dropped = Signal(list)
    drag_state_changed = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.export_paths_provider: Callable[[QModelIndex | None], list[str] | None] | None = None
        self.export_success_notifier: Callable[[QModelIndex], None] | None = None
        self._drag_start_pos = QPoint()
        self._drag_start_index = QModelIndex()

        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QListView.DragDropMode.DragDrop)
        self.setDropIndicatorShown(True)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.viewport().installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.viewport():
            event_type = event.type()
            if event_type in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
                mime = event.mimeData()
                if mime.hasUrls() and any(url.isLocalFile() for url in mime.urls()):
                    self.drag_state_changed.emit(True)
                    event.setDropAction(Qt.DropAction.CopyAction)
                    event.accept()
                    return True
            if event_type == QEvent.Type.DragLeave:
                self.drag_state_changed.emit(False)
                return False
            if event_type == QEvent.Type.Drop:
                mime = event.mimeData()
                if mime.hasUrls():
                    paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
                    if paths:
                        self.drag_state_changed.emit(False)
                        self.files_dropped.emit(paths)
                        event.setDropAction(Qt.DropAction.CopyAction)
                        event.accept()
                        return True
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            self.drag_state_changed.emit(True)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            self.drag_state_changed.emit(True)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        self.drag_state_changed.emit(False)
        if event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            if paths:
                self.files_dropped.emit(paths)
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
                return
        super().dropEvent(event)

    def startDrag(self, supported_actions) -> None:
        self._start_export_drag(self.currentIndex())

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            click_pos = event.position().toPoint()
            self._drag_start_pos = click_pos
            self._drag_start_index = self.indexAt(self._drag_start_pos)
            if (
                not self._drag_start_index.isValid()
                and not (
                    event.modifiers()
                    & (
                        Qt.KeyboardModifier.ControlModifier
                        | Qt.KeyboardModifier.ShiftModifier
                    )
                )
            ):
                self.clearSelection()
                self.setCurrentIndex(QModelIndex())
                selection_model = self.selectionModel()
                if selection_model is not None:
                    selection_model.clearCurrentIndex()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            if (
                self._drag_start_index.isValid()
                and (event.position().toPoint() - self._drag_start_pos).manhattanLength()
                >= QApplication.startDragDistance()
            ):
                if self._start_export_drag(self._drag_start_index):
                    self._drag_start_index = QModelIndex()
                    return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_start_index = QModelIndex()
        super().mouseReleaseEvent(event)

    def _start_export_drag(self, preferred_index: QModelIndex | None) -> bool:
        if self.export_paths_provider is None:
            return False

        drag_index = preferred_index if preferred_index is not None else self.currentIndex()
        selected = self.selectedIndexes()
        if selected:
            drag_index = selected[0]
        elif not drag_index.isValid():
            idx_under_cursor = self.indexAt(self.viewport().mapFromGlobal(QCursor.pos()))
            if idx_under_cursor.isValid():
                drag_index = idx_under_cursor

        if drag_index.isValid() and drag_index != self.currentIndex():
            self.setCurrentIndex(drag_index)

        paths = self.export_paths_provider(drag_index) or []
        if not paths:
            return False

        existing_files: list[str] = []
        for raw in paths:
            try:
                p = Path(raw).expanduser().resolve()
            except Exception:
                continue
            if p.exists() and p.is_file():
                existing_files.append(str(p))

        if not existing_files:
            return False

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path) for path in existing_files])

        drag = QDrag(self)
        drag.setMimeData(mime)
        result = drag.exec(
            Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
            Qt.DropAction.CopyAction,
        )
        if result != Qt.DropAction.IgnoreAction and self.export_success_notifier and drag_index.isValid():
            self.export_success_notifier(drag_index)
            return True
        return result != Qt.DropAction.IgnoreAction


class ExplorerDropFrame(QFrame):
    files_dropped = Signal(list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("explorerDropFrame")
        self.setProperty("dropActive", False)
        self.setAcceptDrops(True)

    def _set_drop_active(self, active: bool) -> None:
        if bool(self.property("dropActive")) == bool(active):
            return
        self.setProperty("dropActive", bool(active))
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            self._set_drop_active(True)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            self._set_drop_active(True)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event) -> None:
        self._set_drop_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self._set_drop_active(False)
        if event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            if paths:
                self.files_dropped.emit(paths)
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
                return
        super().dropEvent(event)
