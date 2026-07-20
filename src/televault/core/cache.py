from __future__ import annotations

import json
import logging
from pathlib import Path
import shutil
import threading
import time

logger = logging.getLogger(__name__)


class CacheManager:
    """LRU-based cache cleanup for the local download cache directory.

    Orphaned .parts directories are removed only when there is no
    corresponding running/pending download job in the SQLite jobs table,
    avoiding race conditions with active downloads.
    """

    _run_lock = threading.Lock()
    _ACTIVE_PARTS_GRACE_SEC = 60.0  # fallback grace period

    def cleanup(
        self,
        cache_dir: str | Path,
        max_bytes: int,
        *,
        active_download_keys: set[tuple[str, str]] | None = None,
    ) -> dict[str, int]:
        """
        Remove orphaned .parts directories and, if total cache size exceeds
        *max_bytes*, evict files by least-recently-accessed order until under limit.

        Parameters
        ----------
        cache_dir : path to the cache root
        max_bytes : 0 = only orphan cleanup; >0 = LRU eviction too
        active_download_keys : set of (folder_path, file_key) tuples for currently
            running/pending download jobs.  If None, falls back to time-based grace.

        Returns {"deleted_files": N, "freed_bytes": M}.
        """
        if not self._run_lock.acquire(blocking=False):
            return {"deleted_files": 0, "freed_bytes": 0}

        try:
            return self._cleanup_locked(
                cache_dir, max_bytes, active_download_keys=active_download_keys
            )
        finally:
            self._run_lock.release()

    def _cleanup_locked(
        self,
        cache_dir: str | Path,
        max_bytes: int,
        *,
        active_download_keys: set[tuple[str, str]] | None = None,
    ) -> dict[str, int]:
        root = Path(cache_dir)
        if not root.exists():
            return {"deleted_files": 0, "freed_bytes": 0}

        deleted_files = 0
        freed_bytes = 0
        now = time.time()

        # 1. Remove orphaned .parts directories
        for parts_dir in root.rglob("*.parts"):
            if not parts_dir.is_dir():
                continue

            # Check if this .parts dir belongs to an active download job
            if active_download_keys is not None:
                if self._parts_dir_is_active(parts_dir, active_download_keys):
                    continue  # skip — live download in progress

            # Fallback: skip recently touched dirs (grace period)
            size, latest_mtime = self._dir_size_and_latest_mtime(parts_dir)
            if (
                latest_mtime is not None
                and (now - latest_mtime) < self._ACTIVE_PARTS_GRACE_SEC
            ):
                continue

            try:
                shutil.rmtree(parts_dir, ignore_errors=True)
                freed_bytes += size
                deleted_files += 1
                logger.info(
                    "Cache cleanup: removed orphan .parts dir %s", parts_dir.name
                )
            except OSError:
                pass

        if max_bytes <= 0:
            return {"deleted_files": deleted_files, "freed_bytes": freed_bytes}

        # 2. Collect all regular files with their access times and sizes.
        # relatime (a typical mount option) doesn't update atime on repeated
        # reads, so we take max(atime, mtime) — a file that's currently being
        # written to (a growing stream prefix) won't look like the oldest one.
        file_entries: list[tuple[float, int, Path]] = []
        for f in root.rglob("*"):
            if f.is_file():
                try:
                    st = f.stat()
                    file_entries.append((max(st.st_atime, st.st_mtime), st.st_size, f))
                except OSError:
                    pass

        total_size = sum(size for _, size, _ in file_entries)
        if total_size <= max_bytes:
            return {"deleted_files": deleted_files, "freed_bytes": freed_bytes}

        # 3. Sort by atime ascending (oldest access first = LRU)
        file_entries.sort(key=lambda e: e[0])

        for atime, size, path in file_entries:
            if total_size <= max_bytes:
                break
            try:
                path.unlink(missing_ok=True)
                total_size -= size
                freed_bytes += size
                deleted_files += 1
            except OSError:
                pass

        return {"deleted_files": deleted_files, "freed_bytes": freed_bytes}

    @staticmethod
    def _parts_dir_is_active(
        parts_dir: Path,
        active_download_keys: set[tuple[str, str]],
    ) -> bool:
        """Check if the .parts directory name matches any active download.

        .parts directories are named like ``<file_key>.parts`` or
        ``<folder_path>_<file_key>.parts`` depending on download implementation.
        We try to extract file_key from the dirname and match against active set.
        """
        dir_name = parts_dir.name

        # Direct match: <file_key>.parts
        if dir_name.endswith(".parts"):
            candidate_key = dir_name[: -len(".parts")]
            for folder_path, file_key in active_download_keys:
                if file_key == candidate_key:
                    return True

        # Also check if the file_key is embedded somewhere in the name
        # (some download implementations prefix with folder)
        for _folder_path, file_key in active_download_keys:
            if file_key in dir_name:
                return True

        return False

    @staticmethod
    def _dir_size_and_latest_mtime(parts_dir: Path) -> tuple[int, float | None]:
        total_size = 0
        latest_mtime: float | None = None
        try:
            latest_mtime = float(parts_dir.stat().st_mtime)
        except OSError:
            latest_mtime = None

        for file_path in parts_dir.rglob("*"):
            if not file_path.is_file():
                continue
            try:
                stat = file_path.stat()
            except OSError:
                continue
            total_size += int(stat.st_size)
            mtime = float(stat.st_mtime)
            if latest_mtime is None or mtime > latest_mtime:
                latest_mtime = mtime

        return total_size, latest_mtime


def get_active_download_keys_from_repo(repo) -> set[tuple[str, str]]:
    """Query the SQLite jobs table for running/pending download jobs.

    Returns a set of (folder_path, file_key) tuples.
    """
    keys: set[tuple[str, str]] = set()
    try:
        rows = repo.list_jobs_by_status("running")
        for row in rows:
            payload = _parse_payload(row.get("payload_json"))
            if payload and str(row.get("type")) == "download":
                fp = str(payload.get("folder_path", "")).strip()
                fk = str(payload.get("file_key", "")).strip()
                if fp and fk:
                    keys.add((fp, fk))
    except Exception:
        logger.exception("Failed to query active download jobs for cache cleanup")

    try:
        rows = repo.list_jobs_by_status("queued")
        for row in rows:
            payload = _parse_payload(row.get("payload_json"))
            if payload and str(row.get("type")) == "download":
                fp = str(payload.get("folder_path", "")).strip()
                fk = str(payload.get("file_key", "")).strip()
                if fp and fk:
                    keys.add((fp, fk))
    except Exception:
        logger.exception("Failed to query queued download jobs for cache cleanup")

    return keys


def _parse_payload(payload_json: str | None) -> dict | None:
    if not payload_json:
        return None
    try:
        data = json.loads(payload_json)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None
