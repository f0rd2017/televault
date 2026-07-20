"""Recover batch-blob manifests directly from Telegram.

Small files are uploaded packed into zip "blobs". Historically the member
list (manifest) lived only in the local SQLite database, so a fresh machine
scanning the same channels saw the blob messages but could not show the
files inside them.

This module rebuilds the missing manifests without re-downloading whole
blobs: it fetches only the tail of each zip (the central directory lives at
the end of the archive), reads the embedded ``_tgccm_manifest.json`` entry
when present (new uploads), or reconstructs members from the central
directory entries (old uploads, whose entries are named ``NNNNN_origname``).
"""

from __future__ import annotations

import calendar
import io
import json
import logging
import re
import zipfile
from typing import Any

from televault.core.jobs import CancelToken
from televault.core.types import AppConfig
from televault.core.utils import (
    file_key_from_sha256,
    normalize_folder_path,
    now_ts,
    random_file_key,
    sanitize_filename,
)
from televault.db.repo import DbRepo
from televault.tg.compression import BATCH_MANIFEST_ARC_NAME as MANIFEST_ARC_NAME
from televault.tg.parser import parse_batch_blob_caption

logger = logging.getLogger(__name__)

_MEMBER_ARC_RE = re.compile(r"^(\d{5})_(.+)$")

# Tail sizes to try, in order. 512 members * ~120 bytes of central directory
# per entry is ~60 KB, so the first attempt nearly always suffices.
_TAIL_LADDER = (256 * 1024, 4 * 1024 * 1024)

# MTProto GetFile: without the "precise" flag a request must not cross a
# 1 MiB boundary, so align the start offset to 1 MiB — at worst ~1 MiB of
# extra download per blob.
_DOWNLOAD_ALIGN = 1024 * 1024
_DOWNLOAD_REQUEST_SIZE = 512 * 1024


class _NeedMoreTail(Exception):
    """Raised when the fetched tail does not cover the data zipfile needs."""


class _TailView(io.RawIOBase):
    """Seekable read-only view of a remote file of which only the tail is
    local. Reads before the tail raise _NeedMoreTail so the caller can retry
    with a bigger tail."""

    def __init__(self, total_size: int, tail_start: int, tail: bytes) -> None:
        self._total = int(total_size)
        self._tail_start = int(tail_start)
        self._tail = tail
        self._pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self._total + offset
        else:
            raise ValueError(f"Unsupported whence: {whence}")
        self._pos = max(0, self._pos)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            end = self._total
        else:
            end = min(self._total, self._pos + size)
        if self._pos >= self._total:
            return b""
        if self._pos < self._tail_start:
            raise _NeedMoreTail(
                f"Read at {self._pos} but tail starts at {self._tail_start}"
            )
        start = self._pos - self._tail_start
        data = self._tail[start : end - self._tail_start]
        self._pos += len(data)
        return data


def _zip_entry_mtime(info: zipfile.ZipInfo) -> int:
    try:
        return max(0, calendar.timegm((*info.date_time, 0, 0, -1)))
    except (ValueError, OverflowError):
        return now_ts()


def parse_members_from_zip_tail(
    tail: bytes,
    tail_start: int,
    total_size: int,
    *,
    blob_folder: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Parse blob members from a zip tail.

    Returns (members, used_embedded_manifest). Each member dict carries
    orig_name / folder_path / rel_path / size / sha256 / mtime /
    member_index / archive_name (no file_key — keys are assigned by the
    caller against the live database).

    Raises _NeedMoreTail when the tail is too short for the central
    directory (or the embedded manifest data).
    """
    view = _TailView(total_size, tail_start, tail)
    archive = zipfile.ZipFile(view)  # raises _NeedMoreTail via reads

    if MANIFEST_ARC_NAME in archive.namelist():
        payload = json.loads(archive.read(MANIFEST_ARC_NAME).decode("utf-8"))
        raw_members = payload.get("members") if isinstance(payload, dict) else None
        members: list[dict[str, Any]] = []
        if isinstance(raw_members, list):
            for item in raw_members:
                if not isinstance(item, dict):
                    continue
                orig_name = sanitize_filename(str(item.get("orig_name") or "").strip())
                if not orig_name:
                    continue
                folder_path = normalize_folder_path(
                    str(item.get("folder_path") or blob_folder)
                )
                members.append(
                    {
                        "orig_name": orig_name,
                        "folder_path": folder_path,
                        "rel_path": str(item.get("rel_path") or ""),
                        "size": int(item.get("size") or 0),
                        "sha256": str(item.get("sha256") or "").strip().lower(),
                        "mtime": int(item.get("mtime") or 0),
                        "member_index": int(item.get("member_index") or 0),
                        "archive_name": str(item.get("archive_name") or ""),
                    }
                )
        if members:
            return members, True

    # Old blobs: reconstruct from the central directory. Entry names are
    # "NNNNN_origname" (index starts at 1); sha256 and per-member folders
    # are unrecoverable without the original manifest.
    members = []
    for order, info in enumerate(archive.infolist()):
        if info.filename == MANIFEST_ARC_NAME or info.is_dir():
            continue
        match = _MEMBER_ARC_RE.match(info.filename)
        if match:
            member_index = int(match.group(1)) - 1
            orig_name = sanitize_filename(match.group(2))
        else:
            member_index = order
            orig_name = sanitize_filename(info.filename)
        if not orig_name:
            orig_name = f"member_{member_index:05d}.bin"
        folder_path = normalize_folder_path(blob_folder)
        members.append(
            {
                "orig_name": orig_name,
                "folder_path": folder_path,
                "rel_path": f"{folder_path}/{orig_name}",
                "size": int(info.file_size),
                "sha256": "",
                "mtime": _zip_entry_mtime(info),
                "member_index": member_index,
                "archive_name": info.filename,
            }
        )
    return members, False


async def _fetch_tail(
    client: Any, document: Any, total_size: int, want_bytes: int
) -> tuple[bytes, int]:
    """Download the last ``want_bytes`` of a document. Returns (tail, tail_start)."""
    tail_start = max(0, total_size - want_bytes)
    aligned = tail_start - (tail_start % _DOWNLOAD_ALIGN)
    chunks: list[bytes] = []
    async for chunk in client.iter_download(
        document, offset=aligned, request_size=_DOWNLOAD_REQUEST_SIZE
    ):
        chunks.append(bytes(chunk))
    buffer = b"".join(chunks)
    return buffer[tail_start - aligned :], tail_start


def _assign_file_key(
    repo: DbRepo,
    config: AppConfig,
    folder_path: str,
    sha256: str,
    local_seen: set[tuple[str, str]],
) -> str:
    if config.use_sha_as_key and len(sha256) == 64:
        candidate = file_key_from_sha256(sha256)
    else:
        candidate = random_file_key(12)
    while True:
        dedup_key = (folder_path, candidate)
        if (
            dedup_key in local_seen
            or repo.get_batch_member(folder_path, candidate) is not None
            or repo.get_parts_for_object(folder_path, candidate)
        ):
            candidate = random_file_key(12)
            continue
        local_seen.add(dedup_key)
        return candidate


async def recover_blob_manifests(
    repo: DbRepo,
    config: AppConfig,
    *,
    client_by_chat_id: dict[str, Any],
    chat_by_chat_id: dict[str, Any] | None = None,
    cancel_token: CancelToken | None = None,
) -> dict[str, int]:
    """Rebuild batch_blobs/batch_members for blob messages that have no local
    manifest (e.g. after moving to a new machine and rescanning channels)."""
    known_blob_keys = set(repo.list_all_blob_keys())
    chat_map = dict(chat_by_chat_id or {})

    orphans: list[tuple[str, int, Any, int | None]] = []
    seen_keys: set[str] = set()
    for row in repo.list_blob_caption_rows():
        meta = parse_batch_blob_caption(
            str(row["caption_raw"] or ""), prefix=config.caption_prefix
        )
        if meta is None:
            continue
        if meta.blob_key in known_blob_keys or meta.blob_key in seen_keys:
            continue
        seen_keys.add(meta.blob_key)
        orphans.append(
            (
                str(row["chat_id"]),
                int(row["msg_id"]),
                meta,
                int(row["file_size"]) if row["file_size"] is not None else None,
            )
        )

    stats = {"orphans": len(orphans), "recovered": 0, "members": 0, "failed": 0}
    if not orphans:
        return stats

    logger.info("Blob manifest recovery: %d orphan blob(s) found", len(orphans))

    for chat_id, msg_id, meta, _file_size in orphans:
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        try:
            client = client_by_chat_id.get(chat_id)
            if client is None:
                raise ValueError(f"No client for chat {chat_id}")
            entity = chat_map.get(chat_id)
            if entity is None:
                from telethon.tl.types import PeerChannel

                entity = await client.get_entity(PeerChannel(int(chat_id)))
                chat_map[chat_id] = entity
            message = await client.get_messages(entity, ids=int(msg_id))
            if message is None or getattr(message, "document", None) is None:
                raise ValueError(f"Blob message {msg_id} has no document")
            total_size = int(message.document.size)

            members: list[dict[str, Any]] | None = None
            used_manifest = False
            for want in _TAIL_LADDER:
                tail, tail_start = await _fetch_tail(
                    client, message.document, total_size, want
                )
                try:
                    members, used_manifest = parse_members_from_zip_tail(
                        tail,
                        tail_start,
                        total_size,
                        blob_folder=meta.folder_path,
                    )
                    break
                except _NeedMoreTail:
                    continue
                except zipfile.BadZipFile as exc:
                    raise ValueError(f"Blob is not a readable zip: {exc}") from exc
            if members is None:
                raise ValueError("Central directory did not fit into the tail window")
            if not members:
                raise ValueError("No members found in blob archive")

            local_seen: set[tuple[str, str]] = set()
            normalized: list[dict[str, Any]] = []
            for item in members:
                file_key = _assign_file_key(
                    repo, config, item["folder_path"], item["sha256"], local_seen
                )
                normalized.append({**item, "file_key": file_key})

            manifest_payload = {
                "version": 2,
                "kind": "tgccm_batch_blob",
                "blob_key": meta.blob_key,
                "orig_name": meta.orig_name,
                "members_count": len(normalized),
                "members": normalized,
                "recovered": True,
                "recovered_source": "embedded_manifest"
                if used_manifest
                else "central_directory",
            }
            repo.upsert_batch_blob(
                blob_key=meta.blob_key,
                folder_path=meta.folder_path,
                chat_id=chat_id,
                msg_id=msg_id,
                blob_name=meta.orig_name,
                blob_size=total_size,
                blob_sha256=None,
                manifest_json=json.dumps(
                    manifest_payload, ensure_ascii=False, separators=(",", ":")
                ),
                is_deleted=0,
            )
            repo.upsert_batch_members_bulk(
                [
                    {
                        "folder_path": member["folder_path"],
                        "file_key": member["file_key"],
                        "blob_key": meta.blob_key,
                        "orig_name": member["orig_name"],
                        "member_index": member["member_index"],
                        "member_size": member["size"],
                        "member_sha256": member["sha256"] or None,
                        "deleted_ts": None,
                        "name_pinned": 0,
                    }
                    for member in normalized
                ]
            )
            stats["recovered"] += 1
            stats["members"] += len(normalized)
            logger.info(
                "Recovered blob %s (%s): %d member(s), source=%s",
                meta.blob_key,
                meta.orig_name,
                len(normalized),
                "embedded manifest" if used_manifest else "central directory",
            )
        except Exception as exc:  # noqa: BLE001 — keep going per blob
            stats["failed"] += 1
            logger.warning(
                "Blob manifest recovery failed: chat=%s msg=%s blob=%s error=%s",
                chat_id,
                msg_id,
                getattr(meta, "blob_key", "?"),
                exc,
            )

    logger.info(
        "Blob manifest recovery done: recovered=%d members=%d failed=%d",
        stats["recovered"],
        stats["members"],
        stats["failed"],
    )
    return stats
