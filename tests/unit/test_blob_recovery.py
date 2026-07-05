"""Tests for batch-blob manifest recovery from zip tails."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.core.jobs import CancelToken
from app.core.types import AppConfig, BatchBlobCaption, PartRecord
from app.core.utils import sha256_file
from app.db.database import connect_db
from app.db.repo import DbRepo
from app.tg import compression
from app.tg.blob_recovery import (
    MANIFEST_ARC_NAME,
    _NeedMoreTail,
    parse_members_from_zip_tail,
    recover_blob_manifests,
)
from app.tg.parser import build_batch_blob_caption


@pytest.fixture()
def repo(tmp_path):
    return DbRepo(connect_db(tmp_path / "index.sqlite3"))


def _make_source_files(
    tmp_path: Path, count: int, repeat: int | None = None
) -> list[tuple[str, str]]:
    items = []
    for i in range(count):
        f = tmp_path / f"cat_{i}.mp4"
        f.write_bytes(f"video-data-{i}".encode() * (repeat or (50 + i)))
        items.append((str(f), "main/videos"))
    return items


def _build_archive(
    tmp_path: Path, count: int = 5, repeat: int | None = None
) -> tuple[Path, list[dict]]:
    (tmp_path / "src").mkdir(exist_ok=True)
    items = _make_source_files(tmp_path / "src", count, repeat)
    archive_path = tmp_path / "blob.zip"
    members = compression.build_group_archive(items, archive_path, CancelToken())
    return archive_path, members


def test_archive_embeds_manifest_as_last_sorted_entry(tmp_path):
    archive_path, members = _build_archive(tmp_path)
    with zipfile.ZipFile(archive_path) as zf:
        names = sorted(zf.namelist())
        assert names[-1] == MANIFEST_ARC_NAME
        # member entries still occupy indices 0..N-1 in sorted order
        assert names[: len(members)] == [m["archive_name"] for m in members]
        payload = json.loads(zf.read(MANIFEST_ARC_NAME))
    assert payload["members_count"] == len(members)
    assert all("source_path" not in m for m in payload["members"])
    assert payload["members"][0]["orig_name"] == "cat_0.mp4"
    assert len(payload["members"][0]["sha256"]) == 64


def test_parse_tail_uses_embedded_manifest(tmp_path):
    archive_path, members = _build_archive(tmp_path)
    data = archive_path.read_bytes()
    tail_start = max(0, len(data) - 8192)
    parsed, used_manifest = parse_members_from_zip_tail(
        data[tail_start:], tail_start, len(data), blob_folder="main/videos"
    )
    assert used_manifest is True
    assert [m["orig_name"] for m in parsed] == [m["orig_name"] for m in members]
    assert parsed[0]["sha256"] == members[0]["sha256"]
    assert parsed[0]["folder_path"] == "main/videos"


def test_parse_tail_reconstructs_from_central_directory(tmp_path):
    """Old blobs (uploaded before manifests were embedded) are reconstructed
    from NNNNN_-prefixed central directory entries."""
    archive_path = tmp_path / "old_blob.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("00001_cat_a.mp4", b"aaaa" * 100)
        zf.writestr("00002_cat_b.mp4", b"bbbb" * 200)
    data = archive_path.read_bytes()
    tail_start = max(0, len(data) - 4096)
    parsed, used_manifest = parse_members_from_zip_tail(
        data[tail_start:], tail_start, len(data), blob_folder="main/memes"
    )
    assert used_manifest is False
    assert [m["orig_name"] for m in parsed] == ["cat_a.mp4", "cat_b.mp4"]
    assert [m["member_index"] for m in parsed] == [0, 1]
    assert parsed[0]["size"] == 400
    assert parsed[1]["size"] == 800
    assert parsed[0]["folder_path"] == "main/memes"
    assert parsed[0]["sha256"] == ""


def test_parse_tail_raises_when_tail_too_short(tmp_path):
    archive_path, _ = _build_archive(tmp_path, count=30)
    data = archive_path.read_bytes()
    # Tail so short it cannot even contain the central directory
    tail_start = len(data) - 100
    with pytest.raises(_NeedMoreTail):
        parse_members_from_zip_tail(
            data[tail_start:], tail_start, len(data), blob_folder="f"
        )


class _FakeDocument:
    def __init__(self, data: bytes):
        self.size = len(data)
        self._data = data


class _FakeMessage:
    def __init__(self, document):
        self.document = document


class _FakeClient:
    """Serves iter_download / get_messages from an in-memory blob."""

    def __init__(self, blob_bytes: bytes, msg_id: int):
        self._doc = _FakeDocument(blob_bytes)
        self._msg_id = msg_id
        self.downloaded_bytes = 0

    async def get_messages(self, entity, ids):
        assert ids == self._msg_id
        return _FakeMessage(self._doc)

    async def iter_download(self, document, offset=0, request_size=512 * 1024):
        data = document._data[offset:]
        self.downloaded_bytes += len(data)
        for i in range(0, len(data), request_size):
            yield data[i : i + request_size]


@pytest.mark.asyncio
async def test_recover_blob_manifests_end_to_end(tmp_path, repo):
    config = AppConfig(
        tg_api_id=1, tg_api_hash="x", tg_session_path="s", cache_dir="/tmp"
    )
    # Blob must be bigger than the tail window (256 KB) plus the 1 MiB
    # offset alignment so the test proves only the tail is downloaded,
    # not the whole archive.
    archive_path, members = _build_archive(tmp_path, count=4, repeat=60_000)
    blob_bytes = archive_path.read_bytes()
    blob_key = sha256_file(archive_path)[:12]
    chat_id = "12345"
    msg_id = 777

    caption = build_batch_blob_caption(
        BatchBlobCaption(
            version=2,
            kind="tgccm_batch_blob",
            folder_path="main/videos",
            blob_key=blob_key,
            orig_name=archive_path.name,
            members_count=len(members),
        ),
        prefix=config.caption_prefix,
    )
    repo.upsert_msg_parts_bulk(
        [
            PartRecord(
                msg_id=msg_id,
                chat_id=chat_id,
                folder_path="main/videos",
                file_key=blob_key,
                part_index=0,
                parts_total=1,
                orig_name=archive_path.name,
                file_size=len(blob_bytes),
                caption_raw=caption,
                date_ts=1_700_000_000,
            )
        ]
    )

    client = _FakeClient(blob_bytes, msg_id)
    stats = await recover_blob_manifests(
        repo,
        config,
        client_by_chat_id={chat_id: client},
        chat_by_chat_id={chat_id: object()},
    )

    assert stats["orphans"] == 1
    assert stats["recovered"] == 1
    assert stats["failed"] == 0
    assert stats["members"] == len(members)

    blob = repo.get_batch_blob(blob_key)
    assert blob is not None
    assert blob.blob_size == len(blob_bytes)
    manifest = json.loads(blob.manifest_json)
    assert manifest["recovered"] is True
    assert manifest["recovered_source"] == "embedded_manifest"

    recovered_members = repo.list_batch_members_by_blob(blob_key)
    assert [m.orig_name for m in recovered_members] == [m["orig_name"] for m in members]
    # sha-based keys survive recovery when use_sha_as_key is on
    assert recovered_members[0].member_sha256 == members[0]["sha256"]
    # Tail download must be a tiny fraction of the blob
    assert client.downloaded_bytes < len(blob_bytes)

    # Second run is a no-op: nothing orphaned anymore
    stats2 = await recover_blob_manifests(
        repo,
        config,
        client_by_chat_id={chat_id: client},
        chat_by_chat_id={chat_id: object()},
    )
    assert stats2["orphans"] == 0


@pytest.mark.asyncio
async def test_recover_skips_unreachable_chat(tmp_path, repo):
    config = AppConfig(
        tg_api_id=1, tg_api_hash="x", tg_session_path="s", cache_dir="/tmp"
    )
    archive_path, members = _build_archive(tmp_path, count=2)
    blob_bytes = archive_path.read_bytes()
    caption = build_batch_blob_caption(
        BatchBlobCaption(
            version=2,
            kind="tgccm_batch_blob",
            folder_path="main/videos",
            blob_key="deadbeef0001",
            orig_name=archive_path.name,
            members_count=len(members),
        ),
        prefix=config.caption_prefix,
    )
    repo.upsert_msg_parts_bulk(
        [
            PartRecord(
                msg_id=1,
                chat_id="999",
                folder_path="main/videos",
                file_key="deadbeef0001",
                part_index=0,
                parts_total=1,
                orig_name=archive_path.name,
                file_size=len(blob_bytes),
                caption_raw=caption,
                date_ts=1_700_000_000,
            )
        ]
    )
    stats = await recover_blob_manifests(
        repo, config, client_by_chat_id={}, chat_by_chat_id={}
    )
    assert stats["orphans"] == 1
    assert stats["recovered"] == 0
    assert stats["failed"] == 1
    assert repo.get_batch_blob("deadbeef0001") is None
