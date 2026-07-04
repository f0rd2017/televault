from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any


from app.core.jobs import CancelToken
from app.tg import compression
from app.core.types import (
    BatchBlobCaption,
)
from app.core.utils import (
    file_key_from_sha256,
    normalize_folder_path,
    random_file_key,
    sanitize_filename,
)
from app.tg.parser import build_batch_blob_caption

logger = logging.getLogger(__name__)


@dataclass
class _PreparedBatch:
    """A batch ready to send: either a single file (no zip) or an assembled
    archive. Archiving happens ahead of time (pipeline stage); sending is a
    separate stage."""

    kind: str  # "single" | "archive"
    upload_path: str
    folder_path: str
    payload_bytes: int
    archive_name: str | None = None
    archive_path: Path | None = None
    temp_dir: Path | None = None
    manifest_members: list[dict[str, Any]] = field(default_factory=list)
    archive_elapsed: float = 0.0

    def cleanup(self) -> None:
        if self.temp_dir is not None:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = None


class _SmallBatchMixin:
    def _normalize_group_items(
        self,
        file_paths: list[str],
        folder_path: str,
        member_folder_paths: list[str] | None,
    ) -> tuple[list[tuple[str, str]], str]:
        source_folder = normalize_folder_path(folder_path)
        folder_hints = list(member_folder_paths or [])
        normalized_items: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for idx, raw in enumerate(file_paths):
            raw_path = str(raw or "").strip()
            if not raw_path:
                continue
            candidate = Path(raw_path).expanduser().resolve()
            key = str(candidate)
            member_folder = source_folder
            if idx < len(folder_hints):
                hinted_folder = str(folder_hints[idx] or "").strip()
                if hinted_folder:
                    member_folder = normalize_folder_path(hinted_folder)
            dedup_key = (key, member_folder)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            if not candidate.exists() or not candidate.is_file():
                # The file may have been deleted/moved between job creation and
                # the start of the upload. Skip it rather than failing the whole
                # batch — the group's other valid files should still upload.
                logger.warning("Skipping missing file in upload group: %s", candidate)
                continue
            normalized_items.append((key, member_folder))
        return normalized_items, source_folder

    async def _prepare_group_batch(
        self,
        normalized_items: list[tuple[str, str]],
        source_folder: str,
        token: CancelToken,
    ) -> _PreparedBatch:
        """Pipeline archiving stage: build a zip (on a thread) or, for a single
        file, hand it back as-is. Does not send anything — only prepares the payload."""

        def _payload_bytes(items: list[tuple[str, str]]) -> int:
            total = 0
            for path, _folder in items:
                try:
                    total += int(Path(path).stat().st_size)
                except OSError:
                    pass
            return total

        if len(normalized_items) == 1:
            path, member_folder = normalized_items[0]
            return _PreparedBatch(
                kind="single",
                upload_path=path,
                folder_path=member_folder,
                payload_bytes=_payload_bytes(normalized_items),
            )

        temp_dir = Path(tempfile.mkdtemp(prefix="tgccm_small_batch_"))
        archive_name = sanitize_filename(
            f"batch_{len(normalized_items)}_files_{int(time.time() * 1000)}.zip"
        )
        archive_path = temp_dir / archive_name
        archive_started = time.monotonic()
        try:
            manifest_members = await asyncio.to_thread(
                compression.build_group_archive,
                normalized_items,
                archive_path,
                token,
            )
        except BaseException:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        archive_elapsed = max(0.0, time.monotonic() - archive_started)
        logger.info(
            "Small-file batch archive built: files=%d path=%s size=%d bytes took=%.3fs",
            len(normalized_items),
            archive_path.name,
            int(archive_path.stat().st_size),
            archive_elapsed,
        )
        return _PreparedBatch(
            kind="archive",
            upload_path=str(archive_path),
            folder_path=source_folder,
            payload_bytes=_payload_bytes(normalized_items),
            archive_name=archive_name,
            archive_path=archive_path,
            temp_dir=temp_dir,
            manifest_members=manifest_members,
            archive_elapsed=archive_elapsed,
        )

    async def _upload_prepared_batch(
        self,
        prepared: _PreparedBatch,
        token: CancelToken,
        progress_cb=None,
    ) -> dict[str, object]:
        """Pipeline send stage: upload an already-prepared payload (single file
        or assembled archive), index the overlay, and clean up temp files."""
        try:
            result = await self.chunked_upload(
                prepared.upload_path,
                prepared.folder_path,
                cancel_token=token,
                progress_cb=progress_cb,
            )
            if prepared.kind == "single":
                return result
            await self._index_small_batch_overlay(
                upload_result=result,
                source_folder=prepared.folder_path,
                archive_name=str(prepared.archive_name),
                archive_path=prepared.archive_path,  # type: ignore[arg-type]
                manifest_members=prepared.manifest_members,
            )
        finally:
            prepared.cleanup()

        files_count = len(prepared.manifest_members)
        analytics = result.get("analytics")
        if isinstance(analytics, dict):
            analytics["small_batch"] = {
                "enabled": True,
                "version": 2,
                "files_count": int(files_count),
                "archive_build_seconds": float(prepared.archive_elapsed),
                "archive_name": prepared.archive_name,
                "mode": "transparent_files",
            }
        result["small_batch"] = {
            "version": 2,
            "files_count": int(files_count),
            "archive_name": prepared.archive_name,
        }
        return result

    async def chunked_upload_group(
        self,
        file_paths: list[str],
        folder_path: str,
        member_folder_paths: list[str] | None = None,
        cancel_token: CancelToken | None = None,
        progress_cb=None,
    ) -> dict[str, object]:
        token = cancel_token or CancelToken()
        normalized_items, source_folder = self._normalize_group_items(
            file_paths, folder_path, member_folder_paths
        )
        if not normalized_items:
            raise ValueError("No files to upload")
        prepared = await self._prepare_group_batch(
            normalized_items, source_folder, token
        )
        return await self._upload_prepared_batch(prepared, token, progress_cb)

    async def chunked_upload_session(
        self,
        batches: list[dict[str, Any]],
        cancel_token: CancelToken | None = None,
        progress_cb=None,
    ) -> dict[str, object]:
        """Pipeline for multiple small batches from a single drop: a background
        archiver prepares batch N+1 while batch N is being uploaded across all
        accounts. This way the network doesn't sit idle during zip building,
        and uploads don't compete with each other for accounts."""
        token = cancel_token or CancelToken()

        plan: list[tuple[list[tuple[str, str]], str]] = []
        for batch in batches:
            raw_paths = batch.get("file_paths") or []
            if not raw_paths and batch.get("file_path"):
                raw_paths = [batch["file_path"]]
            items, source_folder = self._normalize_group_items(
                list(raw_paths),
                str(batch.get("folder_path") or ""),
                batch.get("member_folder_paths"),
            )
            if items:
                plan.append((items, source_folder))
        if not plan:
            raise ValueError("No files to upload")

        total_source_bytes = max(
            1,
            sum(
                int(Path(path).stat().st_size)
                for items, _folder in plan
                for path, _f in items
                if Path(path).exists()
            ),
        )

        # Bounded look-ahead: archive at most ARCHIVE_AHEAD batches before the
        # uploader consumes them (keeps disk/temp usage bounded).
        ready_q: asyncio.Queue = asyncio.Queue(maxsize=1)

        async def producer() -> None:
            for items, source_folder in plan:
                token.raise_if_cancelled()
                prepared = await self._prepare_group_batch(items, source_folder, token)
                await ready_q.put(prepared)
            await ready_q.put(None)

        producer_task = asyncio.create_task(producer())
        results: list[dict[str, object]] = []
        done_bytes = 0
        try:
            while True:
                prepared = await ready_q.get()
                if prepared is None:
                    break
                batch_bytes = max(0, int(prepared.payload_bytes))

                async def scaled_progress(
                    percent: float,
                    message: str,
                    *,
                    _base: int = done_bytes,
                    _bytes: int = batch_bytes,
                ) -> None:
                    if progress_cb is None:
                        return
                    overall = _base + _bytes * max(0.0, min(100.0, percent)) / 100.0
                    await progress_cb(100.0 * overall / total_source_bytes, message)

                try:
                    res = await self._upload_prepared_batch(
                        prepared, token, scaled_progress
                    )
                except BaseException:
                    prepared.cleanup()
                    raise
                results.append(res)
                done_bytes += batch_bytes
            await producer_task
        except BaseException:
            producer_task.cancel()
            with contextlib.suppress(BaseException):
                await producer_task
            # Drain & clean any archives the producer already staged.
            while not ready_q.empty():
                staged = ready_q.get_nowait()
                if isinstance(staged, _PreparedBatch):
                    staged.cleanup()
            raise

        return self._aggregate_session_result(results, total_source_bytes)

    @staticmethod
    def _aggregate_session_result(
        results: list[dict[str, object]], total_source_bytes: int
    ) -> dict[str, object]:
        channels: set[str] = set()
        clients: set[str] = set()
        total_time = 0.0
        for res in results:
            for ch in res.get("channels_used", []) or []:
                channels.add(str(ch))
            for cl in res.get("clients_used", []) or []:
                clients.add(str(cl))
            analytics = res.get("analytics")
            if isinstance(analytics, dict):
                total_time += float(
                    analytics.get("phase_seconds", {}).get("total", 0.0)
                )
        speed = (
            float(total_source_bytes) / total_time / (1024.0 * 1024.0)
            if total_time > 0
            else 0.0
        )
        return {
            "session": True,
            "batches": int(len(results)),
            "channels_used": sorted(channels),
            "clients_used": sorted(clients),
            "analytics": {
                "speed_mbps": {"transfer_payload": speed},
                "phase_seconds": {"total": total_time},
                "bytes": {"source_total": int(total_source_bytes)},
            },
        }

    async def _index_small_batch_overlay(
        self,
        *,
        upload_result: dict[str, object],
        source_folder: str,
        archive_name: str,
        archive_path: Path,
        manifest_members: list[dict[str, Any]],
    ) -> None:
        blob_key = str(upload_result.get("file_key") or "").strip()
        if not blob_key:
            return
        blob_parts = self.repo.get_parts_for_object(source_folder, blob_key)
        if not blob_parts:
            return
        first_part = sorted(blob_parts, key=lambda part: int(part.part_index))[0]
        blob_size = int(archive_path.stat().st_size)
        normalized_members: list[dict[str, Any]] = []
        local_seen: set[tuple[str, str]] = set()
        for item in manifest_members:
            folder_path = normalize_folder_path(
                str(item.get("folder_path") or source_folder)
            )
            member_sha = str(item.get("sha256") or "").strip().lower()
            if len(member_sha) != 64:
                continue
            if self.config.use_sha_as_key:
                candidate_key = file_key_from_sha256(member_sha)
            else:
                candidate_key = random_file_key(12)
            dedup_counter = 0
            while True:
                dedup_key = (folder_path, candidate_key)
                if dedup_key in local_seen:
                    dedup_counter += 1
                    candidate_key = random_file_key(12)
                    continue
                if self.repo.get_batch_member(folder_path, candidate_key) is not None:
                    dedup_counter += 1
                    candidate_key = random_file_key(12)
                    continue
                if self.repo.get_parts_for_object(folder_path, candidate_key):
                    dedup_counter += 1
                    candidate_key = random_file_key(12)
                    continue
                local_seen.add(dedup_key)
                break

            normalized_members.append(
                {
                    "file_key": candidate_key,
                    "orig_name": sanitize_filename(
                        str(item.get("orig_name") or "file.bin")
                    ),
                    "folder_path": folder_path,
                    "rel_path": str(item.get("rel_path") or ""),
                    "size": int(item.get("size") or 0),
                    "sha256": member_sha,
                    "mtime": int(item.get("mtime") or 0),
                    "member_index": int(item.get("member_index") or 0),
                    "archive_name": str(item.get("archive_name") or ""),
                }
            )

        if not normalized_members:
            return

        manifest_payload = {
            "version": 2,
            "kind": "tgccm_batch_blob",
            "blob_key": blob_key,
            "orig_name": str(archive_name),
            "members_count": int(len(normalized_members)),
            "members": normalized_members,
        }
        manifest_json = json.dumps(
            manifest_payload, ensure_ascii=False, separators=(",", ":")
        )
        manifest_sha = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
        self.repo.upsert_batch_blob(
            blob_key=blob_key,
            folder_path=source_folder,
            chat_id=str(first_part.chat_id),
            msg_id=int(first_part.msg_id),
            blob_name=str(archive_name),
            blob_size=blob_size,
            blob_sha256=None,
            manifest_json=manifest_json,
            is_deleted=0,
        )
        member_rows = [
            {
                "folder_path": str(member["folder_path"]),
                "file_key": str(member["file_key"]),
                "blob_key": blob_key,
                "orig_name": str(member["orig_name"]),
                "member_index": int(member["member_index"]),
                "member_size": int(member["size"]),
                "member_sha256": str(member["sha256"]),
                "deleted_ts": None,
                "name_pinned": 0,
            }
            for member in normalized_members
        ]
        self.repo.upsert_batch_members_bulk(member_rows)

        # Replace-by-name for small files: the just-uploaded member supersedes
        # older versions of the same file (same folder+name, different key).
        # Without this, an updated small file would leave a duplicate behind,
        # since the session path bypasses replace-by-name in worker.py.
        superseded = 0
        for member in member_rows:
            superseded += self.repo.supersede_batch_members_by_name(
                str(member["folder_path"]),
                str(member["orig_name"]),
                str(member["file_key"]),
            )
        if superseded:
            logger.info(
                "Batch replace-by-name: superseded %d old small-file version(s)",
                superseded,
            )

        blob_caption = build_batch_blob_caption(
            BatchBlobCaption(
                version=2,
                kind="tgccm_batch_blob",
                folder_path=source_folder,
                blob_key=blob_key,
                orig_name=str(archive_name),
                members_count=int(len(normalized_members)),
                manifest_sha256=manifest_sha,
            ),
            prefix=self.config.caption_prefix,
        )
        for part in blob_parts:
            self.repo.update_caption_raw(
                int(part.msg_id), blob_caption, chat_id=str(part.chat_id)
            )
            try:
                route_client = self.client
                for candidate in self._client_pool:
                    if str(self._chat_id_for_client(candidate)) == str(part.chat_id):
                        route_client = candidate
                        break
                route_chat = await self._chat_for_client(route_client)
                await route_client.edit_message(
                    route_chat, int(part.msg_id), text=blob_caption
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to update batch blob caption remotely: msg_id=%s chat_id=%s error=%s",
                    part.msg_id,
                    part.chat_id,
                    exc,
                )
        self.repo.rebuild_object_aggregate(self.chat_id, source_folder, blob_key)

        analytics = upload_result.get("analytics")
        if isinstance(analytics, dict):
            analytics["small_batch_v2"] = {
                "members_count": int(len(normalized_members)),
                "manifest_sha256": manifest_sha,
                "blob_key": blob_key,
            }
