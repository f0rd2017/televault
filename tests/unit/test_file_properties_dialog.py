from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTableWidget

from app.core.types import PartRecord
from app.ui.dialogs._properties import FilePropertiesDialog


def _entry(parts_total: int = 2):
    return SimpleNamespace(
        orig_name="movie.bin",
        folder_path="Anime/Cache",
        file_key="key123",
        parts_total=parts_total,
        total_size=2048,
    )


def _part(part_index: int, *, chat_id: str, lost: bool = False) -> PartRecord:
    return PartRecord(
        msg_id=10 + part_index,
        chat_id=chat_id,
        folder_path="Anime/Cache",
        file_key="key123",
        part_index=part_index,
        parts_total=2,
        orig_name="movie.bin",
        file_size=1024,
        caption_raw="",
        date_ts=1,
        lost_ts=999 if lost else None,
    )


def test_properties_dialog_builds_complete():
    QApplication.instance() or QApplication([])
    parts = [_part(0, chat_id="-100a"), _part(1, chat_id="-100a")]
    dlg = FilePropertiesDialog(
        entry=_entry(),
        parts=parts,
        connected_labels={"-100a": "Acc1"},
        expected_sha256="a" * 64,
    )
    # 2 part rows in the table.
    table = dlg.findChild(QTableWidget)
    assert table is not None
    assert table.rowCount() == 2


def test_properties_dialog_builds_damaged_without_crash():
    QApplication.instance() or QApplication([])
    parts = [_part(0, chat_id="-100a"), _part(1, chat_id="-100a", lost=True)]
    dlg = FilePropertiesDialog(
        entry=_entry(),
        parts=parts,
        connected_labels={"-100a": "Acc1"},
        expected_sha256=None,
    )
    assert dlg.windowTitle().startswith("Properties")


def test_properties_dialog_exposes_note():
    QApplication.instance() or QApplication([])
    dlg = FilePropertiesDialog(
        entry=_entry(),
        parts=[_part(0, chat_id="-100a")],
        connected_labels={"-100a": "Acc1"},
        expected_sha256=None,
        note="моя заметка",
    )
    assert dlg.note_value == "моя заметка"


def test_folder_properties_dialog_builds():
    from app.ui.dialogs._properties import FolderPropertiesDialog

    QApplication.instance() or QApplication([])
    dlg = FolderPropertiesDialog(
        folder_path="Anime/Cache",
        name="Cache",
        file_count=12,
        total_size=5 * 1024 * 1024,
        state_counts={"complete": 10, "incomplete": 2},
        direct_subfolders=2,
        total_subfolders=5,
        synced=True,
    )
    assert dlg.windowTitle() == "Folder Properties — Cache"


def test_folder_properties_dialog_handles_empty_folder():
    from app.ui.dialogs._properties import FolderPropertiesDialog

    QApplication.instance() or QApplication([])
    # Пустая папка без файлов и подпапок не должна падать.
    dlg = FolderPropertiesDialog(
        folder_path="Empty",
        name="Empty",
        file_count=0,
        total_size=0,
        state_counts={},
        direct_subfolders=0,
        total_subfolders=0,
        synced=False,
    )
    assert dlg.windowTitle().startswith("Folder Properties")
