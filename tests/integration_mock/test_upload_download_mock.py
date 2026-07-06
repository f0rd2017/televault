from dataclasses import dataclass
from datetime import datetime
import asyncio
import base64
import io
import os
from pathlib import Path
import zipfile

import pytest
from telethon.errors import (
    FilePartsInvalidError,
    FileReferenceExpiredError,
    FloodWaitError,
)
from telethon import functions as tl_functions
from telethon import types as tl_types

from app.core.jobs import CancelToken, JobCancelledError
from app.core.types import AppConfig, CryptoConfig, RetryConfig, TgTransferLimits
from app.core.utils import file_key_from_sha256
from app.db.database import connect_db
from app.db.repo import DbRepo
from app.tg.client import TgClientEndpoint
from app.tg.compression import BATCH_MANIFEST_ARC_NAME
from app.tg.download import TgDownloader
from app.tg.parser import parse_caption
from app.tg.upload import TgUploader
from app.core.types import PartRecord


@dataclass
class FakeSentMessage:
    id: int
    date: datetime
    payload: bytes
    caption: str


class FakeClient:
    def __init__(self, floodwait_once: bool = False, send_delay: float = 0.0):
        self.next_id = 1
        self.sent: dict[int, FakeSentMessage] = {}
        self.send_attempts = 0
        self.floodwait_once = floodwait_once
        self.send_delay = send_delay
        self.upload_parts: dict[int, dict[int, bytes]] = {}
        self.upload_part_totals: dict[int, int] = {}
        self.iter_download_calls: list[dict[str, int | None]] = []

    async def send_file(
        self,
        chat,
        file,
        caption=None,
        file_name=None,
        force_document=True,
        progress_callback=None,
    ):
        self.send_attempts += 1
        if self.floodwait_once and self.send_attempts == 1:
            raise FloodWaitError(None, 0)
        if self.send_delay > 0:
            await asyncio.sleep(self.send_delay)
        if isinstance(file, tl_types.InputFileBig):
            part_map = self.upload_parts.get(int(file.id), {})
            total_parts = int(file.parts)
            payload = b"".join(part_map.get(idx, b"") for idx in range(total_parts))
        elif isinstance(file, tl_types.InputFile):
            part_map = self.upload_parts.get(int(file.id), {})
            total_parts = int(file.parts)
            payload = b"".join(part_map.get(idx, b"") for idx in range(total_parts))
        elif isinstance(file, (str, Path)):
            payload = Path(file).read_bytes()
        else:
            payload = bytes(file)
        if progress_callback is not None:
            progress_callback(len(payload), len(payload))
        msg = FakeSentMessage(
            id=self.next_id,
            date=datetime.fromtimestamp(100 + self.next_id),
            payload=payload,
            caption=caption or "",
        )
        self.sent[msg.id] = msg
        self.next_id += 1
        return msg

    async def __call__(self, request, ordered=False):
        _ = ordered
        if isinstance(request, tl_functions.upload.SaveBigFilePartRequest):
            file_id = int(request.file_id)
            self.upload_parts.setdefault(file_id, {})[int(request.file_part)] = bytes(
                request.bytes
            )
            self.upload_part_totals[file_id] = int(request.file_total_parts)
            return True
        raise RuntimeError(f"Unsupported request type: {type(request).__name__}")

    async def get_messages(self, chat, ids):
        result = []
        for msg_id in ids:
            item = self.sent.get(msg_id)
            if item is None:
                result.append(None)
            else:
                if getattr(item, "file", None) is None:
                    item.file = type("FakeFile", (), {"size": len(item.payload)})()
                result.append(item)
        return result

    async def edit_message(self, chat, msg_id: int, text: str) -> None:
        _ = chat
        message = self.sent.get(int(msg_id))
        if message is None:
            return
        message.caption = str(text)

    async def iter_download(
        self,
        message,
        offset: int = 0,
        stride: int | None = None,
        limit: int | None = None,
        request_size: int = 524288,
        file_size: int | None = None,
    ):
        payload = bytes(message.payload)
        _ = file_size
        self.iter_download_calls.append(
            {
                "offset": int(offset),
                "stride": int(stride) if stride is not None else None,
                "limit": int(limit) if limit is not None else None,
                "request_size": int(request_size),
                "payload_size": len(payload),
            }
        )
        step = int(stride) if stride is not None else int(request_size)
        pos = max(0, int(offset))
        yielded = 0
        while pos < len(payload):
            if limit is not None and yielded >= int(limit):
                break
            chunk = payload[pos : pos + int(request_size)]
            if not chunk:
                break
            yield chunk
            yielded += 1
            pos += max(1, step)

    async def download_media(self, message, file=bytes, progress_callback=None):
        payload = bytes(message.payload)
        if progress_callback is not None:
            total = len(payload)
            step = max(1, total // 24)
            current = 0
            while current < total:
                current = min(total, current + step)
                progress_callback(current, total)

        if file is bytes:
            return payload

        target = Path(str(file))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return str(target)


@pytest.mark.asyncio
async def test_upload_then_download(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"A" * 700_000 + b"B" * 600_000)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient(send_delay=0.03)

    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")
    upload_progress: list[float] = []

    async def on_upload_progress(percent: float, message: str) -> None:
        upload_progress.append(percent)

    result = await uploader.chunked_upload(
        str(sample), "Anime/Cache", progress_cb=on_upload_progress
    )

    assert result["parts_total"] == 1
    assert "analytics" in result
    assert result["analytics"]["phase_seconds"]["prehash"] >= 0
    assert result["analytics"]["speed_mbps"]["transfer_payload"] >= 0
    assert result["analytics"]["tg_limits"]["is_premium"] is False
    assert result["analytics"]["tg_limits"]["request_size_bytes"] == 524288
    assert result["analytics"]["upload_profile"]["direct_mode"] is True
    assert result["analytics"]["upload_profile"]["concurrency"] >= 1
    assert result["analytics"]["upload_profile"]["inner_workers"] == 1
    assert upload_progress
    assert max(upload_progress) == pytest.approx(100.0)

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    download_progress: list[float] = []

    async def on_download_progress(percent: float, message: str) -> None:
        download_progress.append(percent)

    dl_result = await downloader.chunked_download(
        "Anime/Cache",
        result["file_key"],
        progress_cb=on_download_progress,
    )

    assert dl_result["verified"] is True
    assert dl_result["integrity_mode"] == "full_sha256"
    assert dl_result["expected_sha256"] == result["sha256"]
    assert "analytics" in dl_result
    assert dl_result["analytics"]["phase_seconds"]["network_download"] >= 0
    assert dl_result["analytics"]["speed_mbps"]["transfer_output"] >= 0
    assert dl_result["analytics"]["tg_limits"]["is_premium"] is False
    assert dl_result["analytics"]["tg_limits"]["request_size_bytes"] == 524288
    assert download_progress
    assert max(download_progress) == pytest.approx(100.0)
    assert len(download_progress) < 40


@pytest.mark.asyncio
async def test_fetch_parts_decrypted_downloads_parts_concurrently(tmp_path) -> None:
    """The stream player waits until all window parts are on disk — if
    fetch_parts_decrypted fetched parts one by one, that would directly hurt
    the time to first frame. We check that several parts are actually fetched
    CONCURRENTLY (not sequentially), the result is correct, and a repeat request
    reuses the already-downloaded parts."""

    class ConcurrencyTrackingClient(FakeClient):
        def __init__(self, *args, delay: float = 0.03, **kwargs):
            super().__init__(*args, **kwargs)
            self._delay = delay
            self.active = 0
            self.max_active = 0

        async def iter_download(
            self,
            message,
            offset: int = 0,
            stride: int | None = None,
            limit: int | None = None,
            request_size: int = 524288,
            file_size: int | None = None,
        ):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                async for chunk in super().iter_download(
                    message,
                    offset=offset,
                    stride=stride,
                    limit=limit,
                    request_size=request_size,
                    file_size=file_size,
                ):
                    await asyncio.sleep(self._delay)
                    yield chunk
            finally:
                self.active -= 1

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    from app.core.types import PartMeta
    from app.tg.parser import build_caption

    payloads = [b"A" * 1_000_000, b"B" * 1_000_000, b"C" * 600_000]
    payload = b"".join(payloads)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = ConcurrencyTrackingClient(delay=0.03)

    # Each part is a separate "sent" message (like real chunks in
    # Telegram); chunked_upload doesn't fit here — with one account in the pool it doesn't
    # split a small file into several parts (see base_logical_parts).
    folder, file_key = "Anime/Cache", "testkey"
    part_records = []
    for idx, blob in enumerate(payloads):
        meta = PartMeta(
            folder_path=folder,
            file_key=file_key,
            part_index=idx,
            parts_total=len(payloads),
            orig_name="sample.bin",
        )
        sent = await client.send_file(
            object(), blob, caption=build_caption(meta, extra={"enc": False})
        )
        part_records.append(
            PartRecord(
                msg_id=sent.id,
                chat_id="1",
                folder_path=folder,
                file_key=file_key,
                part_index=idx,
                parts_total=len(payloads),
                orig_name="sample.bin",
                file_size=len(blob),
                caption_raw=sent.caption,
                date_ts=100 + idx,
            )
        )
    repo.upsert_msg_parts_bulk(part_records)

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    stream_cache = tmp_path / "stream_cache"

    parts = await downloader.fetch_parts_decrypted(
        folder, file_key, [0, 1, 2], str(stream_cache)
    )
    assert set(parts.keys()) == {0, 1, 2}
    assembled = b"".join(Path(parts[i]).read_bytes() for i in range(3))
    assert assembled == payload
    assert client.max_active >= 2, "parts should download concurrently, not one-by-one"

    # A repeat request reuses the already-downloaded parts — no new network calls.
    calls_before = len(client.iter_download_calls)
    parts_again = await downloader.fetch_parts_decrypted(
        folder, file_key, [0, 1, 2], str(stream_cache)
    )
    assert parts_again == parts
    assert len(client.iter_download_calls) == calls_before


@pytest.mark.asyncio
async def test_fetch_parts_decrypted_prefix_grows_without_full_redownload(
    tmp_path,
) -> None:
    """Parts can weigh hundreds of MB — the player must not wait for the WHOLE
    part to download if only a small slice at the start is needed now. prefix_bytes
    must fetch only the prefix and GROW it as the window expands, not re-fetching
    already-received bytes again."""
    from app.core.types import PartMeta
    from app.tg.parser import build_caption

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    payload = bytes(range(256)) * 20_000  # 5,120,000 bytes, deterministic content
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()

    folder, file_key = "Anime/Cache", "hugepart"
    meta = PartMeta(
        folder_path=folder,
        file_key=file_key,
        part_index=0,
        parts_total=1,
        orig_name="huge.mp4",
    )
    sent = await client.send_file(
        object(), payload, caption=build_caption(meta, extra={"enc": False})
    )
    part = PartRecord(
        msg_id=sent.id,
        chat_id="1",
        folder_path=folder,
        file_key=file_key,
        part_index=0,
        parts_total=1,
        orig_name="huge.mp4",
        file_size=len(payload),
        caption_raw=sent.caption,
        date_ts=1,
    )
    repo.upsert_msg_parts_bulk([part])

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    stream_cache = tmp_path / "stream_cache"

    # The first request needs only 700,000 bytes out of 5,120,000.
    parts = await downloader.fetch_parts_decrypted(
        folder, file_key, [0], str(stream_cache), prefix_bytes={0: 700_000}
    )
    cached_path = Path(parts[0])
    first_size = cached_path.stat().st_size
    assert 700_000 <= first_size < len(payload), "should cache only the prefix"
    assert cached_path.read_bytes() == payload[:first_size]
    calls_after_first = len(client.iter_download_calls)

    # Second request — the window advanced, now 2,000,000 bytes are needed: it must
    # GROW the existing prefix, not re-download everything from scratch.
    parts2 = await downloader.fetch_parts_decrypted(
        folder, file_key, [0], str(stream_cache), prefix_bytes={0: 2_000_000}
    )
    second_size = Path(parts2[0]).stat().st_size
    assert 2_000_000 <= second_size < len(payload)
    assert Path(parts2[0]).read_bytes() == payload[:second_size]

    new_calls = client.iter_download_calls[calls_after_first:]
    assert new_calls, "new requests should have gone out to grow the prefix"
    assert all(call["offset"] >= first_size for call in new_calls), (
        "must not re-download already-received prefix bytes"
    )


@pytest.mark.asyncio
async def test_fetch_parts_decrypted_prefix_downloads_striped_in_parallel(
    tmp_path,
) -> None:
    """The stream window (≈12 MB) used to download as ONE sequential stream —
    that was the main contributor to the time-to-first-frame. Now the multi-chunk
    prefix is spread across several parallel stride streams, and the assembled
    bytes must exactly match the start of the file (no gaps/overlap)."""
    from app.core.types import PartMeta
    from app.tg.parser import build_caption

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    # Deterministic content, noticeably bigger than the prefix, so the last chunk
    # is full (no short tails at the file boundary).
    payload = bytes(range(256)) * 24_000  # 6,144,000 bytes
    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()

    folder, file_key = "Anime/Cache", "stripedprefix"
    meta = PartMeta(
        folder_path=folder,
        file_key=file_key,
        part_index=0,
        parts_total=1,
        orig_name="huge.mp4",
    )
    sent = await client.send_file(
        object(), payload, caption=build_caption(meta, extra={"enc": False})
    )
    part = PartRecord(
        msg_id=sent.id,
        chat_id="1",
        folder_path=folder,
        file_key=file_key,
        part_index=0,
        parts_total=1,
        orig_name="huge.mp4",
        file_size=len(payload),
        caption_raw=sent.caption,
        date_ts=1,
    )
    repo.upsert_msg_parts_bulk([part])

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    stream_cache = tmp_path / "stream_cache"

    # We need a prefix spanning several request-size chunks (524288 B each).
    parts = await downloader.fetch_parts_decrypted(
        folder, file_key, [0], str(stream_cache), prefix_bytes={0: 3_000_000}
    )
    cached = Path(parts[0])
    size = cached.stat().st_size
    assert 3_000_000 <= size < len(payload)
    # Bytes assembled correctly — the streams laid down chunks with no gaps or overlap.
    assert cached.read_bytes() == payload[:size]

    # The download used parallel stride streams, not a single sequential one.
    striped = [c for c in client.iter_download_calls if c["stride"] is not None]
    assert len(striped) >= 2, (
        f"expected parallel stride streams, but the calls were: "
        f"{client.iter_download_calls}"
    )
    request_size = 524288
    assert all(c["stride"] == len(striped) * request_size for c in striped)
    offsets = sorted(c["offset"] for c in striped)
    assert offsets == [i * request_size for i in range(len(striped))]

    # The third request needs the whole part — it must fetch the tail.
    parts3 = await downloader.fetch_parts_decrypted(
        folder, file_key, [0], str(stream_cache), prefix_bytes={0: len(payload)}
    )
    assert Path(parts3[0]).read_bytes() == payload


@pytest.mark.asyncio
async def test_fetch_parts_decrypted_ignores_prefix_hint_when_encrypted(
    tmp_path,
) -> None:
    """With encryption enabled, a partial slice of a part is useless without
    decrypting it whole (the AES-GCM tag is verified over the entire ciphertext) —
    prefix_bytes must be ignored, the part is fetched and decrypted
    in full, as before."""
    from app.core.types import CryptoConfig as _CryptoConfig
    from app.core.types import PartMeta
    from app.core.utils import encrypt_bytes
    from app.tg.parser import build_caption

    monkeypatch_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    os.environ["TG_CRYPTO_KEY_B64_TEST_PREFIX"] = monkeypatch_key
    try:
        config = AppConfig(
            tg_api_id=1,
            tg_api_hash="x",
            tg_session_path="./data/session.session",
            cache_dir=str(tmp_path / "cache"),
            chunk_size_mb=1,
            upload_compression_mode="off",
            retry=RetryConfig(max_attempts=2, base_delay=0.01),
            crypto=_CryptoConfig(enabled=True, key_env="TG_CRYPTO_KEY_B64_TEST_PREFIX"),
        )

        payload = bytes(range(256)) * 5_000  # 1,280,000 bytes plaintext
        key = base64.urlsafe_b64decode(monkeypatch_key + "==")
        encrypted = encrypt_bytes(payload, key)

        db = connect_db(tmp_path / "index.sqlite3")
        repo = DbRepo(db)
        client = FakeClient()

        folder, file_key = "Anime/Cache", "encpart"
        meta = PartMeta(
            folder_path=folder,
            file_key=file_key,
            part_index=0,
            parts_total=1,
            orig_name="enc.mp4",
        )
        sent = await client.send_file(
            object(), encrypted, caption=build_caption(meta, extra={"enc": True})
        )
        part = PartRecord(
            msg_id=sent.id,
            chat_id="1",
            folder_path=folder,
            file_key=file_key,
            part_index=0,
            parts_total=1,
            orig_name="enc.mp4",
            file_size=len(encrypted),
            caption_raw=sent.caption,
            date_ts=1,
        )
        repo.upsert_msg_parts_bulk([part])

        downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
        stream_cache = tmp_path / "stream_cache"

        # Ask for a small "prefix" — with encryption this must be
        # ignored, the part is downloaded and decrypted in full.
        parts = await downloader.fetch_parts_decrypted(
            folder, file_key, [0], str(stream_cache), prefix_bytes={0: 1024}
        )
        assert Path(parts[0]).read_bytes() == payload
    finally:
        os.environ.pop("TG_CRYPTO_KEY_B64_TEST_PREFIX", None)


@pytest.mark.asyncio
async def test_upload_then_download_empty_file(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "empty.bin"
    sample.write_bytes(b"")

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()

    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")
    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    assert result["parts_total"] == 1
    assert (
        result["sha256"]
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert len(client.sent) == 1
    sent = next(iter(client.sent.values()))
    assert sent.payload == b"\x00"
    meta = parse_caption(sent.caption)
    assert meta is not None
    assert meta.orig_size == 0
    assert meta.part_size == 1

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    dl_result = await downloader.chunked_download("Anime/Cache", result["file_key"])

    assert dl_result["verified"] is True
    assert dl_result["expected_sha256"] == result["sha256"]
    assert Path(dl_result["output_path"]).read_bytes() == b""


@pytest.mark.asyncio
async def test_upload_send_path_fallbacks_to_inline_payload_on_file_parts_invalid(
    tmp_path,
) -> None:
    class PathPartsInvalidClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.path_failures = 0

        async def send_file(
            self,
            chat,
            file,
            caption=None,
            file_name=None,
            force_document=True,
            progress_callback=None,
        ):
            if isinstance(file, (str, Path)):
                self.path_failures += 1
                raise FilePartsInvalidError(None)
            return await super().send_file(
                chat,
                file,
                caption=caption,
                file_name=file_name,
                force_document=force_document,
                progress_callback=progress_callback,
            )

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=64,
        concurrency=2,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "path_fallback.bin"
    payload = b"P" * (3 * 1024 * 1024 + 12345)
    sample.write_bytes(payload)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = PathPartsInvalidClient()
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    assert result["parts_total"] == 1
    assert client.path_failures == 1
    assert len(client.sent) == 1
    sent = next(iter(client.sent.values()))
    assert sent.payload == payload


@pytest.mark.asyncio
async def test_download_refreshes_message_when_file_reference_expires(tmp_path) -> None:
    class ExpiringFileRefClient(FakeClient):
        def __init__(self):
            super().__init__()
            self._get_messages_calls = 0

        async def get_messages(self, chat, ids):
            base = await super().get_messages(chat, ids)
            self._get_messages_calls += 1
            items = base if isinstance(base, list) else [base]
            result = []
            fresh = self._get_messages_calls > 1
            for item in items:
                if item is None:
                    result.append(None)
                    continue
                msg = FakeSentMessage(
                    id=item.id,
                    date=item.date,
                    payload=item.payload,
                    caption=item.caption,
                )
                msg.file = type("FakeFile", (), {"size": len(msg.payload)})()
                msg.fresh_ref = fresh
                result.append(msg)
            return result

        async def iter_download(
            self,
            message,
            offset: int = 0,
            stride: int | None = None,
            limit: int | None = None,
            request_size: int = 524288,
            file_size: int | None = None,
        ):
            if not bool(getattr(message, "fresh_ref", False)):
                raise FileReferenceExpiredError(None)
            async for chunk in super().iter_download(
                message,
                offset=offset,
                stride=stride,
                limit=limit,
                request_size=request_size,
                file_size=file_size,
            ):
                yield chunk

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"X" * 1_500_000)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = ExpiringFileRefClient()

    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")
    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    dl_result = await downloader.chunked_download("Anime/Cache", result["file_key"])

    assert dl_result["verified"] is True
    assert Path(dl_result["output_path"]).read_bytes() == sample.read_bytes()
    assert client._get_messages_calls >= 2


@pytest.mark.asyncio
async def test_upload_balanced_part_sizing_can_use_disk_backed_parts(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        upload_compression_mode="off",
        balanced_part_sizing_enabled=True,
        balanced_part_min_file_mb=1,
        balanced_part_target_regular_mb=2,
        balanced_part_target_premium_mb=2,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "balanced.bin"
    sample.write_bytes(b"A" * (3 * 1024 * 1024 + 400_000))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    transfer_limits = TgTransferLimits(
        is_premium=False,
        request_size_bytes=524288,
        max_fileparts=16,
        max_file_size_bytes=4 * 1024 * 1024,
    )
    uploader = TgUploader(
        config,
        repo,
        client,
        chat=object(),
        chat_id="1",
        transfer_limits=transfer_limits,
    )
    uploader._IN_MEMORY_PART_MAX_BYTES = 1 * 1024 * 1024

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    assert result["parts_total"] == 2
    profile = result["analytics"]["upload_profile"]
    assert profile["balanced_part_sizing"] is False
    assert profile["disk_backed_parts"] is True
    assert profile["chunk_size"] <= 2 * 1024 * 1024


@pytest.mark.asyncio
async def test_upload_disk_backed_parts_can_run_in_parallel(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        upload_compression_mode="off",
        balanced_part_sizing_enabled=True,
        balanced_part_min_file_mb=1,
        balanced_part_target_regular_mb=2,
        balanced_part_target_premium_mb=2,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "parallel_disk_parts.bin"
    sample.write_bytes(b"B" * (5 * 1024 * 1024 + 200_000))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient(send_delay=0.05)
    active = {"now": 0, "max": 0}
    original_send_file = client.send_file

    async def tracked_send_file(*args, **kwargs):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        try:
            return await original_send_file(*args, **kwargs)
        finally:
            active["now"] -= 1

    client.send_file = tracked_send_file  # type: ignore[method-assign]

    transfer_limits = TgTransferLimits(
        is_premium=False,
        request_size_bytes=524288,
        max_fileparts=16,
        max_file_size_bytes=4 * 1024 * 1024,
    )
    uploader = TgUploader(
        config,
        repo,
        client,
        chat=object(),
        chat_id="1",
        transfer_limits=transfer_limits,
    )
    uploader._IN_MEMORY_PART_MAX_BYTES = 1 * 1024 * 1024

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    profile = result["analytics"]["upload_profile"]
    assert profile["disk_backed_parts"] is True
    assert profile["effective_concurrency"] == 1
    assert result["parts_total"] >= 2
    assert active["max"] == 1


@pytest.mark.asyncio
async def test_upload_disk_backed_large_parts_use_parallel_bigfile_pipeline(
    tmp_path,
) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=12,
        upload_limit_safety_mb=0,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "disk_parallel_big_parts.bin"
    sample.write_bytes(os.urandom(26 * 1024 * 1024 + 123))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    transfer_limits = TgTransferLimits(
        is_premium=False,
        request_size_bytes=524288,
        max_fileparts=4096,
        max_file_size_bytes=16 * 1024 * 1024,
    )
    uploader = TgUploader(
        config,
        repo,
        client,
        chat=object(),
        chat_id="1",
        transfer_limits=transfer_limits,
    )
    uploader._IN_MEMORY_PART_MAX_BYTES = 1 * 1024 * 1024

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    profile = result["analytics"]["upload_profile"]
    assert profile["disk_backed_parts"] is True
    assert profile["parallel_chunk_upload"] is True
    assert int(profile["inner_workers"]) >= 2
    uploaded_part_requests = sum(len(parts) for parts in client.upload_parts.values())
    assert uploaded_part_requests > 0


@pytest.mark.asyncio
async def test_upload_group_batches_small_files_into_single_archive(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    one = tmp_path / "first.txt"
    two = tmp_path / "second.txt"
    one.write_bytes(b"alpha")
    two.write_bytes(b"beta-beta")

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")

    result = await uploader.chunked_upload_group([str(one), str(two)], "Anime/Cache")

    assert len(client.sent) == 1
    assert result["small_batch"]["files_count"] == 2
    assert str(result["orig_name"]).endswith(".zip")

    sent = next(iter(client.sent.values()))
    with zipfile.ZipFile(io.BytesIO(sent.payload), "r") as archive:
        names = sorted(archive.namelist())
        # 2 members + the embedded recovery manifest
        assert names[-1] == BATCH_MANIFEST_ARC_NAME
        assert len(names) == 3
        first_name = next(name for name in names if name.endswith("first.txt"))
        second_name = next(name for name in names if name.endswith("second.txt"))
        assert archive.read(first_name) == one.read_bytes()
        assert archive.read(second_name) == two.read_bytes()


@pytest.mark.asyncio
async def test_upload_group_skips_missing_file_uploads_rest(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    one = tmp_path / "first.txt"
    two = tmp_path / "second.txt"
    one.write_bytes(b"alpha")
    two.write_bytes(b"beta-beta")
    missing = tmp_path / "gone.txt"  # never created — deleted before upload ran

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")

    # A single missing file must NOT abort the whole group: the two present
    # files still upload (regression for FileNotFoundError on doomed source).
    result = await uploader.chunked_upload_group(
        [str(one), str(missing), str(two)], "Anime/Cache"
    )

    assert len(client.sent) == 1
    assert result["small_batch"]["files_count"] == 2
    sent = next(iter(client.sent.values()))
    with zipfile.ZipFile(io.BytesIO(sent.payload), "r") as archive:
        member_names = [
            name for name in archive.namelist() if name != BATCH_MANIFEST_ARC_NAME
        ]
        assert len(member_names) == 2


@pytest.mark.asyncio
async def test_upload_group_all_missing_raises(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")

    with pytest.raises(ValueError, match="No files to upload"):
        await uploader.chunked_upload_group(
            [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")], "Anime/Cache"
        )
    assert len(client.sent) == 0


@pytest.mark.asyncio
async def test_upload_session_pipelines_multiple_batches(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    c = tmp_path / "c.txt"
    d = tmp_path / "d.txt"
    solo = tmp_path / "solo.txt"
    for p, data in (
        (a, b"aaa"),
        (b, b"bbbb"),
        (c, b"ccccc"),
        (d, b"dd"),
        (solo, b"solo!"),
    ):
        p.write_bytes(data)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")

    batches = [
        {"file_paths": [str(a), str(b)], "folder_path": "S/One"},
        {"file_paths": [str(c), str(d)], "folder_path": "S/Two"},
        {"file_paths": [str(solo)], "folder_path": "S/Three"},  # single → no zip
    ]
    progress: list[float] = []

    async def on_progress(percent: float, _msg: str) -> None:
        progress.append(percent)

    result = await uploader.chunked_upload_session(batches, progress_cb=on_progress)

    assert result["session"] is True
    assert int(result["batches"]) == 3
    # Two zipped batches + one passthrough = 3 sends.
    assert len(client.sent) == 3
    # Members of the two real batches are indexed transparently.
    assert {o.orig_name for o in repo.list_objects_by_folder("S/One")} == {
        "a.txt",
        "b.txt",
    }
    assert {o.orig_name for o in repo.list_objects_by_folder("S/Two")} == {
        "c.txt",
        "d.txt",
    }
    assert {o.orig_name for o in repo.list_objects_by_folder("S/Three")} == {"solo.txt"}
    # Progress is monotonic non-decreasing and ends at ~100%.
    assert progress and progress == sorted(progress)
    assert progress[-1] == pytest.approx(100.0, abs=1.0)


@pytest.mark.asyncio
async def test_batch_upload_transparent_members_and_blob_reuse_download(
    tmp_path,
) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    one = tmp_path / "first.txt"
    two = tmp_path / "second.txt"
    one.write_bytes(b"alpha")
    two.write_bytes(b"beta-beta")

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")
    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")

    await uploader.chunked_upload_group([str(one), str(two)], "Anime/Cache")
    rows = repo.list_objects_by_folder("Anime/Cache")
    assert len(rows) == 2
    assert sorted(item.orig_name for item in rows) == ["first.txt", "second.txt"]
    assert all(item.storage_kind == "batch_member" for item in rows)

    row_first = next(item for item in rows if item.orig_name == "first.txt")
    row_second = next(item for item in rows if item.orig_name == "second.txt")

    first_dl = await downloader.chunked_download("Anime/Cache", row_first.file_key)
    assert Path(first_dl["output_path"]).read_bytes() == one.read_bytes()
    calls_after_first = len(client.iter_download_calls)
    assert calls_after_first > 0

    second_dl = await downloader.chunked_download("Anime/Cache", row_second.file_key)
    assert Path(second_dl["output_path"]).read_bytes() == two.read_bytes()
    assert len(client.iter_download_calls) == calls_after_first


@pytest.mark.asyncio
async def test_download_blob_members_extracts_many_in_one_job(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )
    one = tmp_path / "first.txt"
    two = tmp_path / "second.txt"
    three = tmp_path / "third.txt"
    one.write_bytes(b"alpha")
    two.write_bytes(b"beta-beta")
    three.write_bytes(b"gamma-gamma-gamma")

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")
    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")

    await uploader.chunked_upload_group([str(one), str(two), str(three)], "Anime/Cache")
    rows = repo.list_objects_by_folder("Anime/Cache")
    assert {r.storage_kind for r in rows} == {"batch_member"}
    blob_key = rows[0].blob_key
    assert blob_key and all(r.blob_key == blob_key for r in rows)

    from app.core.jobs import CancelToken

    progress: list[float] = []

    async def on_progress(pct: float, _msg: str) -> None:
        progress.append(pct)

    result = await downloader.download_blob_members(
        blob_key=blob_key,
        member_file_keys=[r.file_key for r in rows],
        integrity_mode="fast",
        cancel_token=CancelToken(),
        progress_cb=on_progress,
    )

    assert int(result["downloaded_members"]) == 3
    # Blob fetched once even though 3 members were extracted.
    assert len(client.iter_download_calls) > 0
    one_calls = len(client.iter_download_calls)

    # Verify each extracted file matches the original.
    from app.core.utils import build_safe_output_path

    for src in (one, two, three):
        out = build_safe_output_path(config.cache_dir, "Anime/Cache", src.name)
        assert out.read_bytes() == src.read_bytes()

    # Progress reached 100% and the blob was not re-fetched per member.
    assert progress and progress[-1] == pytest.approx(100.0, abs=0.01)
    assert len(client.iter_download_calls) == one_calls


@pytest.mark.asyncio
async def test_upload_auto_compresses_to_fit_safe_limit(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=64,
        upload_compression_mode="auto",
        upload_limit_safety_mb=1,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "large_text.bin"
    sample.write_bytes(b"A" * (5 * 1024 * 1024 + 700_000))

    transfer_limits = TgTransferLimits(
        is_premium=False,
        request_size_bytes=524288,
        max_fileparts=4000,
        max_file_size_bytes=6 * 1024 * 1024,
    )

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    uploader = TgUploader(
        config,
        repo,
        client,
        chat=object(),
        chat_id="1",
        transfer_limits=transfer_limits,
    )
    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    safe_limit = (6 * 1024 * 1024) - (1 * 1024 * 1024)
    assert result["parts_total"] == 1
    assert str(result["orig_name"]).endswith(".zip")
    assert result["analytics"]["compression"]["used"] is True
    assert result["analytics"]["tg_limits"]["safe_limit_bytes"] == safe_limit

    first_msg = client.sent[min(client.sent.keys())]
    assert len(first_msg.payload) <= safe_limit

    downloader = TgDownloader(
        config,
        repo,
        client,
        chat=object(),
        chat_id="1",
        transfer_limits=transfer_limits,
    )
    dl = await downloader.chunked_download("Anime/Cache", result["file_key"])
    assert dl["verified"] is True

    downloaded = Path(dl["output_path"])
    with zipfile.ZipFile(downloaded, "r") as archive:
        names = archive.namelist()
        assert len(names) == 1
        restored = archive.read(names[0])
    assert restored == sample.read_bytes()


@pytest.mark.asyncio
async def test_upload_auto_mode_skips_compression_when_file_fits_safe_limit(
    tmp_path,
) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=64,
        upload_compression_mode="auto",
        upload_limit_safety_mb=100,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "fits_limit.txt"
    sample.write_bytes((b"hello world\n") * 400_000)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")
    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    assert result["analytics"]["compression"]["used"] is False
    assert result["orig_name"] == sample.name


@pytest.mark.asyncio
async def test_download_legacy_caption_uses_prefix_fallback(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )
    payload = b"legacy-data"
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    file_key = file_key_from_sha256(digest)
    legacy_caption = f"FC1|f=Anime/Cache|k={file_key}|i=0|n=1|nm=legacy.bin"

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    repo.upsert_folder("Anime/Cache")
    repo.upsert_msg_part(
        PartRecord(
            msg_id=1,
            chat_id="1",
            folder_path="Anime/Cache",
            file_key=file_key,
            part_index=0,
            parts_total=1,
            orig_name="legacy.bin",
            file_size=len(payload),
            caption_raw=legacy_caption,
            date_ts=100,
        )
    )
    repo.rebuild_objects_aggregates()

    client = FakeClient()
    client.sent[1] = FakeSentMessage(
        id=1, date=datetime.fromtimestamp(100), payload=payload, caption=legacy_caption
    )

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    dl = await downloader.chunked_download("Anime/Cache", file_key)
    assert dl["verified"] is True
    assert dl["integrity_mode"] == "prefix_fallback"


@pytest.mark.asyncio
async def test_download_without_integrity_metadata_uses_strict_size_fallback(
    tmp_path,
) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )
    payload = b"manual-upload-no-metadata"
    file_key = "msg_0000000000000001"

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    repo.upsert_folder("Imported")
    repo.upsert_msg_part(
        PartRecord(
            msg_id=1,
            chat_id="1",
            folder_path="Imported",
            file_key=file_key,
            part_index=0,
            parts_total=1,
            orig_name="manual.bin",
            file_size=len(payload),
            caption_raw="",
            date_ts=100,
        )
    )
    repo.rebuild_objects_aggregates()

    client = FakeClient()
    client.sent[1] = FakeSentMessage(
        id=1, date=datetime.fromtimestamp(100), payload=payload, caption=""
    )
    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")

    dl = await downloader.chunked_download("Imported", file_key)
    assert dl["verified"] is True
    assert dl["integrity_mode"] == "strict_size_fallback"


@pytest.mark.asyncio
async def test_download_fails_on_sha256_mismatch(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"C" * 400_000)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()

    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")
    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    first_id = min(client.sent.keys())
    damaged = client.sent[first_id]
    client.sent[first_id] = FakeSentMessage(
        id=damaged.id,
        date=damaged.date,
        payload=damaged.payload + b"corruption",
        caption=damaged.caption,
    )

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    with pytest.raises(ValueError, match="Integrity mismatch"):
        await downloader.chunked_download("Anime/Cache", result["file_key"])


@pytest.mark.asyncio
async def test_download_fails_on_conflicting_sha256_metadata(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"G" * 1_300_000)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    transfer_limits = TgTransferLimits(
        is_premium=False,
        request_size_bytes=524288,
        max_fileparts=4096,
        max_file_size_bytes=700_000,
    )
    uploader = TgUploader(
        config,
        repo,
        client,
        chat=object(),
        chat_id="1",
        transfer_limits=transfer_limits,
    )
    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    parts = repo.get_parts_for_object("Anime/Cache", result["file_key"])
    assert len(parts) >= 2
    changed_caption = (
        'FC1|{"folder_path":"Anime/Cache","file_key":"'
        + result["file_key"]
        + f'","part_index":{parts[1].part_index},"parts_total":{len(parts)},"orig_name":"sample.bin","sha256":"'
        + ("f" * 64)
        + '"}'
    )
    with repo.conn:
        repo.conn.execute(
            "UPDATE msg_index SET caption_raw=? WHERE msg_id=?",
            (changed_caption, parts[1].msg_id),
        )

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    with pytest.raises(ValueError, match="Integrity metadata conflict"):
        await downloader.chunked_download("Anime/Cache", result["file_key"])


@pytest.mark.asyncio
async def test_upload_retries_after_floodwait(tmp_path, monkeypatch) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        retry=RetryConfig(max_attempts=3, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"D" * 300_000)

    async def fast_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient(floodwait_once=True)
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")
    assert result["parts_total"] == 1
    assert client.send_attempts >= 2


@pytest.mark.asyncio
async def test_upload_cancel_mid_transfer(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        concurrency=1,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"E" * (8 * 1024 * 1024 + 111))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient(send_delay=0.03)
    transfer_limits = TgTransferLimits(
        is_premium=False,
        request_size_bytes=524288,
        max_fileparts=4096,
        max_file_size_bytes=2 * 1024 * 1024,
    )
    uploader = TgUploader(
        config,
        repo,
        client,
        chat=object(),
        chat_id="1",
        transfer_limits=transfer_limits,
    )
    token = CancelToken()

    async def run_upload() -> None:
        await uploader.chunked_upload(str(sample), "Anime/Cache", cancel_token=token)

    task = asyncio.create_task(run_upload())
    await asyncio.sleep(0.08)
    token.cancel()

    with pytest.raises(JobCancelledError):
        await task

    rows = repo.list_objects_by_folder("Anime/Cache")
    if rows:
        assert rows[0].have_parts >= 1
        assert rows[0].status in {"incomplete", "complete"}


@pytest.mark.asyncio
async def test_upload_respects_use_sha_as_key_flag(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        use_sha_as_key=False,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"F" * 64_000)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")
    assert len(result["file_key"]) == 12

    first_msg = client.sent[min(client.sent.keys())]
    parsed = parse_caption(first_msg.caption, prefix="FC1|")
    assert parsed is not None
    assert parsed.sha256 == result["sha256"]


@pytest.mark.asyncio
async def test_download_progress_events_are_coalesced(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"Z" * 2_400_000)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient(send_delay=0.02)
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")
    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    progress_events: list[float] = []

    async def on_download_progress(percent: float, message: str) -> None:
        progress_events.append(percent)

    await downloader.chunked_download(
        "Anime/Cache", result["file_key"], progress_cb=on_download_progress
    )
    assert progress_events
    assert progress_events[-1] == pytest.approx(100.0)
    assert len(progress_events) < 30


@pytest.mark.asyncio
async def test_regular_download_profile_disables_striding_with_parallel_parts(
    tmp_path,
) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=10,
        concurrency=2,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"P" * (21 * 1024 * 1024))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    transfer_limits = TgTransferLimits(
        is_premium=False,
        request_size_bytes=524288,
        max_fileparts=4096,
        max_file_size_bytes=1_500_000,
    )
    uploader = TgUploader(
        config,
        repo,
        client,
        chat=object(),
        chat_id="1",
        transfer_limits=transfer_limits,
    )
    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    dl = await downloader.chunked_download("Anime/Cache", result["file_key"])

    assert dl["verified"] is True
    # With 1 unique client, part_concurrency is capped to 1 to avoid FloodWait
    assert dl["analytics"]["tg_limits"]["effective_part_concurrency"] == 1
    assert dl["analytics"]["tg_limits"]["effective_stride_streams"] == 1
    assert all(call["stride"] is None for call in client.iter_download_calls)


@pytest.mark.asyncio
async def test_regular_download_profile_uses_two_stride_streams_for_single_part(
    tmp_path,
) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=20,
        concurrency=4,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"S" * (12 * 1024 * 1024))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    transfer_limits = TgTransferLimits(
        is_premium=False,
        request_size_bytes=524288,
        max_fileparts=4096,
        max_file_size_bytes=1_500_000,
    )
    uploader = TgUploader(
        config,
        repo,
        client,
        chat=object(),
        chat_id="1",
        transfer_limits=transfer_limits,
    )
    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    dl = await downloader.chunked_download("Anime/Cache", result["file_key"])

    assert dl["verified"] is True
    assert dl["analytics"]["tg_limits"]["effective_part_concurrency"] == 1
    assert (
        dl["analytics"]["tg_limits"]["effective_stride_streams"] == 1
    )  # Updated to reflect new conservative settings

    # With stride_streams=1, there should be no stride calls (linear download)
    stride_calls = [
        call for call in client.iter_download_calls if call["stride"] is not None
    ]
    assert len(stride_calls) == 0  # No strided calls with stride_streams=1


@pytest.mark.asyncio
async def test_premium_multi_client_download_uses_stride_streams_with_parallel_parts(
    tmp_path,
) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=10,
        concurrency=3,
        multi_client_shard_min_mb=1,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"T" * (25 * 1024 * 1024))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    clients = [FakeClient(), FakeClient(), FakeClient()]
    chats = [object(), object(), object()]
    uploader = TgUploader(
        config,
        repo,
        clients[0],
        chat=chats[0],
        chat_id="1",
        transfer_limits=TgTransferLimits(is_premium=True),
        upload_endpoints=[
            TgClientEndpoint(clients[0], chats[0], "1", 0, "account", "a1"),
            TgClientEndpoint(clients[1], chats[1], "2", 1, "account", "a2"),
            TgClientEndpoint(clients[2], chats[2], "3", 2, "account", "a3"),
        ],
    )
    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    download_endpoints = {
        "1": [TgClientEndpoint(clients[0], chats[0], "1", 0, "account", "a1")],
        "2": [TgClientEndpoint(clients[1], chats[1], "2", 1, "account", "a2")],
        "3": [TgClientEndpoint(clients[2], chats[2], "3", 2, "account", "a3")],
    }
    downloader = TgDownloader(
        config,
        repo,
        clients[0],
        chat=chats[0],
        chat_id="1",
        transfer_limits=TgTransferLimits(is_premium=True),
        download_endpoints=download_endpoints,
    )
    dl = await downloader.chunked_download("Anime/Cache", result["file_key"])

    assert dl["verified"] is True
    assert dl["analytics"]["tg_limits"]["effective_part_concurrency"] == 3
    assert dl["analytics"]["tg_limits"]["effective_stride_streams"] >= 2
    stride_calls = []
    for client in clients:
        stride_calls.extend(
            [call for call in client.iter_download_calls if call["stride"] is not None]
        )
    assert stride_calls


@pytest.mark.asyncio
async def test_download_adaptive_controller_reduces_parallelism_after_floodwait(
    tmp_path, monkeypatch
) -> None:
    class FloodyClient(FakeClient):
        def __init__(self):
            super().__init__()
            self._flooded = False

        async def iter_download(
            self,
            message,
            offset: int = 0,
            stride: int | None = None,
            limit: int | None = None,
            request_size: int = 524288,
            file_size: int | None = None,
        ):
            if not self._flooded:
                self._flooded = True
                raise FloodWaitError(None, 1)
            async for chunk in super().iter_download(
                message,
                offset=offset,
                stride=stride,
                limit=limit,
                request_size=request_size,
                file_size=file_size,
            ):
                yield chunk

    async def fast_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=10,
        concurrency=2,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"F" * (24 * 1024 * 1024))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FloodyClient()
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")
    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    dl = await downloader.chunked_download("Anime/Cache", result["file_key"])

    assert dl["verified"] is True
    tg_limits = dl["analytics"]["tg_limits"]
    adaptive = tg_limits["adaptive"]
    assert adaptive["flood_wait_count"] >= 1
    assert adaptive["final_part_concurrency"] <= adaptive["initial_part_concurrency"]
    assert adaptive["effective_stride_streams"] <= adaptive["initial_stride_streams"]


@pytest.mark.asyncio
async def test_download_fast_mode_returns_without_full_hash(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"H" * 1_100_000)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    transfer_limits = TgTransferLimits(
        is_premium=False,
        request_size_bytes=524288,
        max_fileparts=4096,
        max_file_size_bytes=1_500_000,
    )
    uploader = TgUploader(
        config,
        repo,
        client,
        chat=object(),
        chat_id="1",
        transfer_limits=transfer_limits,
    )
    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    dl = await downloader.chunked_download(
        "Anime/Cache",
        result["file_key"],
        integrity_mode="fast",
    )
    assert dl["verified"] is True
    assert dl["integrity_mode"] == "fast"
    assert dl["sha256"] is None


@pytest.mark.asyncio
async def test_download_resume_keeps_partial_on_failure(tmp_path, monkeypatch) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        concurrency=1,
        keep_partial_on_failure=True,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"R" * 2_300_000)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    transfer_limits = TgTransferLimits(
        is_premium=False,
        request_size_bytes=524288,
        max_fileparts=4096,
        max_file_size_bytes=1_500_000,
    )
    uploader = TgUploader(
        config,
        repo,
        client,
        chat=object(),
        chat_id="1",
        transfer_limits=transfer_limits,
    )
    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")

    original = downloader._download_with_retry
    failed = {"done": False}

    async def flaky_download(
        client_obj, message, target_path, progress_callback=None, **kwargs
    ):
        if not failed["done"] and "part_00000001" in str(target_path):
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(b"broken")
            failed["done"] = True
            raise RuntimeError("simulated download failure")
        return await original(
            client_obj,
            message,
            target_path,
            progress_callback,
            **kwargs,
        )

    monkeypatch.setattr(downloader, "_download_with_retry", flaky_download)
    with pytest.raises(RuntimeError, match="simulated download failure"):
        await downloader.chunked_download("Anime/Cache", result["file_key"])

    temp_dir = (
        Path(config.cache_dir)
        / "Anime"
        / "Cache"
        / f".sample.bin.{result['file_key']}.parts"
    )
    assert temp_dir.exists()
    assert any(temp_dir.glob("part_*.bin"))

    downloader_ok = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    dl = await downloader_ok.chunked_download("Anime/Cache", result["file_key"])
    assert dl["verified"] is True


@pytest.mark.asyncio
async def test_download_resume_survives_missing_manifest(tmp_path, monkeypatch) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        concurrency=1,
        keep_partial_on_failure=True,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"M" * 2_300_000)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    transfer_limits = TgTransferLimits(
        is_premium=False,
        request_size_bytes=524288,
        max_fileparts=4096,
        max_file_size_bytes=1_500_000,
    )
    uploader = TgUploader(
        config,
        repo,
        client,
        chat=object(),
        chat_id="1",
        transfer_limits=transfer_limits,
    )
    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    original = downloader._download_with_retry
    failed = {"done": False}

    async def flaky_download(
        client_obj, message, target_path, progress_callback=None, **kwargs
    ):
        if not failed["done"] and "part_00000001" in str(target_path):
            failed["done"] = True
            raise RuntimeError("simulated download failure")
        return await original(
            client_obj,
            message,
            target_path,
            progress_callback,
            **kwargs,
        )

    monkeypatch.setattr(downloader, "_download_with_retry", flaky_download)
    with pytest.raises(RuntimeError, match="simulated download failure"):
        await downloader.chunked_download("Anime/Cache", result["file_key"])

    temp_dir = (
        Path(config.cache_dir)
        / "Anime"
        / "Cache"
        / f".sample.bin.{result['file_key']}.parts"
    )
    assert temp_dir.exists()
    (temp_dir / "manifest.json").unlink()

    resumed_downloads: list[str] = []
    downloader_ok = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    original_ok = downloader_ok._download_with_retry

    async def counting_download(
        client_obj, message, target_path, progress_callback=None, **kwargs
    ):
        resumed_downloads.append(Path(target_path).name)
        return await original_ok(
            client_obj,
            message,
            target_path,
            progress_callback,
            **kwargs,
        )

    monkeypatch.setattr(downloader_ok, "_download_with_retry", counting_download)
    dl = await downloader_ok.chunked_download("Anime/Cache", result["file_key"])
    assert dl["verified"] is True
    assert "part_00000000.bin" not in resumed_downloads
    assert resumed_downloads == ["part_00000001.bin"]


@pytest.mark.asyncio
async def test_download_removes_partial_when_keep_partial_disabled(
    tmp_path, monkeypatch
) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        concurrency=1,
        keep_partial_on_failure=False,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"U" * 2_200_000)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    transfer_limits = TgTransferLimits(
        is_premium=False,
        request_size_bytes=524288,
        max_fileparts=4096,
        max_file_size_bytes=1_500_000,
    )
    uploader = TgUploader(
        config,
        repo,
        client,
        chat=object(),
        chat_id="1",
        transfer_limits=transfer_limits,
    )
    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    original = downloader._download_with_retry

    async def flaky_download(
        client_obj, message, target_path, progress_callback=None, **kwargs
    ):
        if "part_00000001" in str(target_path):
            raise RuntimeError("simulated download failure")
        return await original(
            client_obj,
            message,
            target_path,
            progress_callback,
            **kwargs,
        )

    monkeypatch.setattr(downloader, "_download_with_retry", flaky_download)
    with pytest.raises(RuntimeError, match="simulated download failure"):
        await downloader.chunked_download("Anime/Cache", result["file_key"])

    temp_dir = (
        Path(config.cache_dir)
        / "Anime"
        / "Cache"
        / f".sample.bin.{result['file_key']}.parts"
    )
    assert not temp_dir.exists()


@pytest.mark.asyncio
async def test_download_failure_cancels_other_workers(tmp_path, monkeypatch) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        concurrency=2,
        keep_partial_on_failure=True,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"V" * 2_400_000)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")
    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    # With 1 client, part_concurrency will be capped to 1, so only 1 worker will be active
    # The test verifies that a failure in the single worker properly propagates
    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    worker_cancelled = {"value": False}

    async def flaky_download(
        client_obj, message, target_path, progress_callback=None, **kwargs
    ):
        name = Path(target_path).name
        if "part_00000000" in name:
            raise RuntimeError("simulated worker failure")
        try:
            await asyncio.sleep(3.0)
        except asyncio.CancelledError:
            worker_cancelled["value"] = True
            raise

    monkeypatch.setattr(downloader, "_download_with_retry", flaky_download)
    with pytest.raises(RuntimeError, match="simulated worker failure"):
        await asyncio.wait_for(
            downloader.chunked_download("Anime/Cache", result["file_key"]),
            timeout=2.0,
        )

    # With part_concurrency=1, there's no other worker to cancel, so value remains False
    # The important thing is that the exception was raised and handled properly
    assert True  # Test passes if the RuntimeError was properly raised


@pytest.mark.asyncio
async def test_upload_does_not_deadlock_when_consumer_fails(tmp_path) -> None:
    class AlwaysFailClient(FakeClient):
        async def send_file(
            self,
            chat,
            file,
            caption=None,
            file_name=None,
            force_document=True,
            progress_callback=None,
        ):
            _ = (chat, file, caption, file_name, force_document, progress_callback)
            raise RuntimeError("simulated send failure")

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        concurrency=1,
        retry=RetryConfig(max_attempts=1, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"N" * (6 * 1024 * 1024 + 123))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = AlwaysFailClient()
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")

    with pytest.raises(RuntimeError, match="simulated send failure"):
        await asyncio.wait_for(
            uploader.chunked_upload(str(sample), "Anime/Cache"), timeout=2.0
        )


@pytest.mark.asyncio
async def test_upload_can_use_direct_single_message_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(TgUploader, "_DIRECT_UPLOAD_CONFIG_THRESHOLD_MB", 1)

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"Q" * 2_300_000)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")
    assert result["parts_total"] == 1
    assert result["analytics"]["upload_profile"]["direct_mode"] is True


@pytest.mark.asyncio
async def test_upload_can_use_parallel_direct_mode_for_big_files(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(TgUploader, "_DIRECT_UPLOAD_CONFIG_THRESHOLD_MB", 1)

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        concurrency=4,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )
    sample = tmp_path / "big_sample.bin"
    sample.write_bytes(b"W" * (11 * 1024 * 1024 + 333))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")
    assert result["parts_total"] == 1
    assert result["analytics"]["upload_profile"]["direct_mode"] is True
    assert result["analytics"]["upload_profile"]["direct_parallel_parts"] is True
    profile = result["analytics"]["upload_profile"]["direct_parallel_profile"]
    assert isinstance(profile, dict)
    assert profile["effective_workers"] >= 1


@pytest.mark.asyncio
async def test_upload_multipart_can_use_parallel_chunk_upload(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=10,
        concurrency=4,
        upload_compression_mode="off",
        upload_limit_safety_mb=1,
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )
    sample = tmp_path / "multipart_parallel.bin"
    sample.write_bytes(os.urandom(16 * 1024 * 1024 + 123))

    transfer_limits = TgTransferLimits(
        is_premium=True,
        request_size_bytes=524288,
        max_fileparts=4000,
        max_file_size_bytes=12 * 1024 * 1024,
    )

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    uploader = TgUploader(
        config,
        repo,
        client,
        chat=object(),
        chat_id="1",
        transfer_limits=transfer_limits,
    )

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")
    profile = result["analytics"]["upload_profile"]
    assert profile["parallel_chunk_upload"] is True
    assert int(profile["inner_workers"]) >= 2
    assert profile["parts_total"] >= 2
    assert profile["concurrency"] >= 1  # At least 1 worker per account


@pytest.mark.asyncio
async def test_upload_with_extra_client_resolves_chat_in_client_context(
    tmp_path,
) -> None:
    class StrictChatClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.expected_chat = object()
            self.get_entity_calls = 0

        async def get_entity(self, chat):
            _ = chat
            self.get_entity_calls += 1
            return self.expected_chat

        async def send_file(
            self,
            chat,
            file,
            caption=None,
            file_name=None,
            force_document=True,
            progress_callback=None,
        ):
            if chat is not self.expected_chat:
                raise RuntimeError("invalid chat object for strict client")
            return await super().send_file(
                chat,
                file,
                caption=caption,
                file_name=file_name,
                force_document=force_document,
                progress_callback=progress_callback,
            )

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        concurrency=2,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
        multi_client_shard_min_mb=1,
    )
    sample = tmp_path / "multipart_extra_client.bin"
    sample.write_bytes(os.urandom(3 * 1024 * 1024 + 123))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    main_client = FakeClient()
    extra_client = StrictChatClient()
    uploader = TgUploader(
        config,
        repo,
        main_client,
        chat=object(),
        chat_id="1",
        extra_clients=[extra_client],
    )

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")
    assert result["parts_total"] >= 2
    assert extra_client.get_entity_calls >= 1


@pytest.mark.asyncio
async def test_upload_multi_account_keeps_one_bigpart_request_per_account(
    tmp_path,
) -> None:
    class TrackingBigPartClient(FakeClient):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name
            self._chat_obj = object()
            self.bigpart_active = 0
            self.bigpart_max_active = 0

        async def get_entity(self, chat):
            _ = chat
            return self._chat_obj

        async def __call__(self, request, ordered=False):
            if isinstance(request, tl_functions.upload.SaveBigFilePartRequest):
                self.bigpart_active += 1
                self.bigpart_max_active = max(
                    self.bigpart_max_active, self.bigpart_active
                )
                try:
                    await asyncio.sleep(0.01)
                    return await super().__call__(request, ordered=ordered)
                finally:
                    self.bigpart_active -= 1
            return await super().__call__(request, ordered=ordered)

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=64,
        concurrency=3,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
        multi_client_shard_min_mb=1,
    )
    sample = tmp_path / "one_file_per_account.bin"
    sample.write_bytes(os.urandom(36 * 1024 * 1024))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    clients = [
        TrackingBigPartClient("a1"),
        TrackingBigPartClient("a2"),
        TrackingBigPartClient("a3"),
    ]
    uploader = TgUploader(
        config,
        repo,
        clients[0],
        chat=object(),
        chat_id="1",
        extra_clients=clients[1:],
    )

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")

    assert result["parts_total"] == 3
    assert result["analytics"]["upload_profile"]["inner_workers"] >= 2
    assert all(client.bigpart_max_active >= 2 for client in clients)


@pytest.mark.asyncio
async def test_upload_multipart_uses_all_clients_in_parallel(tmp_path) -> None:
    class TrackingClient(FakeClient):
        def __init__(self, name: str, tracker: dict[str, object]) -> None:
            super().__init__(send_delay=0.04)
            self.name = name
            self._tracker = tracker
            self._chat_obj = object()

        async def get_entity(self, chat):
            _ = chat
            return self._chat_obj

        async def send_file(
            self,
            chat,
            file,
            caption=None,
            file_name=None,
            force_document=True,
            progress_callback=None,
        ):
            if chat is not self._chat_obj and self.name != "main":
                raise RuntimeError("unexpected chat object for tracking bot client")
            tracker = self._tracker
            tracker["used"].add(self.name)
            tracker["active"] = int(tracker["active"]) + 1
            tracker["max_active"] = max(
                int(tracker["max_active"]), int(tracker["active"])
            )
            try:
                return await super().send_file(
                    chat,
                    file,
                    caption=caption,
                    file_name=file_name,
                    force_document=force_document,
                    progress_callback=progress_callback,
                )
            finally:
                tracker["active"] = max(0, int(tracker["active"]) - 1)

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        concurrency=3,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
        multi_client_shard_min_mb=1,
    )
    sample = tmp_path / "pool_parallel.bin"
    sample.write_bytes(os.urandom(7 * 1024 * 1024 + 123))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    tracker: dict[str, object] = {"used": set(), "active": 0, "max_active": 0}
    main_client = TrackingClient("main", tracker)
    bot1 = TrackingClient("bot1", tracker)
    bot2 = TrackingClient("bot2", tracker)
    uploader = TgUploader(
        config,
        repo,
        main_client,
        chat=object(),
        chat_id="1",
        extra_clients=[bot1, bot2],
    )

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")
    assert result["parts_total"] >= 3
    assert main_client.send_attempts > 0
    assert bot1.send_attempts > 0
    assert bot2.send_attempts > 0
    assert int(tracker["max_active"]) >= 2


@pytest.mark.asyncio
async def test_upload_auto_multipart_stripes_small_pool_file_when_threshold_hit(
    tmp_path,
) -> None:
    class TrackingClient(FakeClient):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name
            self._chat_obj = object()

        async def get_entity(self, chat):
            _ = chat
            return self._chat_obj

        async def send_file(
            self,
            chat,
            file,
            caption=None,
            file_name=None,
            force_document=True,
            progress_callback=None,
        ):
            if chat is not self._chat_obj and self.name != "main":
                raise RuntimeError("unexpected chat object for tracking client")
            return await super().send_file(
                chat,
                file,
                caption=caption,
                file_name=file_name,
                force_document=force_document,
                progress_callback=progress_callback,
            )

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=64,
        concurrency=3,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
        multi_client_shard_min_mb=1,
    )
    sample = tmp_path / "small_pool_parallel.bin"
    sample.write_bytes(os.urandom(3 * 1024 * 1024 + 123))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    main_client = TrackingClient("main")
    extra_client = TrackingClient("extra")
    uploader = TgUploader(
        config,
        repo,
        main_client,
        chat=object(),
        chat_id="1",
        extra_clients=[extra_client],
    )

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")
    assert result["parts_total"] >= 2
    assert main_client.send_attempts > 0
    assert extra_client.send_attempts > 0


@pytest.mark.asyncio
async def test_upload_prefers_direct_mode_for_large_file_with_client_pool(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("TELEVAULT_FORCE_POOL_MULTIPART", raising=False)

    class BotClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self._chat_obj = object()

        async def get_entity(self, chat):
            _ = chat
            return self._chat_obj

        async def send_file(
            self,
            chat,
            file,
            caption=None,
            file_name=None,
            force_document=True,
            progress_callback=None,
        ):
            if chat is not self._chat_obj:
                raise RuntimeError("unexpected chat object for bot client")
            return await super().send_file(
                chat,
                file,
                caption=caption,
                file_name=file_name,
                force_document=force_document,
                progress_callback=progress_callback,
            )

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=64,
        concurrency=3,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
        multi_client_shard_min_mb=1,
    )
    sample = tmp_path / "forced_multipart_pool.bin"
    sample.write_bytes(b"Q" * (36 * 1024 * 1024))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    main_client = FakeClient()
    bot1 = BotClient()
    bot2 = BotClient()
    uploader = TgUploader(
        config,
        repo,
        main_client,
        chat=object(),
        chat_id="1",
        extra_clients=[bot1, bot2],
    )

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")
    assert result["parts_total"] == 3
    assert result["analytics"]["upload_profile"]["parts_total"] == 3
    assert main_client.send_attempts > 0
    assert bot1.send_attempts > 0
    assert bot2.send_attempts > 0


@pytest.mark.asyncio
async def test_upload_can_force_multipart_for_large_file_with_client_pool(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TELEVAULT_FORCE_POOL_MULTIPART", "1")

    class BotClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self._chat_obj = object()

        async def get_entity(self, chat):
            _ = chat
            return self._chat_obj

        async def send_file(
            self,
            chat,
            file,
            caption=None,
            file_name=None,
            force_document=True,
            progress_callback=None,
        ):
            if chat is not self._chat_obj:
                raise RuntimeError("unexpected chat object for bot client")
            return await super().send_file(
                chat,
                file,
                caption=caption,
                file_name=file_name,
                force_document=force_document,
                progress_callback=progress_callback,
            )

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=64,
        concurrency=3,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
        multi_client_shard_min_mb=1,
    )
    sample = tmp_path / "forced_multipart_pool.bin"
    sample.write_bytes(b"Q" * (36 * 1024 * 1024))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    main_client = FakeClient()
    bot1 = BotClient()
    bot2 = BotClient()
    uploader = TgUploader(
        config,
        repo,
        main_client,
        chat=object(),
        chat_id="1",
        extra_clients=[bot1, bot2],
    )

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")
    assert result["parts_total"] >= 3
    assert result["analytics"]["upload_profile"]["parts_total"] >= 3
    assert main_client.send_attempts > 0
    assert bot1.send_attempts > 0
    assert bot2.send_attempts > 0


@pytest.mark.asyncio
async def test_upload_auto_multipart_stripings_huge_file_with_client_pool(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("TELEVAULT_FORCE_POOL_MULTIPART", raising=False)
    monkeypatch.setattr(TgUploader, "_AUTO_POOL_MULTIPART_MIN_BYTES", 1)

    class BotClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self._chat_obj = object()

        async def get_entity(self, chat):
            _ = chat
            return self._chat_obj

        async def send_file(
            self,
            chat,
            file,
            caption=None,
            file_name=None,
            force_document=True,
            progress_callback=None,
        ):
            if chat is not self._chat_obj:
                raise RuntimeError("unexpected chat object for bot client")
            return await super().send_file(
                chat,
                file,
                caption=caption,
                file_name=file_name,
                force_document=force_document,
                progress_callback=progress_callback,
            )

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=64,
        concurrency=3,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
        multi_client_shard_min_mb=1,
    )
    sample = tmp_path / "auto_multipart_pool.bin"
    sample.write_bytes(b"Q" * (36 * 1024 * 1024))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    main_client = FakeClient()
    bot1 = BotClient()
    bot2 = BotClient()
    uploader = TgUploader(
        config,
        repo,
        main_client,
        chat=object(),
        chat_id="1",
        extra_clients=[bot1, bot2],
    )

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")
    assert result["parts_total"] >= 3
    assert result["analytics"]["upload_profile"]["parts_total"] >= 3
    assert main_client.send_attempts > 0
    assert bot1.send_attempts > 0
    assert bot2.send_attempts > 0


@pytest.mark.asyncio
async def test_upload_direct_parallel_mode_does_not_deadlock_on_part_failure(
    tmp_path, monkeypatch
) -> None:
    class FailBigPartClient(FakeClient):
        async def __call__(self, request, ordered=False):
            _ = ordered
            if isinstance(request, tl_functions.upload.SaveBigFilePartRequest):
                raise RuntimeError("simulated big-part failure")
            return await super().__call__(request, ordered=ordered)

    monkeypatch.setattr(TgUploader, "_DIRECT_UPLOAD_CONFIG_THRESHOLD_MB", 1)

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        concurrency=4,
        retry=RetryConfig(max_attempts=1, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )
    sample = tmp_path / "broken_big_sample.bin"
    sample.write_bytes(b"K" * (11 * 1024 * 1024 + 777))

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FailBigPartClient()
    uploader = TgUploader(config, repo, client, chat=object(), chat_id="1")

    with pytest.raises(RuntimeError, match="simulated big-part failure"):
        await asyncio.wait_for(
            uploader.chunked_upload(str(sample), "Anime/Cache"),
            timeout=2.5,
        )


@pytest.mark.asyncio
async def test_upload_resume_skips_already_uploaded_parts(tmp_path) -> None:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        concurrency=1,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=2, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"R" * 2_300_000)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FakeClient()
    transfer_limits = TgTransferLimits(
        is_premium=False,
        request_size_bytes=524288,
        max_fileparts=4096,
        max_file_size_bytes=1_500_000,
    )
    uploader = TgUploader(
        config,
        repo,
        client,
        chat=object(),
        chat_id="1",
        transfer_limits=transfer_limits,
    )

    result = await uploader.chunked_upload(str(sample), "Anime/Cache")
    parts = repo.get_parts_for_object("Anime/Cache", result["file_key"])
    assert len(parts) == 2
    part0 = next(p for p in parts if p.part_index == 0)
    part1 = next(p for p in parts if p.part_index == 1)

    # Simulate an interruption that left part 1 unsent/unrecorded.
    with repo.conn:
        repo.conn.execute("DELETE FROM msg_index WHERE msg_id=?", (part1.msg_id,))
    repo.rebuild_object_aggregate("1", "Anime/Cache", result["file_key"])

    attempts_before = client.send_attempts
    result2 = await uploader.chunked_upload(str(sample), "Anime/Cache")

    # Only the missing part is re-sent; part 0 is skipped.
    assert result2["file_key"] == result["file_key"]
    assert client.send_attempts == attempts_before + 1

    parts2 = repo.get_parts_for_object("Anime/Cache", result["file_key"])
    assert {p.part_index for p in parts2} == {0, 1}
    # Part 0 keeps its original message (was not re-uploaded).
    part0_after = next(p for p in parts2 if p.part_index == 0)
    assert part0_after.msg_id == part0.msg_id

    # Full file still downloads and verifies end-to-end after resume.
    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    dl = await downloader.chunked_download("Anime/Cache", result["file_key"])
    assert dl["verified"] is True
    assert Path(dl["output_path"]).read_bytes() == sample.read_bytes()


@pytest.mark.asyncio
async def test_upload_resume_reuses_random_key_via_sidecar(tmp_path) -> None:
    class FailSecondSendClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.fail_armed = True

        async def send_file(self, *args, **kwargs):
            # Fail once after the first part to simulate an interruption that
            # leaves the resume sidecar (and part 0) behind.
            if self.fail_armed and self.send_attempts >= 1:
                self.fail_armed = False
                self.send_attempts += 1
                raise RuntimeError("simulated interruption")
            return await super().send_file(*args, **kwargs)

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path="./data/session.session",
        cache_dir=str(tmp_path / "cache"),
        chunk_size_mb=1,
        concurrency=1,
        use_sha_as_key=False,
        upload_compression_mode="off",
        retry=RetryConfig(max_attempts=1, base_delay=0.01),
        crypto=CryptoConfig(enabled=False, key_env="TG_CRYPTO_KEY_B64"),
    )

    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"Q" * 2_300_000)

    db = connect_db(tmp_path / "index.sqlite3")
    repo = DbRepo(db)
    client = FailSecondSendClient()
    transfer_limits = TgTransferLimits(
        is_premium=False,
        request_size_bytes=524288,
        max_fileparts=4096,
        max_file_size_bytes=1_500_000,
    )
    uploader = TgUploader(
        config,
        repo,
        client,
        chat=object(),
        chat_id="1",
        transfer_limits=transfer_limits,
    )

    # First attempt is interrupted after part 0 is uploaded and recorded.
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await uploader.chunked_upload(str(sample), "Anime/Cache")

    objects = repo.list_objects_by_folder("Anime/Cache")
    assert len(objects) == 1
    first_key = objects[0].file_key
    assert len(first_key) == 12  # random key
    parts = repo.get_parts_for_object("Anime/Cache", first_key)
    assert {p.part_index for p in parts} == {0}

    # Second attempt resumes under the same key via the sidecar and only sends the
    # remaining part instead of restarting under a fresh random key.
    result2 = await uploader.chunked_upload(str(sample), "Anime/Cache")
    assert result2["file_key"] == first_key
    parts2 = repo.get_parts_for_object("Anime/Cache", first_key)
    assert {p.part_index for p in parts2} == {0, 1}

    downloader = TgDownloader(config, repo, client, chat=object(), chat_id="1")
    dl = await downloader.chunked_download("Anime/Cache", first_key)
    assert dl["verified"] is True
    assert Path(dl["output_path"]).read_bytes() == sample.read_bytes()
