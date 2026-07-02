"""Tests for utility functions not covered by test_utils.py."""

from __future__ import annotations

from pathlib import Path

from app.core.utils import (
    sanitize_filename,
    sha256_file,
    to_human_size,
    random_file_key,
    now_ts,
    ensure_dir,
    iter_file_chunks,
    _normalize_text,
)


# ── sanitize_filename ──────────────────────────────────────────────

def test_sanitize_filename_basic() -> None:
    assert sanitize_filename("hello world.txt") == "hello world.txt"


def test_sanitize_filename_removes_invalid_chars() -> None:
    result = sanitize_filename("file<name>.txt")
    assert "<" not in result
    assert ">" not in result


def test_sanitize_filename_dot_segments() -> None:
    # Should not allow . or .. as filename
    result = sanitize_filename("..")
    assert result != ".."
    assert len(result) > 0


def test_sanitize_filename_max_length() -> None:
    long_name = "a" * 300 + ".txt"
    result = sanitize_filename(long_name)
    assert len(result) <= 255


def test_sanitize_filename_empty() -> None:
    result = sanitize_filename("")
    assert len(result) > 0  # should fallback to something non-empty



# ── sha256_file ────────────────────────────────────────────────────

def test_sha256_file_small_file(tmp_path: Path) -> None:
    f = tmp_path / "small.bin"
    f.write_bytes(b"hello")
    digest = sha256_file(str(f))
    assert len(digest) == 64  # hex string
    assert digest == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_sha256_file_empty_file(tmp_path: Path) -> None:
    f = tmp_path / "empty.bin"
    f.write_bytes(b"")
    digest = sha256_file(str(f))
    assert len(digest) == 64


def test_sha256_file_with_chunk_size(tmp_path: Path) -> None:
    f = tmp_path / "chunked.bin"
    f.write_bytes(b"x" * 1000)
    digest = sha256_file(str(f), chunk_size=100)
    assert len(digest) == 64


# ── to_human_size ──────────────────────────────────────────────────


def test_to_human_size_bytes() -> None:
    assert to_human_size(500) == "500 B"


def test_to_human_size_kb() -> None:
    assert to_human_size(1500) == "1.5 KB"


def test_to_human_size_mb() -> None:
    result = to_human_size(5 * 1024 * 1024)
    assert "5.0" in result
    assert "MB" in result


def test_to_human_size_gb() -> None:
    result = to_human_size(2 * 1024 * 1024 * 1024)
    assert "2.0" in result
    assert "GB" in result


def test_to_human_size_tb() -> None:
    result = to_human_size(1024**4)
    assert "1.0" in result
    assert "TB" in result


# ── random_file_key ────────────────────────────────────────────────

def test_random_file_key_format() -> None:
    key = random_file_key()
    assert isinstance(key, str)
    assert len(key) > 0
    assert " " not in key


def test_random_file_key_unique() -> None:
    keys = {random_file_key() for _ in range(100)}
    assert len(keys) == 100


# ── now_ts ─────────────────────────────────────────────────────────

def test_now_ts_returns_int() -> None:
    ts = now_ts()
    assert isinstance(ts, int)
    assert ts > 1_000_000_000


# ── ensure_dir ─────────────────────────────────────────────────────

def test_ensure_dir_creates_nested(tmp_path: Path) -> None:
    d = tmp_path / "a" / "b" / "c"
    ensure_dir(str(d))
    assert d.exists()
    assert d.is_dir()


def test_ensure_dir_existing(tmp_path: Path) -> None:
    d = tmp_path / "existing"
    d.mkdir()
    ensure_dir(str(d))
    assert d.exists()


# ── iter_file_chunks ───────────────────────────────────────────────

def test_iter_file_chunks_small(tmp_path: Path) -> None:
    f = tmp_path / "data.bin"
    f.write_bytes(b"0123456789")
    chunks = list(iter_file_chunks(str(f), chunk_size=3))
    assert chunks == [b"012", b"345", b"678", b"9"]


def test_iter_file_chunks_exact(tmp_path: Path) -> None:
    f = tmp_path / "exact.bin"
    f.write_bytes(b"abcd")
    chunks = list(iter_file_chunks(str(f), chunk_size=2))
    assert chunks == [b"ab", b"cd"]


def test_iter_file_chunks_empty(tmp_path: Path) -> None:
    f = tmp_path / "empty.bin"
    f.write_bytes(b"")
    chunks = list(iter_file_chunks(str(f), chunk_size=10))
    assert chunks == []


# ── _normalize_text ────────────────────────────────────────────────

def test_normalize_text_strips_control_chars() -> None:
    result = _normalize_text("hello\x00world")
    assert "\x00" not in result


def test_normalize_text_nfkc() -> None:
    # NFKC normalization should normalize compatibility characters
    result = _normalize_text("ﬁ")  # ligature fi
    assert result == "fi"


def test_normalize_text_basic() -> None:
    result = _normalize_text("  hello  ")
    assert result == "hello"
