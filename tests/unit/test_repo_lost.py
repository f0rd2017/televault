from __future__ import annotations

from app.core.types import PartRecord
from app.db.database import connect_db
from app.db.repo import DbRepo


def _repo(tmp_path) -> DbRepo:
    return DbRepo(connect_db(tmp_path / "index.sqlite3"))


def _part(repo: DbRepo, part_index: int, *, lost: bool = False) -> None:
    repo.upsert_folder("F")
    repo.upsert_msg_part(
        PartRecord(
            msg_id=10 + part_index,
            chat_id="-100a",
            folder_path="F",
            file_key="k",
            part_index=part_index,
            parts_total=2,
            orig_name="m.bin",
            file_size=5,
            caption_raw="",
            date_ts=1,
        )
    )


def test_mark_lost_sets_lost_ts_without_deleting(tmp_path):
    repo = _repo(tmp_path)
    _part(repo, 0)
    _part(repo, 1)

    marked = repo.mark_messages_lost_refs([("-100a", 11)])
    assert marked == 1

    parts = repo.get_parts_for_object("F", "k")
    # Row stays visible (not deleted), and carries the lost marker.
    assert {p.part_index for p in parts} == {0, 1}
    lost = {p.part_index for p in parts if p.lost_ts}
    assert lost == {1}


def test_reindex_clears_lost_ts(tmp_path):
    repo = _repo(tmp_path)
    _part(repo, 0)
    _part(repo, 1)
    repo.mark_messages_lost_refs([("-100a", 11)])
    assert any(p.lost_ts for p in repo.get_parts_for_object("F", "k"))

    # Re-seeing/re-indexing the message clears the lost mark (recovery).
    _part(repo, 1)
    assert not any(p.lost_ts for p in repo.get_parts_for_object("F", "k"))


def test_mark_lost_empty_refs_is_noop(tmp_path):
    repo = _repo(tmp_path)
    assert repo.mark_messages_lost_refs([]) == 0


def test_part_chat_ids_and_lost_keys_by_folder(tmp_path):
    repo = _repo(tmp_path)
    _part(repo, 0)
    _part(repo, 1)
    repo.mark_messages_lost_refs([("-100a", 11)])

    chat_map = repo.get_part_chat_ids_by_folder("F")
    assert chat_map.get("k") == {"-100a"}

    lost = repo.get_lost_file_keys_by_folder("F")
    assert lost == {"k"}


def test_object_notes_roundtrip(tmp_path):
    repo = _repo(tmp_path)
    _part(repo, 0)
    assert repo.get_object_note("F", "k") == ""

    repo.set_object_note("F", "k", "important file")
    assert repo.get_object_note("F", "k") == "important file"
    assert repo.get_object_notes_by_folder("F") == {"k": "important file"}

    # Update overwrites.
    repo.set_object_note("F", "k", "updated")
    assert repo.get_object_note("F", "k") == "updated"
