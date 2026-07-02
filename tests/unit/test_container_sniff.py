from pathlib import Path
from types import SimpleNamespace

from app.api.shares import (
    _NON_STREAMABLE_EXT,
    _is_non_streamable_container,
    _mp4_needs_full_assembly,
)


def _box(box_type: bytes, payload: bytes = b"") -> bytes:
    size = len(payload) + 8
    return size.to_bytes(4, "big") + box_type + payload


def test_faststart_mp4_streams_by_range():
    head = (
        _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2avc1mp41")
        + _box(b"moov", b"\x00" * 256)
        + _box(b"mdat", b"\x00" * 64)
    )
    assert _mp4_needs_full_assembly(head) is False


def test_non_faststart_mp4_needs_full_assembly():
    head = (
        _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2avc1mp41")
        + _box(b"free")
        + b"\x13\x6b\xe1\x4amdat"
        + b"\x00" * 64
    )
    assert _mp4_needs_full_assembly(head) is True


def test_head_without_mdat_is_streamable():
    assert _mp4_needs_full_assembly(b"\x00\x00\x00\x20ftypisom") is False


class _PartZeroWorker:
    """Отдаёт заданные байты как содержимое part 0 (для сниффа контейнера)."""

    def __init__(self, head: bytes) -> None:
        self.head = head
        self.calls = 0

    def fetch_stream_parts_blocking(
        self, folder, file_key, part_indices, cache_dir, *, timeout: float = 600.0
    ) -> dict[int, str]:
        self.calls += 1
        cache = Path(cache_dir)
        cache.mkdir(parents=True, exist_ok=True)
        out = cache / "part_00000000.bin"
        out.write_bytes(self.head)
        return {0: str(out)}


def test_avi_signature_is_non_streamable(tmp_path):
    # RIFF....AVI  — сигнатура AVI (индекс idx1 в хвосте файла, см. модульный
    # докстринг): такие контейнеры НЕ должны стримиться частями по Range.
    worker = _PartZeroWorker(b"RIFF\x00\x00\x00\x00AVI LIST" + b"\x00" * 64)
    ctx = SimpleNamespace(worker=worker, share_dir=str(tmp_path))
    assert (
        _is_non_streamable_container(ctx, "F", "k", "movie.avi", _NON_STREAMABLE_EXT)
        is True
    )


def test_matroska_signature_is_streamable(tmp_path):
    worker = _PartZeroWorker(b"\x1a\x45\xdf\xa3" + b"\x00" * 64)
    ctx = SimpleNamespace(worker=worker, share_dir=str(tmp_path))
    assert (
        _is_non_streamable_container(ctx, "F", "k", "movie.mkv", _NON_STREAMABLE_EXT)
        is False
    )
