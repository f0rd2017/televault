"""Общие хелперы для пакета download."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re

from telethon.errors import FileReferenceExpiredError, FloodWaitError, RPCError

_SHA_PREFIX_RE = re.compile(r"^[0-9a-f]{12}$")


def _is_retryable_error(exc: BaseException) -> bool:
    return isinstance(exc, (OSError, TimeoutError, RPCError)) and not isinstance(
        exc,
        (FloodWaitError, FileReferenceExpiredError),
    )


def _preallocate_file(path: Path, size: int) -> None:
    """Create a file of exactly `size` bytes so parallel writers can seek into it."""
    with open(path, "wb") as f:
        if size > 0:
            f.seek(size - 1)
            f.write(b"\x00")


def _sha256_file_sync(path: Path, chunk_size: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(max(1024, int(chunk_size)))
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
