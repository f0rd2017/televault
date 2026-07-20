"""The explorer icon delegate wraps long space-less file names across the cell
instead of eliding them to one line, and must render without error for files,
folders, selected items and empty labels."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QListView,
    QStyle,
    QStyleOptionViewItem,
)

from televault.core.types import ObjectEntry
from televault.ui.models_qt import ExplorerFileItem, ExplorerFolderItem, ExplorerGridModel
from televault.ui.panels.explorer_delegate import ExplorerIconDelegate


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _entry(name: str) -> ObjectEntry:
    return ObjectEntry(
        file_key=name,
        folder_path="main",
        orig_name=name,
        parts_total=1,
        have_parts=1,
        status="complete",
        total_size=10,
        last_seen_ts=0,
    )


def _view_with_items(model: ExplorerGridModel) -> QListView:
    view = QListView()
    view.setViewMode(QListView.ViewMode.IconMode)
    view.setUniformItemSizes(True)
    view.setWordWrap(True)
    view.setGridSize(QSize(100, 110))
    view.setIconSize(QSize(56, 56))
    view.setModel(model)
    view.setItemDelegate(ExplorerIconDelegate(view))
    return view


def _paint(view: QListView, model: ExplorerGridModel, row: int, *, selected: bool):
    delegate = view.itemDelegate()
    index = model.index(row, 0)
    opt = QStyleOptionViewItem()
    opt.initFrom(view)
    opt.rect = QRect(0, 0, 100, 110)
    opt.decorationSize = QSize(56, 56)
    if selected:
        opt.state |= QStyle.StateFlag.State_Selected
    pm = QPixmap(100, 110)
    pm.fill(Qt.GlobalColor.black)
    painter = QPainter(pm)
    try:
        delegate.paint(painter, opt, index)
    finally:
        painter.end()
    return delegate, index, opt


def test_delegate_size_hint_matches_model_grid_cell():
    _app()
    model = ExplorerGridModel(thumb_cache_dir=None)
    model.set_icon_size(56)
    model.set_items([ExplorerFileItem(entry=_entry("a.jpg"), local_exists=True)])
    view = _view_with_items(model)
    delegate, index, opt = _paint(view, model, 0, selected=False)
    assert delegate.sizeHint(opt, index) == QSize(100, 110)


def test_delegate_paints_file_folder_selected_and_long_name():
    _app()
    model = ExplorerGridModel(thumb_cache_dir=None)
    model.set_icon_size(56)
    model.set_items(
        [
            ExplorerFolderItem(name="Sub", path="main/Sub", downloaded=True),
            ExplorerFileItem(
                entry=_entry("photo_2026-06-21_evening_no_spaces.jpg"),
                local_exists=True,
            ),
            ExplorerFileItem(entry=_entry("x.zip"), local_exists=False),
        ]
    )
    view = _view_with_items(model)
    # Folder, long space-less file, and a selected file all paint without raising.
    _paint(view, model, 0, selected=False)
    _paint(view, model, 1, selected=False)
    _paint(view, model, 2, selected=True)


def test_delegate_handles_empty_label_and_null_decoration():
    _app()
    model = ExplorerGridModel(thumb_cache_dir=None)
    # A folder with an empty name → empty DisplayRole; must not crash.
    model.set_items([ExplorerFolderItem(name="", path="main/")])
    view = _view_with_items(model)
    _paint(view, model, 0, selected=False)
