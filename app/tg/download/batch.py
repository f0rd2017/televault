from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any
import zipfile


from app.core.jobs import CancelToken
from app.core.utils import (
    build_safe_output_path,
    ensure_dir,
)
from app.tg.download._common import (
    _sha256_file_sync,
)

logger = logging.getLogger(__name__)


class _DownloadBatchMixin:
    async def _download_batch_member(
        self,
        *,
        folder_path: str,
        file_key: str,
        integrity_mode: str,
        cancel_token: CancelToken,
        progress_cb=None,
        dest_root: str | None = None,
    ) -> dict[str, object]:
        started = time.monotonic()
        member = self.repo.get_batch_member(folder_path, file_key)
        if member is None or member.deleted_ts is not None:
            raise ValueError("Batch member is missing or deleted")
        blob = self.repo.get_batch_blob(member.blob_key)
        if blob is None or blob.is_deleted:
            raise ValueError("Batch blob is missing or deleted")

        manifest_payload: dict[str, Any] = {}
        try:
            parsed = json.loads(blob.manifest_json)
            if isinstance(parsed, dict):
                manifest_payload = parsed
        except json.JSONDecodeError:
            manifest_payload = {}

        selected_manifest_member: dict[str, Any] | None = None
        manifest_members = manifest_payload.get("members")
        if isinstance(manifest_members, list):
            for item in manifest_members:
                if not isinstance(item, dict):
                    continue
                if (
                    str(item.get("file_key") or "") == member.file_key
                    and str(item.get("folder_path") or "") == member.folder_path
                ):
                    selected_manifest_member = item
                    break
        cache_path, blob_reused = await self._ensure_blob_cached(
            blob, member.blob_key, cancel_token, dest_root=dest_root
        )

        extract_started = time.monotonic()
        with zipfile.ZipFile(cache_path, "r") as archive:
            sorted_names = sorted(archive.namelist())
            output_path = self._extract_archive_member(
                archive,
                member=member,
                manifest_member=selected_manifest_member,
                sorted_names=sorted_names,
                cancel_token=cancel_token,
                dest_root=dest_root,
            )
        extract_elapsed = max(0.0, time.monotonic() - extract_started)
        if progress_cb is not None:
            await progress_cb(100.0, "Downloaded from batch blob")

        output_bytes = int(output_path.stat().st_size)
        digest_hex: str | None = None
        verified = True
        if integrity_mode == "strict" and member.member_sha256:
            digest_hex = await asyncio.to_thread(
                _sha256_file_sync,
                output_path,
                self._MERGE_BUFFER_SIZE,
            )
            if str(digest_hex).lower() != str(member.member_sha256).lower():
                output_path.unlink(missing_ok=True)
                raise ValueError("Batch member integrity mismatch")

        elapsed = max(0.001, time.monotonic() - started)
        analytics = self._build_download_analytics(
            phase_seconds={
                "parts_fetch": 0.0,
                "messages_fetch": 0.0,
                "resume_validate": 0.0,
                "network_download": 0.0 if blob_reused else elapsed - extract_elapsed,
                "decrypt": 0.0,
                "manifest_write": 0.0,
                "merge": extract_elapsed,
                "integrity_check": 0.0,
                "transfer": elapsed,
                "total": elapsed,
            },
            output_total_bytes=output_bytes,
            resume_completed_bytes=0,
            transfer_elapsed=elapsed,
            total_elapsed=elapsed,
            download_profile={
                "channels_used": [str(blob.chat_id)],
                "parts_by_channel": {str(blob.chat_id): 1},
                "clients_used": [],
                "cross_channel_parts": False,
            },
            requests_per_file=0.0 if blob_reused else 1.0,
            batch_hit_ratio=1.0,
            blob_reuse_ratio=1.0 if blob_reused else 0.0,
            effective_part_concurrency=1,
            effective_stride_streams=1,
            adaptive={},
        )
        return {
            "output_path": str(output_path),
            "sha256": digest_hex,
            "verified": bool(verified),
            "expected_sha256": str(member.member_sha256)
            if member.member_sha256
            else None,
            "integrity_mode": "strict_member_sha256"
            if integrity_mode == "strict"
            else "batch_fast",
            "integrity_error": None,
            "parts_downloaded": 1,
            "parts_expected": 1,
            "channels_used": [str(blob.chat_id)],
            "clients_used": [],
            "cross_channel_parts": False,
            "analytics": analytics,
        }

    async def _ensure_blob_cached(
        self,
        blob,
        blob_key: str,
        cancel_token: CancelToken,
        dest_root: str | None = None,
    ) -> tuple[Path, bool]:
        """Download the blob archive once into the shared blob cache (locked) and
        return (cache_path, reused). Reused = it was already cached."""
        blob_cache_dir = ensure_dir(Path(self.config.cache_dir) / ".batch_blob_cache")
        cache_path = Path(blob_cache_dir) / f"{blob_key}.zip"

        blob_lock = self._blob_cache_locks.get(blob_key)
        if blob_lock is None:
            blob_lock = asyncio.Lock()
            self._blob_cache_locks[blob_key] = blob_lock

        blob_reused = False
        async with blob_lock:
            if (
                cache_path.exists()
                and cache_path.is_file()
                and cache_path.stat().st_size > 0
            ):
                blob_reused = True
            else:
                blob_download = await self.chunked_download(
                    blob.folder_path,
                    blob_key,
                    allow_incomplete=False,
                    integrity_mode="fast",
                    cancel_token=cancel_token,
                    progress_cb=None,
                    _storage_override="regular",
                    dest_root=dest_root,
                )
                blob_output_path = Path(
                    str(blob_download.get("output_path") or "")
                ).resolve()
                if not blob_output_path.exists():
                    raise FileNotFoundError(
                        f"Blob cache source not found: {blob_output_path}"
                    )
                if blob_output_path != cache_path:
                    shutil.copyfile(blob_output_path, cache_path)
        cancel_token.raise_if_cancelled()
        return cache_path, blob_reused

    def _extract_archive_member(
        self,
        archive: zipfile.ZipFile,
        *,
        member,
        manifest_member: dict[str, Any] | None,
        sorted_names: list[str],
        cancel_token: CancelToken,
        dest_root: str | None = None,
    ) -> Path:
        """Extract one member from an already-open blob archive to its output
        path. Resolves the archive entry by manifest archive_name, else index."""
        output_path = build_safe_output_path(
            dest_root or self.config.download_root, member.folder_path, member.orig_name
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        target_name: str | None = None
        if isinstance(manifest_member, dict):
            candidate_name = str(manifest_member.get("archive_name") or "").strip()
            if candidate_name and candidate_name in sorted_names:
                target_name = candidate_name
        if target_name is None:
            if member.member_index < 0 or member.member_index >= len(sorted_names):
                raise ValueError(
                    "Batch manifest mismatch: member index is out of archive bounds"
                )
            target_name = sorted_names[member.member_index]

        with archive.open(target_name, "r") as src, output_path.open("wb") as dst:
            while True:
                cancel_token.raise_if_cancelled()
                chunk = src.read(self._MERGE_BUFFER_SIZE)
                if not chunk:
                    break
                dst.write(chunk)
        return output_path

    async def download_blob_members(
        self,
        *,
        blob_key: str,
        member_file_keys: list[str],
        integrity_mode: str,
        cancel_token: CancelToken,
        progress_cb=None,
    ) -> dict[str, object]:
        """Download ONE blob and extract many of its members in a single job —
        used for folder downloads so a 1000-file folder is a few blob jobs, not
        a thousand per-file jobs (which floods/freezes the UI)."""
        started = time.monotonic()
        blob = self.repo.get_batch_blob(blob_key)
        if blob is None or blob.is_deleted:
            raise ValueError("Batch blob is missing or deleted")

        manifest_by_key: dict[str, dict[str, Any]] = {}
        try:
            parsed = json.loads(blob.manifest_json)
            if isinstance(parsed, dict) and isinstance(parsed.get("members"), list):
                for item in parsed["members"]:
                    if isinstance(item, dict) and item.get("file_key"):
                        manifest_by_key[str(item["file_key"])] = item
        except json.JSONDecodeError:
            manifest_by_key = {}

        requested = {str(k) for k in member_file_keys}
        members = [
            m
            for m in self.repo.list_batch_members_by_blob(blob_key)
            if m.deleted_ts is None and m.file_key in requested
        ]
        if not members:
            raise ValueError("No batch members to download for this blob")

        cache_path, blob_reused = await self._ensure_blob_cached(
            blob, blob_key, cancel_token
        )

        total = len(members)
        output_bytes = 0
        extracted = 0
        extract_started = time.monotonic()
        with zipfile.ZipFile(cache_path, "r") as archive:
            sorted_names = sorted(archive.namelist())
            for member in members:
                cancel_token.raise_if_cancelled()
                output_path = self._extract_archive_member(
                    archive,
                    member=member,
                    manifest_member=manifest_by_key.get(member.file_key),
                    sorted_names=sorted_names,
                    cancel_token=cancel_token,
                )
                if integrity_mode == "strict" and member.member_sha256:
                    digest_hex = await asyncio.to_thread(
                        _sha256_file_sync, output_path, self._MERGE_BUFFER_SIZE
                    )
                    if str(digest_hex).lower() != str(member.member_sha256).lower():
                        output_path.unlink(missing_ok=True)
                        raise ValueError(
                            f"Batch member integrity mismatch: {member.orig_name}"
                        )
                output_bytes += int(output_path.stat().st_size)
                extracted += 1
                if progress_cb is not None:
                    await progress_cb(
                        100.0 * extracted / total,
                        f"Extracted {extracted}/{total} from blob",
                    )
        extract_elapsed = max(0.0, time.monotonic() - extract_started)
        elapsed = max(0.001, time.monotonic() - started)
        analytics = self._build_download_analytics(
            phase_seconds={
                "parts_fetch": 0.0,
                "messages_fetch": 0.0,
                "resume_validate": 0.0,
                "network_download": 0.0 if blob_reused else elapsed - extract_elapsed,
                "decrypt": 0.0,
                "manifest_write": 0.0,
                "merge": extract_elapsed,
                "integrity_check": 0.0,
                "transfer": elapsed,
                "total": elapsed,
            },
            output_total_bytes=output_bytes,
            resume_completed_bytes=0,
            transfer_elapsed=elapsed,
            total_elapsed=elapsed,
            download_profile={
                "channels_used": [str(blob.chat_id)],
                "parts_by_channel": {str(blob.chat_id): 1},
                "clients_used": [],
                "cross_channel_parts": False,
            },
            requests_per_file=0.0 if blob_reused else 1.0,
            batch_hit_ratio=1.0,
            blob_reuse_ratio=1.0 if blob_reused else 0.0,
            effective_part_concurrency=1,
            effective_stride_streams=1,
            adaptive={},
        )
        return {
            "blob_key": blob_key,
            "downloaded_members": extracted,
            "members_expected": total,
            "integrity_mode": "strict_member_sha256"
            if integrity_mode == "strict"
            else "batch_fast",
            "channels_used": [str(blob.chat_id)],
            "clients_used": [],
            "output_total_bytes": output_bytes,
            "analytics": analytics,
        }
