"""Tests for CacheManager: LRU eviction, active_download_keys, key matching."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from televault.core.cache import CacheManager, get_active_download_keys_from_repo


class FakeRepo:
    """Minimal fake DbRepo for cache key queries."""

    def __init__(self) -> None:
        self._jobs: list[dict] = []

    def add_job(
        self, status: str, job_type: str, folder_path: str, file_key: str
    ) -> None:
        self._jobs.append(
            {
                "payload_json": json.dumps(
                    {"folder_path": folder_path, "file_key": file_key}
                ),
                "type": job_type,
                "status": status,
            }
        )

    def list_jobs_by_status(self, status: str) -> list[dict]:
        return [j for j in self._jobs if j["status"] == status]


def _make_parts_dir(root: Path, name: str, old: bool = False) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "chunk.dat").write_bytes(b"x" * 100)
    if old:
        t = time.time() - 300
        for f in list(d.rglob("*")) + [d]:
            os.utime(f, (t, t))
    return d


def test_cache_keeps_fresh_parts(tmp_path: Path) -> None:
    _make_parts_dir(tmp_path, "abc.parts")
    r = CacheManager().cleanup(tmp_path, 0)
    assert r["deleted_files"] == 0
    assert (tmp_path / "abc.parts").exists()


def test_cache_removes_stale_parts(tmp_path: Path) -> None:
    d = _make_parts_dir(tmp_path, "stale.parts", old=True)
    r = CacheManager().cleanup(tmp_path, 0)
    assert r["deleted_files"] == 1
    assert not d.exists()


def test_cache_skips_active_key(tmp_path: Path) -> None:
    _make_parts_dir(tmp_path, "active.parts")
    active_keys = {("/f", "active")}
    r = CacheManager().cleanup(tmp_path, 0, active_download_keys=active_keys)
    assert r["deleted_files"] == 0
    assert (tmp_path / "active.parts").exists()


def test_cache_removes_non_active_key(tmp_path: Path) -> None:
    d = _make_parts_dir(tmp_path, "orphan.parts", old=True)
    active_keys = {("/f", "different")}
    r = CacheManager().cleanup(tmp_path, 0, active_download_keys=active_keys)
    assert r["deleted_files"] == 1
    assert not d.exists()


def test_cache_embedded_key_match(tmp_path: Path) -> None:
    _make_parts_dir(tmp_path, "folder_mykey.parts")
    active_keys = {("folder", "mykey")}
    r = CacheManager().cleanup(tmp_path, 0, active_download_keys=active_keys)
    assert r["deleted_files"] == 0


def test_cache_lru_eviction(tmp_path: Path) -> None:
    now = time.time()
    old_f = tmp_path / "old.bin"
    old_f.write_bytes(b"y" * 500)
    os.utime(old_f, (now - 1000, now - 1000))
    new_f = tmp_path / "new.bin"
    new_f.write_bytes(b"y" * 500)
    os.utime(new_f, (now, now))
    r = CacheManager().cleanup(tmp_path, 600)
    assert r["deleted_files"] == 1
    assert not old_f.exists()
    assert new_f.exists()


def test_cache_lru_keeps_recently_written_file(tmp_path: Path) -> None:
    # On relatime, atime is not updated on repeated reads — a file that
    # is being appended right now (fresh mtime with an old atime, a growing prefix
    # stream), it must not be evicted before something genuinely older.
    now = time.time()
    growing = tmp_path / "growing.bin"
    growing.write_bytes(b"y" * 500)
    os.utime(growing, (now - 1000, now))  # old atime, fresh mtime
    stale = tmp_path / "stale.bin"
    stale.write_bytes(b"y" * 500)
    os.utime(stale, (now - 500, now - 500))  # newer atime, but all old
    r = CacheManager().cleanup(tmp_path, 600)
    assert r["deleted_files"] == 1
    assert growing.exists()
    assert not stale.exists()


def test_cache_no_eviction_under_limit(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"y" * 100)
    (tmp_path / "b.bin").write_bytes(b"y" * 100)
    r = CacheManager().cleanup(tmp_path, 1000)
    assert r["deleted_files"] == 0


def test_get_active_download_keys_running(tmp_path: Path) -> None:
    repo = FakeRepo()
    repo.add_job("running", "download", "/videos", "key_run")
    repo.add_job("queued", "download", "/music", "key_queued")
    repo.add_job("running", "upload", "/docs", "key_up")
    keys = get_active_download_keys_from_repo(repo)
    assert keys == {("/videos", "key_run"), ("/music", "key_queued")}


def test_get_active_keys_empty(tmp_path: Path) -> None:
    assert get_active_download_keys_from_repo(FakeRepo()) == set()


def test_get_active_keys_done_not_included(tmp_path: Path) -> None:
    repo = FakeRepo()
    repo.add_job("done", "download", "/d", "k")
    repo.add_job("error", "download", "/e", "k2")
    assert get_active_download_keys_from_repo(repo) == set()
