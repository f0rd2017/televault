from __future__ import annotations

from televault.db.database import connect_db
from televault.db.repo import DbRepo


def _repo(tmp_path) -> DbRepo:
    return DbRepo(connect_db(tmp_path / "index.sqlite3"))


def test_folder_sync_flag_roundtrip(tmp_path) -> None:
    repo = _repo(tmp_path)
    assert repo.is_folder_synced("Anime/Cache") is False
    assert repo.list_synced_folders() == []

    repo.set_folder_sync("Anime/Cache", True)
    assert repo.is_folder_synced("Anime/Cache") is True
    assert repo.list_synced_folders() == ["Anime/Cache"]


def test_folder_sync_disable_removes_from_list(tmp_path) -> None:
    repo = _repo(tmp_path)
    repo.set_folder_sync("A", True)
    repo.set_folder_sync("B", True)
    assert sorted(repo.list_synced_folders()) == ["A", "B"]

    repo.set_folder_sync("A", False)
    assert repo.is_folder_synced("A") is False
    assert repo.list_synced_folders() == ["B"]


def test_folder_sync_normalizes_path(tmp_path) -> None:
    repo = _repo(tmp_path)
    repo.set_folder_sync("/Anime/Cache/", True)
    assert repo.is_folder_synced("Anime/Cache") is True
