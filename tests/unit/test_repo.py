from pathlib import Path

from televault.core.types import PartRecord
from televault.db.database import connect_db
from televault.db.repo import DbRepo


def test_repo_rebuild_complete(tmp_path) -> None:
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    chat_id = "100"

    repo.upsert_folder("Anime/Cache")
    repo.upsert_msg_part(
        PartRecord(
            msg_id=1,
            chat_id=chat_id,
            folder_path="Anime/Cache",
            file_key="abc",
            part_index=0,
            parts_total=2,
            orig_name="x.bin",
            file_size=5,
            caption_raw="",
            date_ts=100,
        )
    )
    repo.upsert_msg_part(
        PartRecord(
            msg_id=2,
            chat_id=chat_id,
            folder_path="Anime/Cache",
            file_key="abc",
            part_index=1,
            parts_total=2,
            orig_name="x.bin",
            file_size=7,
            caption_raw="",
            date_ts=101,
        )
    )

    repo.rebuild_objects_aggregates()
    objects = repo.list_objects_by_folder("Anime/Cache")
    assert len(objects) == 1
    assert objects[0].status == "complete"
    assert objects[0].total_size == 12


def test_repo_dedup_and_delete(tmp_path) -> None:
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    chat_id = "200"

    repo.upsert_folder("Work/Builds")
    repo.upsert_msg_part(
        PartRecord(
            msg_id=10,
            chat_id=chat_id,
            folder_path="Work/Builds",
            file_key="zzz",
            part_index=0,
            parts_total=1,
            orig_name="old.bin",
            file_size=3,
            caption_raw="",
            date_ts=10,
        )
    )
    repo.upsert_msg_part(
        PartRecord(
            msg_id=11,
            chat_id=chat_id,
            folder_path="Work/Builds",
            file_key="zzz",
            part_index=0,
            parts_total=1,
            orig_name="new.bin",
            file_size=5,
            caption_raw="",
            date_ts=20,
        )
    )

    repo.rebuild_objects_aggregates()
    obj = repo.list_objects_by_folder("Work/Builds")[0]
    assert obj.orig_name == "new.bin"
    assert obj.total_size == 5

    deleted = repo.mark_messages_deleted([10, 11])
    assert deleted == 2
    repo.rebuild_objects_aggregates()
    assert repo.list_objects_by_folder("Work/Builds") == []


def test_get_parts_for_object_uses_latest_consistent_revision(tmp_path) -> None:
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    chat_id = "rev"
    folder = "Smoke/Diag"
    file_key = "samekey123456"

    # Older revision with 4 parts.
    for idx in range(4):
        repo.upsert_msg_part(
            PartRecord(
                msg_id=100 + idx,
                chat_id=chat_id,
                folder_path=folder,
                file_key=file_key,
                part_index=idx,
                parts_total=4,
                orig_name="sample.bin",
                file_size=10 + idx,
                caption_raw="",
                date_ts=1000 + idx,
            )
        )

    # Newer revision of the same file_key but single-part upload.
    repo.upsert_msg_part(
        PartRecord(
            msg_id=200,
            chat_id=chat_id,
            folder_path=folder,
            file_key=file_key,
            part_index=0,
            parts_total=1,
            orig_name="sample.bin",
            file_size=99,
            caption_raw="",
            date_ts=2000,
        )
    )

    parts = repo.get_parts_for_object(folder, file_key)
    assert len(parts) == 1
    assert parts[0].msg_id == 200
    assert parts[0].parts_total == 1
    assert parts[0].part_index == 0


def test_repo_jobs_persistence_lifecycle(tmp_path) -> None:
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)

    job_id = repo.insert_job(
        "upload",
        {"file_path": str(Path("x.bin")), "folder_path": "A/B"},
        status="queued",
    )
    jobs = repo.list_jobs(limit=10)
    assert jobs
    assert jobs[0]["id"] == job_id
    assert jobs[0]["status"] == "queued"

    repo.update_job(job_id, status="running", progress=42.5)
    jobs = repo.list_jobs(limit=10)
    assert jobs[0]["status"] == "running"
    assert jobs[0]["progress"] == 42.5

    repo.update_job(job_id, status="error", progress=42.5, error_text="network")
    jobs = repo.list_jobs(limit=10)
    assert jobs[0]["status"] == "error"
    assert jobs[0]["error_text"] == "network"


def test_upsert_folder_keeps_pinned_when_not_specified(tmp_path) -> None:
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)

    repo.upsert_folder("Pinned/Folder", pinned=1)
    repo.upsert_folder("Pinned/Folder")

    folders = repo.list_folders()
    assert len(folders) == 1
    assert folders[0].folder_path == "Pinned/Folder"
    assert folders[0].pinned == 1


def test_rebuild_single_object_aggregate(tmp_path) -> None:
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    chat_id = "single"

    repo.upsert_msg_parts_bulk(
        [
            PartRecord(
                msg_id=1,
                chat_id=chat_id,
                folder_path="A/B",
                file_key="obj1",
                part_index=0,
                parts_total=1,
                orig_name="one.bin",
                file_size=10,
                caption_raw="",
                date_ts=100,
            ),
            PartRecord(
                msg_id=2,
                chat_id=chat_id,
                folder_path="A/B",
                file_key="obj2",
                part_index=0,
                parts_total=1,
                orig_name="two.bin",
                file_size=20,
                caption_raw="",
                date_ts=101,
            ),
        ]
    )
    repo.upsert_folders_bulk(["A/B"])
    repo.rebuild_objects_aggregates()
    assert len(repo.list_objects_by_folder("A/B")) == 2

    repo.mark_messages_deleted([1])
    repo.rebuild_object_aggregate(chat_id, "A/B", "obj1")
    rows = repo.list_objects_by_folder("A/B")
    assert len(rows) == 1
    assert rows[0].file_key == "obj2"


def test_mark_messages_deleted_large_batch(tmp_path) -> None:
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    chat_id = "bulk-delete"

    parts = [
        PartRecord(
            msg_id=i,
            chat_id=chat_id,
            folder_path="Bulk/Test",
            file_key=f"k{i}",
            part_index=0,
            parts_total=1,
            orig_name=f"{i}.bin",
            file_size=1,
            caption_raw="",
            date_ts=100 + i,
        )
        for i in range(1, 1205)
    ]
    repo.upsert_folders_bulk(["Bulk/Test"])
    repo.upsert_msg_parts_bulk(parts)

    deleted = repo.mark_messages_deleted([part.msg_id for part in parts])
    assert deleted == len(parts)


def test_batch_overlay_unified_list_and_tombstone(tmp_path) -> None:
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)

    repo.upsert_folder("Anime/Cache")
    repo.upsert_msg_part(
        PartRecord(
            msg_id=901,
            chat_id="chat-1",
            folder_path="Anime/Cache",
            file_key="blob001",
            part_index=0,
            parts_total=1,
            orig_name="batch_2_files.zip",
            file_size=1234,
            caption_raw='FC1|{"version":2,"kind":"tgccm_batch_blob","folder_path":"Anime/Cache","blob_key":"blob001","orig_name":"batch_2_files.zip","members_count":2}',
            date_ts=1000,
        )
    )
    repo.rebuild_objects_aggregates()
    repo.upsert_batch_blob(
        blob_key="blob001",
        folder_path="Anime/Cache",
        chat_id="chat-1",
        msg_id=901,
        blob_name="batch_2_files.zip",
        blob_size=1234,
        blob_sha256=None,
        manifest_json='{"members":[]}',
    )
    repo.upsert_batch_members_bulk(
        [
            {
                "folder_path": "Anime/Cache",
                "file_key": "m1",
                "blob_key": "blob001",
                "orig_name": "a.txt",
                "member_index": 0,
                "member_size": 10,
                "member_sha256": "a" * 64,
            },
            {
                "folder_path": "Anime/Cache",
                "file_key": "m2",
                "blob_key": "blob001",
                "orig_name": "b.txt",
                "member_index": 1,
                "member_size": 20,
                "member_sha256": "b" * 64,
            },
        ]
    )

    rows = repo.list_objects_by_folder("Anime/Cache")
    assert len(rows) == 2
    assert sorted(item.file_key for item in rows) == ["m1", "m2"]
    assert all(item.storage_kind == "batch_member" for item in rows)
    assert repo.resolve_object_storage("Anime/Cache", "m1") == "batch_member"
    assert repo.count_active_batch_members("blob001") == 2

    assert repo.rename_batch_member("Anime/Cache", "m1", "renamed.txt") == 1
    renamed = repo.get_batch_member("Anime/Cache", "m1")
    assert renamed is not None
    assert renamed.orig_name == "renamed.txt"
    assert renamed.name_pinned == 1

    assert repo.mark_batch_member_deleted("Anime/Cache", "m1") == 1
    assert repo.count_active_batch_members("blob001") == 1
    rows_after = repo.list_objects_by_folder("Anime/Cache")
    assert len(rows_after) == 1
    assert rows_after[0].file_key == "m2"


def test_trash_hides_restores_and_lists(tmp_path) -> None:
    """Trash: move_to_trash hides from listings, list_trash shows,
    restore brings back, delete_trash_entry removes the record."""
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    repo.upsert_folder("Docs")
    for msg_id, key in ((1, "k1"), (2, "k2")):
        repo.upsert_msg_part(
            PartRecord(
                msg_id=msg_id,
                chat_id="c",
                folder_path="Docs",
                file_key=key,
                part_index=0,
                parts_total=1,
                orig_name=f"f{msg_id}.txt",
                file_size=10,
                caption_raw="",
                date_ts=msg_id,
            )
        )
    repo.rebuild_objects_aggregates()
    assert sorted(o.file_key for o in repo.list_objects_by_folder("Docs")) == [
        "k1",
        "k2",
    ]

    repo.move_to_trash("Docs", "k1", "f1.txt", "regular", 10)
    # Hidden from the normal listing, visible in trash.
    assert [o.file_key for o in repo.list_objects_by_folder("Docs")] == ["k2"]
    assert [o.file_key for o in repo.list_trash()] == ["k1"]
    assert repo.count_trash() == 1

    # Restore brings it back to the listing.
    assert repo.restore_from_trash("Docs", "k1") == 1
    assert sorted(o.file_key for o in repo.list_objects_by_folder("Docs")) == [
        "k1",
        "k2",
    ]
    assert repo.count_trash() == 0

    # Trash survives the reconcile rebuild of aggregates.
    repo.move_to_trash("Docs", "k2", "f2.txt")
    repo.rebuild_objects_aggregates()
    assert [o.file_key for o in repo.list_objects_by_folder("Docs")] == ["k1"]
    assert [o.file_key for o in repo.list_trash()] == ["k2"]
    # Deleting the trash record (after remote-delete) is final.
    assert repo.delete_trash_entry("Docs", "k2") == 1
    assert repo.count_trash() == 0


def test_supersede_batch_members_by_name(tmp_path) -> None:
    """An updated small file (same folder+name, new key) evicts the old
    version: the old member is marked deleted and disappears from the listing."""
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)

    repo.upsert_folder("Docs")
    for blob_key, msg_id in (("blobOld", 11), ("blobNew", 12)):
        repo.upsert_batch_blob(
            blob_key=blob_key,
            folder_path="Docs",
            chat_id="chat-1",
            msg_id=msg_id,
            blob_name=f"{blob_key}.zip",
            blob_size=100,
            blob_sha256=None,
            manifest_json='{"members":[]}',
        )
    # Old and new versions of the same file a.txt — different keys (different sha).
    repo.upsert_batch_members_bulk(
        [
            {
                "folder_path": "Docs",
                "file_key": "a_old",
                "blob_key": "blobOld",
                "orig_name": "a.txt",
                "member_index": 0,
                "member_size": 10,
                "member_sha256": "a" * 64,
            },
            {
                "folder_path": "Docs",
                "file_key": "keepb",
                "blob_key": "blobOld",
                "orig_name": "b.txt",
                "member_index": 1,
                "member_size": 20,
                "member_sha256": "b" * 64,
            },
            {
                "folder_path": "Docs",
                "file_key": "a_new",
                "blob_key": "blobNew",
                "orig_name": "a.txt",
                "member_index": 0,
                "member_size": 11,
                "member_sha256": "c" * 64,
            },
        ]
    )
    # Before eviction, a.txt is duplicated (a_old + a_new).
    before = sorted(o.file_key for o in repo.list_objects_by_folder("Docs"))
    assert before == ["a_new", "a_old", "keepb"]

    # Evict old versions of a.txt, keeping a_new.
    superseded = repo.supersede_batch_members_by_name("Docs", "a.txt", "a_new")
    assert superseded == 1

    after = sorted(o.file_key for o in repo.list_objects_by_folder("Docs"))
    assert after == ["a_new", "keepb"]  # a_old is gone, b.txt untouched
    # A repeat call is idempotent (the old one is already deleted).
    assert repo.supersede_batch_members_by_name("Docs", "a.txt", "a_new") == 0
