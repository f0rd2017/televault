"""Resuming an interrupted upload.

The source of truth for parts already uploaded is the ``msg_index`` (a row is
written only after the part was successfully sent to Telegram). Resuming means
skipping parts that already exist in the index for the same ``file_key`` with
the same ``parts_total`` and the same payload sha256 (taken from the part's
caption signature).

For a deterministic key (``use_sha_as_key=True``), ``file_key`` naturally
matches across runs. For a random key, ``file_key`` is recovered from a
sidecar manifest next to the cache (mirroring download's resume mechanism).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from app.core.types import PartRecord
from app.core.utils import ensure_dir, now_ts
from app.tg.parser import parse_caption

logger = logging.getLogger(__name__)

_RESUME_DIRNAME = ".upload_resume"


def source_signature(source_path: Path, *, size: int, mtime_ns: int) -> str:
    """Stable signature of the source file: path + size + mtime."""
    raw = f"{Path(source_path).resolve()}|{int(size)}|{int(mtime_ns)}".encode()
    return hashlib.sha256(raw).hexdigest()


def _sidecar_path(cache_dir: str | Path, signature: str) -> Path:
    return ensure_dir(Path(cache_dir) / _RESUME_DIRNAME) / f"{signature}.json"


def load_resume_file_key(
    cache_dir: str | Path, *, signature: str, payload_sha256: str
) -> str | None:
    """Return the saved ``file_key`` for this source, if the payload matches."""
    path = _sidecar_path(cache_dir, signature)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("payload_sha256") or "").lower() != str(payload_sha256).lower():
        return None
    file_key = str(data.get("file_key") or "").strip()
    return file_key or None


def write_resume_file(
    cache_dir: str | Path,
    *,
    signature: str,
    file_key: str,
    parts_total: int,
    payload_sha256: str,
    orig_name: str,
) -> None:
    """Save a sidecar for possible resumption (best-effort)."""
    payload = {
        "file_key": str(file_key),
        "parts_total": int(parts_total),
        "payload_sha256": str(payload_sha256),
        "orig_name": str(orig_name),
        "created_ts": now_ts(),
    }
    try:
        _sidecar_path(cache_dir, signature).write_text(
            json.dumps(payload), encoding="utf-8"
        )
    except OSError as exc:
        logger.debug("Failed to write upload resume sidecar: %s", exc)


def clear_resume_file(cache_dir: str | Path, *, signature: str) -> None:
    """Remove the sidecar (after a successful full upload; best-effort)."""
    try:
        _sidecar_path(cache_dir, signature).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Failed to clear upload resume sidecar: %s", exc)


def existing_completed_parts(
    parts: list[PartRecord],
    *,
    planned_parts_total: int,
    payload_sha256: str,
    caption_prefix: str,
) -> set[int]:
    """The set of ``part_index`` values already uploaded that qualify for resume.

    A part only counts if its ``parts_total`` matches the planned value and the
    sha256 in its caption signature matches the current payload's digest — this
    rules out a layout mismatch (the account pool changed) and non-deterministic
    compression.
    """
    target = str(payload_sha256).lower()
    completed: set[int] = set()
    for part in parts:
        if int(part.parts_total) != int(planned_parts_total):
            continue
        caption = (part.caption_raw or "").strip()
        if not caption:
            continue
        meta = parse_caption(caption, prefix=caption_prefix)
        if meta is None or not meta.sha256:
            continue
        if str(meta.sha256).lower() == target:
            completed.add(int(part.part_index))
    return completed
