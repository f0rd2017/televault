"""Тесты поблочного стрима без полного скачивания (инкремент 9/10).

Чистая логика раскладки/нарезки (без сети) + сквозной HTTP-стрим через реальный
сокет с фейковым воркером, который «скачивает» расшифрованные части на диск —
проверяем, что по Range качаются ТОЛЬКО нужные части.
"""

from __future__ import annotations

import os
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from app.api.server import ApiContext, ApiServer, StreamResponse, dispatch
from app.core.stream import (
    GCM_OVERHEAD,
    LayoutError,
    build_layout,
    iter_range_bytes,
)
from app.core.types import ApiConfig, PartMeta, PartRecord
from app.db.database import connect_db
from app.db.repo import DbRepo
from app.tg.parser import build_caption


# ── Хелперы построения частей ────────────────────────────────────────────────


def _caption(
    folder: str, key: str, idx: int, total: int, name: str, *, enc: bool
) -> str:
    meta = PartMeta(
        folder_path=folder,
        file_key=key,
        part_index=idx,
        parts_total=total,
        orig_name=name,
    )
    return build_caption(meta, extra={"enc": enc})


def _part(
    folder: str,
    key: str,
    idx: int,
    total: int,
    name: str,
    file_size: int,
    *,
    enc: bool = False,
    msg_id: int | None = None,
    chat_id: str = "chat",
) -> PartRecord:
    return PartRecord(
        msg_id=msg_id if msg_id is not None else 1000 + idx,
        chat_id=chat_id,
        folder_path=folder,
        file_key=key,
        part_index=idx,
        parts_total=total,
        orig_name=name,
        file_size=file_size,
        caption_raw=_caption(folder, key, idx, total, name, enc=enc),
        date_ts=100 + idx,
    )


# ── Раскладка (чистая) ───────────────────────────────────────────────────────


def test_layout_plaintext_offsets_unencrypted():
    parts = [
        _part("F", "k", 0, 3, "a.bin", 100),
        _part("F", "k", 1, 3, "a.bin", 50),
        _part("F", "k", 2, 3, "a.bin", 25),
    ]
    layout = build_layout(parts, caption_prefix="FC1|")
    assert layout.total_size == 175
    assert [(p.plain_start, p.plain_end) for p in layout.parts] == [
        (0, 100),
        (100, 150),
        (150, 175),
    ]
    assert all(p.plain_size == p.stored_size for p in layout.parts)


def test_layout_encrypted_subtracts_gcm_overhead():
    # Зашифрованные части хранят на GCM_OVERHEAD байт больше plaintext.
    parts = [
        _part("F", "k", 0, 2, "a.bin", 100 + GCM_OVERHEAD, enc=True),
        _part("F", "k", 1, 2, "a.bin", 40 + GCM_OVERHEAD, enc=True),
    ]
    layout = build_layout(parts, caption_prefix="FC1|")
    assert layout.total_size == 140
    assert layout.parts[0].plain_size == 100
    assert layout.parts[1].plain_size == 40
    assert layout.parts[1].plain_start == 100


def test_layout_rejects_incomplete():
    parts = [_part("F", "k", 0, 3, "a.bin", 10), _part("F", "k", 1, 3, "a.bin", 10)]
    with pytest.raises(LayoutError):
        build_layout(parts, caption_prefix="FC1|")


def test_layout_rejects_non_contiguous():
    parts = [_part("F", "k", 0, 2, "a.bin", 10), _part("F", "k", 2, 2, "a.bin", 10)]
    # parts_total=2, two parts, но индексы 0 и 2 — несвязно.
    parts[1] = _part("F", "k", 2, 3, "a.bin", 10)
    with pytest.raises(LayoutError):
        build_layout(parts, caption_prefix="FC1|")


def test_layout_rejects_unknown_size():
    parts = [_part("F", "k", 0, 1, "a.bin", 10)]
    parts[0] = PartRecord(
        msg_id=1,
        chat_id="c",
        folder_path="F",
        file_key="k",
        part_index=0,
        parts_total=1,
        orig_name="a.bin",
        file_size=None,
        caption_raw="",
        date_ts=1,
    )
    with pytest.raises(LayoutError):
        build_layout(parts, caption_prefix="FC1|")


def test_select_parts_by_range():
    parts = [
        _part("F", "k", 0, 3, "a.bin", 100),
        _part("F", "k", 1, 3, "a.bin", 100),
        _part("F", "k", 2, 3, "a.bin", 100),
    ]
    layout = build_layout(parts, caption_prefix="FC1|")
    assert [p.part_index for p in layout.select_parts(0, 299)] == [0, 1, 2]
    assert [p.part_index for p in layout.select_parts(0, 0)] == [0]
    assert [p.part_index for p in layout.select_parts(150, 150)] == [1]
    assert [p.part_index for p in layout.select_parts(99, 100)] == [0, 1]
    assert [p.part_index for p in layout.select_parts(250, 299)] == [2]
    # точная граница: байт 100 принадлежит части 1, не 0
    assert [p.part_index for p in layout.select_parts(100, 199)] == [1]


# ── Нарезка байтов из расшифрованных part-файлов ─────────────────────────────


def _write_parts(tmp_path: Path, blobs: list[bytes]) -> dict[int, str]:
    paths: dict[int, str] = {}
    for idx, blob in enumerate(blobs):
        p = tmp_path / f"part_{idx:08d}.bin"
        p.write_bytes(blob)
        paths[idx] = str(p)
    return paths


def test_iter_range_bytes_full_and_subrange(tmp_path):
    blobs = [b"AAAAA", b"BBBBB", b"CCCCC"]  # 15 байт всего
    parts = [_part("F", "k", i, 3, "a.bin", 5) for i in range(3)]
    layout = build_layout(parts, caption_prefix="FC1|")
    paths = _write_parts(tmp_path, blobs)

    full = b"".join(iter_range_bytes(layout, paths, 0, 14))
    assert full == b"AAAAABBBBBCCCCC"

    # подрэндж, пересекающий 3 части: байты 3..11 → "AABBBBBCC"
    mid = b"".join(iter_range_bytes(layout, paths, 3, 11))
    assert mid == b"AABBBBBCC"

    # внутри одной части
    one = b"".join(iter_range_bytes(layout, paths, 6, 8))
    assert one == b"BBB"


def test_iter_range_bytes_missing_part_raises(tmp_path):
    parts = [_part("F", "k", i, 2, "a.bin", 5) for i in range(2)]
    layout = build_layout(parts, caption_prefix="FC1|")
    paths = {0: str(_write_parts(tmp_path, [b"AAAAA"])[0])}  # части 1 нет
    with pytest.raises(FileNotFoundError):
        list(iter_range_bytes(layout, paths, 0, 9))


# ── Сквозной HTTP-стрим (реальный сокет, фейковый воркер) ────────────────────


CONTENT = b"".join(bytes([65 + i]) * 64 for i in range(6))  # 6 частей по 64 байта


class FakeStreamWorker:
    """«Скачивает» только запрошенные части, кладёт их plaintext на диск.

    Считает, сколько РАЗ каждую часть скачали с нуля — проверяем, что по узкому
    Range качаются не все части."""

    def __init__(self, content: bytes, part_size: int) -> None:
        self.content = content
        self.part_size = part_size
        self.fetch_log: list[list[int]] = []
        self.prefix_log: list[dict[int, int]] = []

    def fetch_stream_parts_blocking(
        self,
        folder,
        file_key,
        part_indices,
        cache_dir,
        *,
        timeout: float = 600.0,
        prefix_bytes: dict[int, int] | None = None,
    ) -> dict[int, str]:
        self.prefix_log.append(dict(prefix_bytes or {}))
        self.fetch_log.append(sorted(int(i) for i in part_indices))
        cache = Path(cache_dir)
        cache.mkdir(parents=True, exist_ok=True)
        out: dict[int, str] = {}
        for idx in part_indices:
            p = cache / f"part_{int(idx):08d}.bin"
            if not p.exists():  # переиспользуем кэш между запросами
                lo = int(idx) * self.part_size
                p.write_bytes(self.content[lo : lo + self.part_size])
            out[int(idx)] = str(p)
        return out


def _repo_with_parts(tmp_path) -> tuple[DbRepo, str]:
    repo = DbRepo(connect_db(tmp_path / "idx.sqlite3"))
    key = "k1"
    parts = [
        _part("Vids", key, i, 6, "movie.mp4", 64, msg_id=2000 + i) for i in range(6)
    ]
    repo.upsert_msg_parts_bulk(parts)
    return repo, key


def _stream_ctx(tmp_path, worker):
    repo, key = _repo_with_parts(tmp_path)
    repo.create_share("vid", "Vids", key, "movie.mp4", total_size=len(CONTENT))
    config = SimpleNamespace(
        cache_dir=str(tmp_path / "cache"),
        download_root=str(tmp_path / "dl"),
        caption_prefix="FC1|",
        api=ApiConfig(enabled=True, host="127.0.0.1", port=20451, token=""),
    )
    ctx = ApiContext(
        repo=repo,
        worker=worker,
        token="",
        config=config,
        share_dir=str(tmp_path / "share"),
    )
    return ctx, repo


def test_serve_share_returns_stream_response(tmp_path):
    worker = FakeStreamWorker(CONTENT, 64)
    ctx, _ = _stream_ctx(tmp_path, worker)
    result = dispatch(ctx, "GET", "/share/vid", {}, {}, b"")
    assert isinstance(result, StreamResponse)
    assert result.layout.total_size == len(CONTENT)
    assert result.content_type == "video/mp4"


def test_real_http_stream_range_fetches_only_needed_parts(tmp_path):
    worker = FakeStreamWorker(CONTENT, 64)
    ctx, repo = _stream_ctx(tmp_path, worker)
    config = SimpleNamespace(
        cache_dir=ctx.config.cache_dir,
        api=ApiConfig(enabled=True, host="127.0.0.1", port=0, token=""),
    )
    server = ApiServer(config=config, repo=repo, worker=worker)
    # Подменяем контекст на наш (со stream-полями).
    server._ctx = ctx  # noqa: SLF001
    assert server.start()
    try:
        host, port = server.address
        base = f"http://{host}:{port}/share/vid"

        # Узкий Range внутри части 2 (байты 130..140) → качается ТОЛЬКО часть 2.
        req = urllib.request.Request(base, headers={"Range": "bytes=130-140"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 206
            assert resp.headers["Content-Range"] == f"bytes 130-140/{len(CONTENT)}"
            body = resp.read()
        assert body == CONTENT[130:141]
        # Для отдачи Range синхронно качается ТОЛЬКО часть 2.
        assert worker.fetch_log[0] == [2]
        # prefix_bytes просит у части 2 только байты до конца запрошенного Range
        # (130..140 → внутри части 2 это 2..12 → нужно 13 байт от начала части),
        # а не всю часть целиком — так плеер не ждёт скачивания огромной части.
        assert worker.prefix_log[0] == {2: 13}
        # Упреждающая подкачка в фоне греет следующую часть (3) к приходу плеера.
        deadline = time.monotonic() + 5.0
        while [3] not in worker.fetch_log and time.monotonic() < deadline:
            time.sleep(0.02)
        assert [3] in worker.fetch_log

        # Полный запрос → все части, корректная склейка.
        with urllib.request.urlopen(base, timeout=10) as resp:
            assert resp.status == 200
            assert resp.read() == CONTENT
        assert repo.get_share("vid")["download_count"] >= 1
    finally:
        server.stop()


def test_stream_falls_back_to_full_when_no_parts(tmp_path):
    # Объект без частей в БД → build_layout падает → отдаём через полную сборку.
    repo = DbRepo(connect_db(tmp_path / "idx.sqlite3"))
    repo.create_share("np", "Docs", "nokey", "x.bin", total_size=5)

    class AssembleWorker:
        def __init__(self):
            self.assembled = False

        def fetch_stream_parts_blocking(self, *a, **k):
            return {}

        def assemble_file_blocking(self, folder, file_key, dest_dir, timeout=1800.0):
            self.assembled = True
            d = Path(dest_dir)
            d.mkdir(parents=True, exist_ok=True)
            f = d / "out.bin"
            f.write_bytes(b"hello")
            return str(f)

    worker = AssembleWorker()
    config = SimpleNamespace(
        cache_dir=str(tmp_path / "cache"),
        download_root=str(tmp_path / "dl"),
        caption_prefix="FC1|",
        api=ApiConfig(enabled=True, host="127.0.0.1", port=20451, token=""),
    )
    ctx = ApiContext(
        repo=repo,
        worker=worker,
        token="",
        config=config,
        share_dir=str(tmp_path / "share"),
    )
    result = dispatch(ctx, "GET", "/share/np", {}, {}, b"")
    from app.api.server import FileResponse

    assert isinstance(result, FileResponse)
    assert worker.assembled is True
