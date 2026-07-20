from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from televault.ui.models_qt import FolderTreeModel


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_set_folders_skips_reset_when_unchanged() -> None:
    _app()
    model = FolderTreeModel()

    resets: list[int] = []
    model.modelAboutToBeReset.connect(lambda: resets.append(1))

    folders = ["A", "A/B", "C"]
    model.set_folders(folders)
    assert resets == [1]  # first time — full reset

    # The same set (different order / duplicates) — reset must NOT repeat,
    # otherwise the tree on the left flickers on every reload during a download.
    model.set_folders(["C", "A/B", "A", "A"])
    assert resets == [1]

    # The set changed → reset again.
    model.set_folders(["A", "A/B", "C", "D"])
    assert resets == [1, 1]


def test_set_folders_builds_tree() -> None:
    _app()
    model = FolderTreeModel()
    model.set_folders(["Anime", "Anime/Movies", "Docs"])

    # Two root nodes: Anime and Docs.
    assert model.rowCount() == 2
    anime_index = model.find_index_by_path("Anime")
    assert anime_index.isValid()
    # Anime has one child — Movies.
    assert model.rowCount(anime_index) == 1
