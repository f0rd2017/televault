from dataclasses import dataclass
from datetime import datetime

import pytest

from televault.core.types import AppConfig, CryptoConfig, PartMeta, RetryConfig
from televault.db.database import connect_db
from televault.db.repo import DbRepo
from televault.tg.parser import build_caption
from televault.tg.scan import TgScanner


@dataclass
class FakeFile:
    size: int
    name: str | None = None


@dataclass
class FakeMessage:
    id: int
    message: str
    date: datetime
    file: FakeFile | None = None


class FakeClient:
    def __init__(self, messages):
        self.messages = messages

    async def iter_messages(self, chat, search=None, min_id=0, limit=None):
        for msg in self.messages:
            if msg.id > min_id:
                yield msg


@pytest.mark.asyncio
async def test_refresh_incremental_indexes_parts(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        retry=RetryConfig(),
        crypto=CryptoConfig(),
    )
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)

    messages = [
        FakeMessage(
            id=1,
            message=build_caption(
                meta=PartMeta(
                    folder_path="Anime/Cache",
                    file_key="abc123",
                    part_index=0,
                    parts_total=1,
                    orig_name="file.bin",
                ),
                prefix="FC1|",
                extra={
                    "sha256": "x" * 64,
                    "orig_size": 42,
                    "part_size": 42,
                    "enc": False,
                },
            ),
            date=datetime.fromtimestamp(100),
            file=FakeFile(size=42),
        )
    ]
    scanner = TgScanner(config, repo, FakeClient(messages), chat=object(), chat_id="1")

    stats = await scanner.refresh_incremental()

    assert stats.indexed_parts == 1
    objects = repo.list_objects_by_folder("Anime/Cache")
    assert len(objects) == 1
    assert objects[0].status == "complete"


@pytest.mark.asyncio
async def test_reconcile_marks_missing_messages_deleted(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        retry=RetryConfig(),
        crypto=CryptoConfig(),
    )
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)

    first = FakeMessage(
        id=1,
        message=build_caption(
            meta=PartMeta(
                folder_path="Anime/Cache",
                file_key="obj111111111",
                part_index=0,
                parts_total=1,
                orig_name="a.bin",
            ),
            prefix="FC1|",
            extra={"sha256": "a" * 64, "orig_size": 1, "part_size": 1, "enc": False},
        ),
        date=datetime.fromtimestamp(100),
        file=FakeFile(size=1),
    )
    second = FakeMessage(
        id=2,
        message=build_caption(
            meta=PartMeta(
                folder_path="Anime/Cache",
                file_key="obj222222222",
                part_index=0,
                parts_total=1,
                orig_name="b.bin",
            ),
            prefix="FC1|",
            extra={"sha256": "b" * 64, "orig_size": 1, "part_size": 1, "enc": False},
        ),
        date=datetime.fromtimestamp(101),
        file=FakeFile(size=1),
    )

    client = FakeClient([first, second])
    scanner = TgScanner(config, repo, client, chat=object(), chat_id="1")

    stats = await scanner.refresh_incremental()
    assert stats.indexed_parts == 2
    assert len(repo.list_objects_by_folder("Anime/Cache")) == 2

    client.messages = [first]
    reconcile_stats = await scanner.reconcile()
    assert reconcile_stats.deleted_marked == 1

    objects = repo.list_objects_by_folder("Anime/Cache")
    assert len(objects) == 1
    assert objects[0].orig_name == "a.bin"


@pytest.mark.asyncio
async def test_refresh_full_indexes_unmanaged_file_messages(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        retry=RetryConfig(),
        crypto=CryptoConfig(),
    )
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)

    messages = [
        FakeMessage(
            id=11,
            message="plain caption without FC1 metadata",
            date=datetime.fromtimestamp(120),
            file=FakeFile(size=1234, name="manual_upload.iso"),
        )
    ]
    scanner = TgScanner(config, repo, FakeClient(messages), chat=object(), chat_id="1")
    stats = await scanner.refresh_full()

    assert stats.indexed_parts == 1
    imported = repo.list_objects_by_folder("Imported")
    assert len(imported) == 1
    assert imported[0].orig_name == "manual_upload.iso"
    assert imported[0].status == "complete"
