from __future__ import annotations

import os
import time

from app.core.cache import CacheManager


def test_cleanup_keeps_fresh_parts_dirs(tmp_path) -> None:
    cache_dir = tmp_path / "cache"
    parts_dir = cache_dir / "Anime" / ".file.bin.abc.parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    part_file = parts_dir / "part_00000000.bin"
    part_file.write_bytes(b"x" * 32)

    now = time.time()
    os.utime(part_file, (now, now))
    os.utime(parts_dir, (now, now))

    manager = CacheManager()
    result = manager.cleanup(cache_dir, max_bytes=0)

    assert result["deleted_files"] == 0
    assert result["freed_bytes"] == 0
    assert parts_dir.exists()


def test_cleanup_removes_stale_parts_dirs(tmp_path) -> None:
    cache_dir = tmp_path / "cache"
    parts_dir = cache_dir / "Anime" / ".file.bin.abc.parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    part_file = parts_dir / "part_00000000.bin"
    payload = b"y" * 128
    part_file.write_bytes(payload)

    old = time.time() - (CacheManager._ACTIVE_PARTS_GRACE_SEC + 120.0)
    os.utime(part_file, (old, old))
    os.utime(parts_dir, (old, old))

    manager = CacheManager()
    result = manager.cleanup(cache_dir, max_bytes=0)

    assert result["deleted_files"] == 1
    assert result["freed_bytes"] >= len(payload)
    assert not parts_dir.exists()


def test_cleanup_skips_when_another_cleanup_is_running(tmp_path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    acquired = CacheManager._run_lock.acquire(blocking=False)
    assert acquired is True
    try:
        manager = CacheManager()
        result = manager.cleanup(cache_dir, max_bytes=0)
    finally:
        CacheManager._run_lock.release()

    assert result == {"deleted_files": 0, "freed_bytes": 0}
