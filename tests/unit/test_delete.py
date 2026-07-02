from __future__ import annotations

import pytest
from telethon.errors import MessageDeleteForbiddenError, MessageIdInvalidError

from app.core.types import AppConfig, CryptoConfig, PartRecord, RetryConfig
from app.db.database import connect_db
from app.db.repo import DbRepo
from app.tg.client import TgClientEndpoint
from app.tg.delete import TgDeleter
from app.tg.parser import parse_caption


class FakeChat:
    def __init__(self, chat_id: int | str) -> None:
        self.id = int(chat_id)


class FakeEditClient:
    def __init__(self) -> None:
        self.edits: list[tuple[int, str]] = []

    async def edit_message(self, chat, msg_id: int, text: str) -> None:
        _ = chat
        self.edits.append((msg_id, text))


class FakeDeleteClient:
    def __init__(
        self,
        *,
        batch_invalid_once: bool = False,
        always_forbidden: bool = False,
        allowed_chat_ids: set[str] | None = None,
    ) -> None:
        self.batch_invalid_once = batch_invalid_once
        self.always_forbidden = always_forbidden
        self.allowed_chat_ids = {str(item) for item in (allowed_chat_ids or set())}
        self.calls: list[list[int]] = []
        self.chat_calls: list[tuple[str | None, list[int]]] = []

    async def delete_messages(self, chat, msg_ids) -> None:
        ids = [int(x) for x in msg_ids]
        chat_id = getattr(chat, "id", None)
        self.calls.append(ids)
        self.chat_calls.append((str(chat_id) if chat_id is not None else None, ids))
        if self.allowed_chat_ids and str(chat_id) not in self.allowed_chat_ids:
            raise ValueError("Could not find the input entity for the given route")
        if self.always_forbidden:
            raise MessageDeleteForbiddenError(request=None)
        if self.batch_invalid_once and len(ids) > 1:
            self.batch_invalid_once = False
            raise MessageIdInvalidError(request=None)


@pytest.mark.asyncio
async def test_rename_preserves_integrity_metadata(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    repo.upsert_folder("Anime/Cache")

    sha = "a" * 64
    caption = (
        'FC1|{"folder_path":"Anime/Cache","file_key":"abc123abc123",'
        '"part_index":0,"parts_total":1,"orig_name":"old.bin",'
        f'"sha256":"{sha}","orig_size":12345,"part_size":4567,"enc":true}}'
    )
    repo.upsert_msg_part(
        PartRecord(
            msg_id=10,
            chat_id="1",
            folder_path="Anime/Cache",
            file_key="abc123abc123",
            part_index=0,
            parts_total=1,
            orig_name="old.bin",
            file_size=4567,
            caption_raw=caption,
            date_ts=100,
        )
    )
    repo.rebuild_objects_aggregates()

    client = FakeEditClient()
    deleter = TgDeleter(config, repo, client, chat=object(), chat_id="1")
    result = await deleter.rename_file("Anime/Cache", "abc123abc123", "new.bin")

    assert result["edited"] == 1
    assert len(client.edits) == 1
    updated = repo.get_parts_for_object("Anime/Cache", "abc123abc123")[0].caption_raw
    assert updated is not None
    parsed = parse_caption(updated, prefix=config.caption_prefix)
    assert parsed is not None
    assert parsed.orig_name == "new.bin"
    assert parsed.sha256 == sha
    assert parsed.orig_size == 12345
    assert parsed.part_size == 4567
    assert parsed.enc is True
    assert repo.list_objects("Anime/Cache")[0].orig_name == "new.bin"


@pytest.mark.asyncio
async def test_rename_adds_metadata_for_unmanaged_message(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    repo.upsert_folder("Imported")
    repo.upsert_msg_part(
        PartRecord(
            msg_id=11,
            chat_id="1",
            folder_path="Imported",
            file_key="msg_000000000000000b",
            part_index=0,
            parts_total=1,
            orig_name="old_manual.bin",
            file_size=777,
            caption_raw="",
            date_ts=101,
        )
    )
    repo.rebuild_objects_aggregates()

    client = FakeEditClient()
    deleter = TgDeleter(config, repo, client, chat=object(), chat_id="1")
    result = await deleter.rename_file(
        "Imported", "msg_000000000000000b", "new_manual.bin"
    )

    assert result["edited"] == 1
    assert len(client.edits) == 1
    updated = repo.get_parts_for_object("Imported", "msg_000000000000000b")[
        0
    ].caption_raw
    assert updated is not None
    parsed = parse_caption(updated, prefix=config.caption_prefix)
    assert parsed is not None
    assert parsed.orig_name == "new_manual.bin"
    assert parsed.file_key == "msg_000000000000000b"
    assert repo.list_objects("Imported")[0].orig_name == "new_manual.bin"


@pytest.mark.asyncio
async def test_delete_remote_falls_back_to_single_when_batch_has_invalid_id(
    tmp_path,
) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    repo.upsert_folder("Anime/Cache")
    repo.upsert_msg_part(
        PartRecord(
            msg_id=21,
            chat_id="1",
            folder_path="Anime/Cache",
            file_key="delete_fallback_01",
            part_index=0,
            parts_total=2,
            orig_name="movie.bin",
            file_size=123,
            caption_raw="",
            date_ts=201,
        )
    )
    repo.upsert_msg_part(
        PartRecord(
            msg_id=22,
            chat_id="1",
            folder_path="Anime/Cache",
            file_key="delete_fallback_01",
            part_index=1,
            parts_total=2,
            orig_name="movie.bin",
            file_size=456,
            caption_raw="",
            date_ts=202,
        )
    )
    repo.rebuild_objects_aggregates()

    client = FakeDeleteClient(batch_invalid_once=True)
    deleter = TgDeleter(config, repo, client, chat=object(), chat_id="1")
    result = await deleter.delete_remote("Anime/Cache", "delete_fallback_01")

    assert result["deleted"] == 2
    assert result["failed"] == 0
    # First call is batch, then fallback per part.
    assert client.calls[0] == [21, 22]
    assert [21] in client.calls
    assert [22] in client.calls
    repo.rebuild_objects_aggregates()
    assert repo.list_objects_by_folder("Anime/Cache") == []


@pytest.mark.asyncio
async def test_delete_remote_raises_when_forbidden_parts_remain(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    repo.upsert_folder("Anime/Cache")
    repo.upsert_msg_part(
        PartRecord(
            msg_id=31,
            chat_id="1",
            folder_path="Anime/Cache",
            file_key="delete_forbidden_01",
            part_index=0,
            parts_total=1,
            orig_name="service_like.bin",
            file_size=123,
            caption_raw="",
            date_ts=301,
        )
    )
    repo.rebuild_objects_aggregates()

    client = FakeDeleteClient(always_forbidden=True)
    deleter = TgDeleter(config, repo, client, chat=object(), chat_id="1")

    with pytest.raises(RuntimeError, match="Failed to delete") as exc_info:
        await deleter.delete_remote("Anime/Cache", "delete_forbidden_01")

    # The error must surface the underlying reason, not just an opaque count.
    assert "forbidden" in str(exc_info.value).lower()

    # DB should still keep object because remote delete did not succeed.
    repo.rebuild_objects_aggregates()
    objects = repo.list_objects_by_folder("Anime/Cache")
    assert len(objects) == 1


@pytest.mark.asyncio
async def test_delete_remote_uses_matching_client_for_each_cross_channel_part(
    tmp_path,
) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    repo.upsert_folder("Anime/Cache")
    for msg_id, chat_id, part_index in (
        (101, "1", 0),
        (102, "2", 1),
        (103, "3", 2),
    ):
        repo.upsert_msg_part(
            PartRecord(
                msg_id=msg_id,
                chat_id=chat_id,
                folder_path="Anime/Cache",
                file_key="delete_cross_channel_01",
                part_index=part_index,
                parts_total=3,
                orig_name="movie.bin",
                file_size=123,
                caption_raw="",
                date_ts=400 + part_index,
            )
        )
    repo.rebuild_objects_aggregates()

    main_client = FakeDeleteClient(allowed_chat_ids={"1"})
    account2_client = FakeDeleteClient(allowed_chat_ids={"2"})
    account3_client = FakeDeleteClient(allowed_chat_ids={"3"})
    deleter = TgDeleter(
        config,
        repo,
        main_client,
        chat=FakeChat(1),
        chat_id="1",
        chats=[FakeChat(1), FakeChat(2), FakeChat(3)],
        chat_ids=["1", "2", "3"],
        delete_endpoints=[
            TgClientEndpoint(main_client, FakeChat(1), "1", 0, "account", "a1"),
            TgClientEndpoint(account2_client, FakeChat(2), "2", 1, "account", "a2"),
            TgClientEndpoint(account3_client, FakeChat(3), "3", 2, "account", "a3"),
        ],
    )

    result = await deleter.delete_remote("Anime/Cache", "delete_cross_channel_01")

    assert result["deleted"] == 3
    assert result["failed"] == 0
    assert main_client.chat_calls == [("1", [101])]
    assert account2_client.chat_calls == [("2", [102])]
    assert account3_client.chat_calls == [("3", [103])]
    repo.rebuild_objects_aggregates()
    assert repo.list_objects_by_folder("Anime/Cache") == []


@pytest.mark.asyncio
async def test_delete_remote_skips_unusable_first_route_and_uses_valid_route(
    tmp_path,
) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    repo.upsert_folder("Anime/Cache")
    repo.upsert_msg_part(
        PartRecord(
            msg_id=201,
            chat_id="2",
            folder_path="Anime/Cache",
            file_key="delete_route_fallback_01",
            part_index=0,
            parts_total=1,
            orig_name="route.bin",
            file_size=123,
            caption_raw="",
            date_ts=501,
        )
    )
    repo.rebuild_objects_aggregates()

    wrong_client = FakeDeleteClient(allowed_chat_ids={"1"})
    right_client = FakeDeleteClient(allowed_chat_ids={"2"})
    deleter = TgDeleter(
        config,
        repo,
        right_client,
        chat=FakeChat(2),
        chat_id="2",
        delete_endpoints=[
            TgClientEndpoint(right_client, FakeChat(2), "2", 0, "account", "right"),
        ],
    )
    deleter._routes_by_chat_id["2"] = [
        (wrong_client, FakeChat(2), "wrong"),
        (right_client, FakeChat(2), "right"),
    ]

    result = await deleter.delete_remote("Anime/Cache", "delete_route_fallback_01")

    assert result["deleted"] == 1
    assert result["failed"] == 0
    assert wrong_client.chat_calls == [("2", [201])]
    assert right_client.chat_calls == [("2", [201])]
    repo.rebuild_objects_aggregates()
    assert repo.list_objects_by_folder("Anime/Cache") == []


@pytest.mark.asyncio
async def test_delete_remote_keeps_partial_object_when_one_cross_channel_part_fails(
    tmp_path,
) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    repo.upsert_folder("Anime/Cache")
    for msg_id, chat_id, part_index in (
        (301, "1", 0),
        (302, "2", 1),
        (303, "3", 2),
    ):
        repo.upsert_msg_part(
            PartRecord(
                msg_id=msg_id,
                chat_id=chat_id,
                folder_path="Anime/Cache",
                file_key="delete_partial_cross_channel_01",
                part_index=part_index,
                parts_total=3,
                orig_name="movie.bin",
                file_size=123,
                caption_raw="",
                date_ts=600 + part_index,
            )
        )
    repo.rebuild_objects_aggregates()

    client1 = FakeDeleteClient(allowed_chat_ids={"1"})
    client2 = FakeDeleteClient(always_forbidden=True, allowed_chat_ids={"2"})
    client3 = FakeDeleteClient(allowed_chat_ids={"3"})
    deleter = TgDeleter(
        config,
        repo,
        client1,
        chat=FakeChat(1),
        chat_id="1",
        chats=[FakeChat(1), FakeChat(2), FakeChat(3)],
        chat_ids=["1", "2", "3"],
        delete_endpoints=[
            TgClientEndpoint(client1, FakeChat(1), "1", 0, "account", "a1"),
            TgClientEndpoint(client2, FakeChat(2), "2", 1, "account", "a2"),
            TgClientEndpoint(client3, FakeChat(3), "3", 2, "account", "a3"),
        ],
    )

    with pytest.raises(RuntimeError, match="Failed to delete"):
        await deleter.delete_remote("Anime/Cache", "delete_partial_cross_channel_01")

    repo.rebuild_objects_aggregates()
    objects = repo.list_objects_by_folder("Anime/Cache")
    assert len(objects) == 1
    remaining_refs = repo.get_all_msg_index_refs_for_object(
        "Anime/Cache", "delete_partial_cross_channel_01"
    )
    assert remaining_refs == [("2", 302)]


@pytest.mark.asyncio
async def test_batch_member_delete_is_logical_until_last_member(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    repo.upsert_folder("Anime/Cache")
    repo.upsert_msg_part(
        PartRecord(
            msg_id=501,
            chat_id="1",
            folder_path="Anime/Cache",
            file_key="blobk1",
            part_index=0,
            parts_total=1,
            orig_name="batch.zip",
            file_size=100,
            caption_raw='FC1|{"version":2,"kind":"tgccm_batch_blob","folder_path":"Anime/Cache","blob_key":"blobk1","orig_name":"batch.zip","members_count":2}',
            date_ts=1000,
        )
    )
    repo.rebuild_objects_aggregates()
    repo.upsert_batch_blob(
        blob_key="blobk1",
        folder_path="Anime/Cache",
        chat_id="1",
        msg_id=501,
        blob_name="batch.zip",
        blob_size=100,
        blob_sha256=None,
        manifest_json='{"members":[]}',
    )
    repo.upsert_batch_members_bulk(
        [
            {
                "folder_path": "Anime/Cache",
                "file_key": "m1",
                "blob_key": "blobk1",
                "orig_name": "a.txt",
                "member_index": 0,
                "member_size": 10,
                "member_sha256": "a" * 64,
            },
            {
                "folder_path": "Anime/Cache",
                "file_key": "m2",
                "blob_key": "blobk1",
                "orig_name": "b.txt",
                "member_index": 1,
                "member_size": 20,
                "member_sha256": "b" * 64,
            },
        ]
    )

    client = FakeDeleteClient()
    deleter = TgDeleter(config, repo, client, chat=object(), chat_id="1")
    first = await deleter.delete_remote("Anime/Cache", "m1")
    assert first["logical_only"] is True
    assert first.get("blob_gc") is False
    assert repo.count_active_batch_members("blobk1") == 1
    assert client.calls == []

    second = await deleter.delete_remote("Anime/Cache", "m2")
    assert second["logical_only"] is True
    assert second.get("blob_gc") is True
    assert repo.count_active_batch_members("blobk1") == 0
    assert client.calls == [[501]]


@pytest.mark.asyncio
async def test_batch_member_rename_is_logical_only(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    repo.upsert_batch_blob(
        blob_key="blobk2",
        folder_path="Anime/Cache",
        chat_id="1",
        msg_id=777,
        blob_name="batch.zip",
        blob_size=100,
        blob_sha256=None,
        manifest_json='{"members":[]}',
    )
    repo.upsert_batch_members_bulk(
        [
            {
                "folder_path": "Anime/Cache",
                "file_key": "mk",
                "blob_key": "blobk2",
                "orig_name": "old.txt",
                "member_index": 0,
                "member_size": 10,
                "member_sha256": "a" * 64,
            }
        ]
    )

    client = FakeEditClient()
    deleter = TgDeleter(config, repo, client, chat=object(), chat_id="1")
    result = await deleter.rename_file("Anime/Cache", "mk", "new.txt")
    assert result["logical_only"] is True
    member = repo.get_batch_member("Anime/Cache", "mk")
    assert member is not None
    assert member.orig_name == "new.txt"
    assert member.name_pinned == 1
    assert client.edits == []


@pytest.mark.asyncio
async def test_delete_folder_removes_orphan_batch_blob(tmp_path) -> None:
    """Folder whose only content is a batch blob with all members already
    logically deleted must still delete the blob message from the channel —
    otherwise reconcile resurrects the folder on the next launch.
    """
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    repo.upsert_folder("Anime/Cache")
    repo.upsert_msg_part(
        PartRecord(
            msg_id=501,
            chat_id="1",
            folder_path="Anime/Cache",
            file_key="blobk1",
            part_index=0,
            parts_total=1,
            orig_name="batch.zip",
            file_size=100,
            caption_raw='FC1|{"version":2,"kind":"tgccm_batch_blob","folder_path":"Anime/Cache","blob_key":"blobk1","orig_name":"batch.zip","members_count":2}',
            date_ts=1000,
        )
    )
    repo.rebuild_objects_aggregates()
    repo.upsert_batch_blob(
        blob_key="blobk1",
        folder_path="Anime/Cache",
        chat_id="1",
        msg_id=501,
        blob_name="batch.zip",
        blob_size=100,
        blob_sha256=None,
        manifest_json='{"members":[]}',
    )
    repo.upsert_batch_members_bulk(
        [
            {
                "folder_path": "Anime/Cache",
                "file_key": "m1",
                "blob_key": "blobk1",
                "orig_name": "a.txt",
                "member_index": 0,
                "member_size": 10,
                "member_sha256": "a" * 64,
            },
            {
                "folder_path": "Anime/Cache",
                "file_key": "m2",
                "blob_key": "blobk1",
                "orig_name": "b.txt",
                "member_index": 1,
                "member_size": 20,
                "member_sha256": "b" * 64,
            },
        ]
    )
    # Reproduce the stuck state: members logically gone, blob still alive.
    repo.mark_batch_member_deleted("Anime/Cache", "m1")
    repo.mark_batch_member_deleted("Anime/Cache", "m2")
    assert repo.list_objects_recursive("Anime/Cache") == []

    client = FakeDeleteClient()
    deleter = TgDeleter(config, repo, client, chat=object(), chat_id="1")
    result = await deleter.delete_folder("Anime/Cache")

    # Blob message removed from the channel.
    assert client.calls == [[501]]
    assert result["deleted"] == 1
    # Folder, blob and msg_index reference are all gone locally.
    assert all(f.folder_path != "Anime/Cache" for f in repo.list_folders())
    assert repo.list_batch_blob_keys_by_folder("Anime/Cache") == []
    blob_row = db.execute(
        "SELECT is_deleted FROM batch_blobs WHERE blob_key='blobk1'"
    ).fetchone()
    assert blob_row is None or int(blob_row["is_deleted"]) == 1
    live_msgs = db.execute(
        "SELECT COUNT(*) AS c FROM msg_index WHERE is_deleted=0"
    ).fetchone()
    assert int(live_msgs["c"]) == 0
