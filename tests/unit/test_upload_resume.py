from __future__ import annotations

from pathlib import Path

from televault.core.types import PartMeta, PartRecord
from televault.tg.parser import build_caption
from televault.tg.upload.resume import (
    clear_resume_file,
    existing_completed_parts,
    load_resume_file_key,
    source_signature,
    write_resume_file,
)

_PREFIX = "FC1|"
_DIGEST = "a" * 64
_OTHER_DIGEST = "b" * 64


def _part(part_index: int, *, parts_total: int, sha256: str | None) -> PartRecord:
    extra = {"orig_size": 1000, "part_size": 500, "enc": False}
    if sha256 is not None:
        extra["sha256"] = sha256
    caption = build_caption(
        PartMeta(
            folder_path="/f",
            file_key="key123",
            part_index=part_index,
            parts_total=parts_total,
            orig_name="movie.bin",
        ),
        prefix=_PREFIX,
        extra=extra,
    )
    return PartRecord(
        msg_id=1000 + part_index,
        chat_id="-100123",
        folder_path="/f",
        file_key="key123",
        part_index=part_index,
        parts_total=parts_total,
        orig_name="movie.bin",
        file_size=500,
        caption_raw=caption,
        date_ts=1,
    )


def test_existing_completed_parts_matches_digest_and_total():
    parts = [
        _part(0, parts_total=3, sha256=_DIGEST),
        _part(1, parts_total=3, sha256=_DIGEST),
    ]
    completed = existing_completed_parts(
        parts, planned_parts_total=3, payload_sha256=_DIGEST, caption_prefix=_PREFIX
    )
    assert completed == {0, 1}


def test_existing_completed_parts_rejects_parts_total_mismatch():
    parts = [_part(0, parts_total=2, sha256=_DIGEST)]
    completed = existing_completed_parts(
        parts, planned_parts_total=3, payload_sha256=_DIGEST, caption_prefix=_PREFIX
    )
    assert completed == set()


def test_existing_completed_parts_rejects_digest_mismatch():
    parts = [_part(0, parts_total=3, sha256=_OTHER_DIGEST)]
    completed = existing_completed_parts(
        parts, planned_parts_total=3, payload_sha256=_DIGEST, caption_prefix=_PREFIX
    )
    assert completed == set()


def test_existing_completed_parts_rejects_missing_sha():
    parts = [_part(0, parts_total=3, sha256=None)]
    completed = existing_completed_parts(
        parts, planned_parts_total=3, payload_sha256=_DIGEST, caption_prefix=_PREFIX
    )
    assert completed == set()


def test_sidecar_roundtrip(tmp_path: Path):
    sig = source_signature(tmp_path / "movie.bin", size=1000, mtime_ns=123)
    write_resume_file(
        tmp_path,
        signature=sig,
        file_key="randomkey",
        parts_total=3,
        payload_sha256=_DIGEST,
        orig_name="movie.bin",
    )
    assert (
        load_resume_file_key(tmp_path, signature=sig, payload_sha256=_DIGEST)
        == "randomkey"
    )
    # Payload digest mismatch => no reuse (source content changed under same name).
    assert (
        load_resume_file_key(tmp_path, signature=sig, payload_sha256=_OTHER_DIGEST)
        is None
    )
    clear_resume_file(tmp_path, signature=sig)
    assert load_resume_file_key(tmp_path, signature=sig, payload_sha256=_DIGEST) is None


def test_source_signature_is_stable_and_sensitive(tmp_path: Path):
    p = tmp_path / "movie.bin"
    base = source_signature(p, size=1000, mtime_ns=123)
    assert base == source_signature(p, size=1000, mtime_ns=123)
    assert base != source_signature(p, size=1001, mtime_ns=123)
    assert base != source_signature(p, size=1000, mtime_ns=124)


def test_load_missing_sidecar_returns_none(tmp_path: Path):
    sig = source_signature(tmp_path / "nope.bin", size=1, mtime_ns=1)
    assert load_resume_file_key(tmp_path, signature=sig, payload_sha256=_DIGEST) is None
