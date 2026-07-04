"""Compression and archiving for the upload path.

Pure synchronous functions (run via ``asyncio.to_thread`` from
``TgUploader``): fast single-file zip (with the MT 7z backend, if available),
"should we compress" heuristics, and building a transparent STORED archive
for a batch of small files. No state — only the passed-in arguments and the
cancel token.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any
import zipfile

from app.core.jobs import CancelToken, JobCancelledError
from app.core.utils import sanitize_filename, sha256_file

logger = logging.getLogger(__name__)

ZIP_COMPRESSION_LEVEL = 3
ZIP_BUFFER_SIZE = 4 * 1024 * 1024
SMALL_BATCH_ZIP_BUFFER_SIZE = 2 * 1024 * 1024
AUTO_COMPRESSION_MIN_GAIN_BYTES = 1 * 1024 * 1024
PREHASH_CHUNK_SIZE = 16 * 1024 * 1024
ZIP_MT_TOOL_NAMES = ("7z", "7za")
ZIP_MT_POLL_INTERVAL_SEC = 0.1
ZIP_MT_ENV_PATH = "TGCCM_7Z_PATH"
ZIP_MT_ENV_THREADS = "TGCCM_7Z_THREADS"
AUTO_SKIP_EXTENSIONS = frozenset(
    {
        ".7z",
        ".avi",
        ".bz2",
        ".flac",
        ".gif",
        ".gz",
        ".heic",
        ".img",
        ".iso",
        ".jpeg",
        ".jpg",
        ".m4a",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".png",
        ".rar",
        ".tar",
        ".vhd",
        ".vmdk",
        ".webm",
        ".xz",
        ".zip",
        ".zst",
    }
)


def should_attempt_fast_compression(
    *,
    source_path: Path,
    source_size: int,
    safe_limit_bytes: int,
    mode: str,
) -> bool:
    normalized_mode = str(mode).strip().lower()
    if normalized_mode == "off":
        return False
    if normalized_mode == "force":
        return source_size > 0
    if source_size <= 0:
        return False
    ext = source_path.suffix.lower()
    if source_size > int(safe_limit_bytes):
        if ext in AUTO_SKIP_EXTENSIONS:
            return False
        return True
    # For speed, auto mode skips compression when file already fits safe Telegram limit.
    return False


def should_use_compressed_payload(
    *,
    source_size: int,
    compressed_size: int,
    safe_limit_bytes: int,
    mode: str,
) -> bool:
    if source_size <= 0 or compressed_size <= 0:
        return False
    if compressed_size >= source_size:
        return False

    normalized_mode = str(mode).strip().lower()
    if normalized_mode == "force":
        return True
    if source_size > int(safe_limit_bytes):
        if compressed_size <= int(safe_limit_bytes):
            return True
        return (source_size - compressed_size) >= AUTO_COMPRESSION_MIN_GAIN_BYTES

    min_gain = max(AUTO_COMPRESSION_MIN_GAIN_BYTES, int(source_size * 0.02))
    return (source_size - compressed_size) >= min_gain


def compress_file_to_temp_zip(
    source_path: Path,
    arc_name: str,
    cancel_token: CancelToken,
) -> Path:
    source = source_path.expanduser().resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"File not found: {source}")

    mt_archive = _compress_file_to_temp_zip_mt_7z(source, cancel_token)
    if mt_archive is not None:
        return mt_archive

    temp_file = tempfile.NamedTemporaryFile(
        prefix="tgccm_zip_", suffix=".zip", delete=False
    )
    temp_path = Path(temp_file.name)
    temp_file.close()
    entry_name = sanitize_filename(arc_name) or "file.bin"
    try:
        with zipfile.ZipFile(
            temp_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=ZIP_COMPRESSION_LEVEL,
            allowZip64=True,
        ) as archive:
            with (
                source.open("rb") as src,
                archive.open(
                    entry_name,
                    mode="w",
                    force_zip64=True,
                ) as dst,
            ):
                while True:
                    cancel_token.raise_if_cancelled()
                    chunk = src.read(ZIP_BUFFER_SIZE)
                    if not chunk:
                        break
                    dst.write(chunk)
        if temp_path.stat().st_size <= 0:
            raise ValueError("Compressed archive is empty")
        return temp_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _compress_file_to_temp_zip_mt_7z(
    source: Path,
    cancel_token: CancelToken,
) -> Path | None:
    seven_zip = _find_7z_executable()
    if not seven_zip:
        logger.info(
            "7z multi-thread backend unavailable: executable not found, using python zip fallback"
        )
        return None

    temp_file = tempfile.NamedTemporaryFile(
        prefix="tgccm_zip_mt_", suffix=".zip", delete=False
    )
    temp_path = Path(temp_file.name)
    temp_file.close()
    # 7z "a" expects to create a new archive if target does not exist.
    # NamedTemporaryFile already created an empty file, so remove it first.
    temp_path.unlink(missing_ok=True)
    proc: subprocess.Popen[str] | None = None
    try:
        mt_threads = _resolve_7z_threads()
        cmd = [
            seven_zip,
            "a",
            "-tzip",
            str(temp_path),
            source.name,
            f"-mx={int(ZIP_COMPRESSION_LEVEL)}",
            f"-mmt={mt_threads}",
            "-bd",
            "-y",
            "-bso0",
            "-bsp0",
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=str(source.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        while True:
            cancel_token.raise_if_cancelled()
            rc = proc.poll()
            if rc is not None:
                break
            time.sleep(ZIP_MT_POLL_INTERVAL_SEC)

        if rc != 0:
            stderr_text = ""
            if proc.stderr is not None:
                stderr_text = (proc.stderr.read() or "").strip()
            logger.warning(
                "7z multi-thread compression failed (rc=%s), fallback to python zip: %s",
                rc,
                stderr_text[:200],
            )
            temp_path.unlink(missing_ok=True)
            return None

        if temp_path.stat().st_size <= 0:
            temp_path.unlink(missing_ok=True)
            return None

        logger.info(
            "Upload compression backend: 7z multi-thread threads=%d exe=%s",
            mt_threads,
            seven_zip,
        )
        return temp_path
    except JobCancelledError:
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass
        temp_path.unlink(missing_ok=True)
        raise
    except Exception:
        temp_path.unlink(missing_ok=True)
        return None
    finally:
        if proc is not None and proc.stderr is not None:
            proc.stderr.close()


def _find_7z_executable() -> str | None:
    env_override = str(os.getenv(ZIP_MT_ENV_PATH, "")).strip()
    if env_override:
        candidate = Path(env_override).expanduser()
        if candidate.exists() and candidate.is_file():
            return str(candidate)

    for name in ZIP_MT_TOOL_NAMES:
        resolved = shutil.which(name)
        if resolved:
            return resolved

    common_dirs = [
        os.getenv("ProgramW6432", ""),
        os.getenv("ProgramFiles", ""),
        os.getenv("ProgramFiles(x86)", ""),
    ]
    seen: set[str] = set()
    for root in common_dirs:
        root_clean = str(root or "").strip()
        if not root_clean or root_clean in seen:
            continue
        seen.add(root_clean)
        candidate = Path(root_clean) / "7-Zip" / "7z.exe"
        if candidate.exists() and candidate.is_file():
            return str(candidate)
    return None


def _resolve_7z_threads() -> int:
    raw = str(os.getenv(ZIP_MT_ENV_THREADS, "")).strip()
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return min(256, parsed)
        except ValueError:
            logger.warning(
                "Invalid %s value '%s', falling back to CPU count",
                ZIP_MT_ENV_THREADS,
                raw,
            )
    cpu_threads = int(os.cpu_count() or 1)
    return max(1, min(256, cpu_threads))


def build_group_archive(
    file_items: list[tuple[str, str]],
    archive_path: Path,
    cancel_token: CancelToken,
) -> list[dict[str, Any]]:
    ensure_parent = archive_path.parent
    ensure_parent.mkdir(parents=True, exist_ok=True)

    used_names: set[str] = set()
    members: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:
            for index, (raw_path, member_folder) in enumerate(file_items, start=1):
                cancel_token.raise_if_cancelled()
                source = Path(raw_path).expanduser().resolve()
                if not source.exists() or not source.is_file():
                    raise FileNotFoundError(f"File not found: {source}")

                base_name = sanitize_filename(source.name) or f"file_{index:04d}.bin"
                arc_name = f"{index:05d}_{base_name}"
                suffix = 1
                while arc_name in used_names:
                    stem = Path(base_name).stem or "file"
                    ext = Path(base_name).suffix
                    arc_name = f"{index:05d}_{stem}_{suffix}{ext}"
                    suffix += 1
                used_names.add(arc_name)

                source_stat = source.stat()
                source_digest = sha256_file(source, PREHASH_CHUNK_SIZE)
                source_size = int(source_stat.st_size)
                if source_size < 0:
                    source_size = 0
                member_name = sanitize_filename(source.name)
                rel_path = f"{member_folder}/{member_name}"
                members.append(
                    {
                        "source_path": str(source),
                        "orig_name": member_name,
                        "folder_path": str(member_folder),
                        "rel_path": rel_path,
                        "size": source_size,
                        "sha256": source_digest,
                        "mtime": int(source_stat.st_mtime),
                        "member_index": index - 1,
                        "archive_name": arc_name,
                    }
                )

                with (
                    source.open("rb") as src,
                    archive.open(
                        arc_name,
                        mode="w",
                        force_zip64=True,
                    ) as dst,
                ):
                    while True:
                        cancel_token.raise_if_cancelled()
                        chunk = src.read(SMALL_BATCH_ZIP_BUFFER_SIZE)
                        if not chunk:
                            break
                        dst.write(chunk)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    return members
