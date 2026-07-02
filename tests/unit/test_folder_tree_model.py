from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.ui.models_qt import FolderTreeModel


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_set_folders_skips_reset_when_unchanged() -> None:
    _app()
    model = FolderTreeModel()

    resets: list[int] = []
    model.modelAboutToBeReset.connect(lambda: resets.append(1))

    folders = ["A", "A/B", "C"]
    model.set_folders(folders)
    assert resets == [1]  # первый раз — полный reset

    # Тот же набор (другой порядок / дубликаты) — reset НЕ должен повторяться,
    # иначе дерево слева мигает при каждом релоаде во время скачивания.
    model.set_folders(["C", "A/B", "A", "A"])
    assert resets == [1]

    # Изменился набор → reset снова.
    model.set_folders(["A", "A/B", "C", "D"])
    assert resets == [1, 1]


def test_set_folders_builds_tree() -> None:
    _app()
    model = FolderTreeModel()
    model.set_folders(["Anime", "Anime/Movies", "Docs"])

    # Два корневых узла: Anime и Docs.
    assert model.rowCount() == 2
    anime_index = model.find_index_by_path("Anime")
    assert anime_index.isValid()
    # У Anime один дочерний — Movies.
    assert model.rowCount(anime_index) == 1
