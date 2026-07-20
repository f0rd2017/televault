"""Upload drop mixin: file drop handling, upload queue, path expansion."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QFileDialog, QMessageBox

from televault.core.types import JobType
from televault.core.utils import normalize_folder_path


class UploadDropMixin:
    """Methods for file drop handling and upload queue management."""

    def _on_files_dropped(self, paths: list[str]) -> None:
        # If no folder is open, only allow dropping folders (they create their own structure at root)
        has_dir = any(Path(p).is_dir() for p in paths)
        if not self.current_folder and not has_dir:
            QMessageBox.warning(
                self,
                self.tr("Upload"),
                self.tr("Open a target folder first, then drop the files"),
            )
            return

        base_folder = self.current_folder or ""

        try:
            expanded = self._expand_drop_paths(paths, base_folder)
        except Exception as exc:  # noqa: BLE001
            self.progress_widget.append_log(f"Failed to process dropped files: {exc}")
            return
        if expanded:
            self._queue_drop_items(expanded, base_folder)

    def _queue_drop_items(self, items: list[tuple[str, str]], base_folder: str) -> None:
        """Called on the Qt main thread after background enumeration completes."""
        jobs, stats = self._build_pending_upload_jobs(items, source="drop")
        self._pending_upload_jobs.extend(jobs)
        cloud_target = (
            f"Cloud:/{base_folder.strip('/')}" if base_folder.strip("/") else "Cloud:/"
        )
        batched_jobs = int(stats.get("batched_jobs", 0))
        batched_files = int(stats.get("batched_files", 0))
        self.progress_widget.append_log(
            (
                f"Dropped {len(items)} file(s) for upload into {cloud_target} "
                f"(jobs queued: {len(jobs)}"
                + (
                    f", small batches: {batched_jobs} / files: {batched_files}"
                    if batched_jobs > 0
                    else ""
                )
                + ")"
            )
        )
        skipped = int(stats.get("skipped_files", 0))
        if skipped > 0:
            self.progress_widget.append_log(
                f"Skipped missing/unreadable dropped files: {skipped}"
            )
        truncated_segments = int(stats.get("trimmed_folder_segments", 0))
        if truncated_segments > 0:
            self.progress_widget.append_log(
                (
                    f"Auto-shortened {truncated_segments} long folder segment(s) "
                    f"to fit Telegram path limits"
                )
            )
        self._start_next_pending_upload()

    @staticmethod
    def _expand_drop_paths(paths: list[str], base_folder: str) -> list[tuple[str, str]]:
        """Expand dropped paths into (file_path, target_folder) pairs.

        Files dropped directly go into base_folder.
        Files inside a dropped folder preserve their relative structure:
          base_folder / dropped_folder_name / relative_subpath
        """
        result: list[tuple[str, str]] = []
        for raw in paths:
            path = Path(raw)
            if path.is_file():
                result.append((str(path), base_folder))
            elif path.is_dir():
                # root of the dropped folder becomes a subfolder of base_folder
                drop_root = path.parent
                for child in path.rglob("*"):
                    if not child.is_file():
                        continue
                    relative = child.relative_to(drop_root)
                    # relative = "FolderName/sub/file.txt"
                    # target folder = base_folder / "FolderName" / "sub"
                    rel_dir = relative.parent
                    if str(rel_dir) == ".":
                        target = base_folder
                    else:
                        rel_posix = rel_dir.as_posix()
                        target = f"{base_folder}/{rel_posix}"
                    result.append((str(child), target))
        return result

    def _start_next_pending_upload(self) -> None:
        small_limit = max(
            1,
            min(
                int(self.config.small_upload_parallel_jobs),
                int(self.config.max_active_jobs),
            ),
        )
        small_inflight = self._count_inflight_small_uploads()
        while (
            self._pending_upload_jobs
            and len(self._inflight_requests) < self.config.max_active_jobs
        ):
            launch_index: int | None = None
            for idx, payload in enumerate(self._pending_upload_jobs):
                is_small = bool(payload.get("_ui_small_upload"))
                if is_small and small_inflight >= small_limit:
                    continue
                launch_index = idx
                break
            if launch_index is None:
                break
            payload = self._pending_upload_jobs.pop(launch_index)
            if bool(payload.get("_ui_small_upload")):
                small_inflight += 1
            self._enqueue_upload_job(payload)

    def _enqueue_upload_job(self, payload: dict[str, Any]) -> None:
        self._enqueue_job(JobType.UPLOAD.value, payload)

    def _coalesce_small_batches_into_session(
        self, jobs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Merge all small batches/files from one drop into a single upload
        session, so the background archiver prepares the next batch while the
        current one is being sent (a pipeline, with no network idle time for
        zip assembly). Large files stay as separate jobs. With <2 small jobs,
        nothing changes — no pipeline needed."""
        small_jobs = [j for j in jobs if str(j.get("_lane")) == "upload_small"]
        if len(small_jobs) < 2:
            return jobs
        other_jobs = [j for j in jobs if str(j.get("_lane")) != "upload_small"]

        batches: list[dict[str, Any]] = []
        total_bytes = 0
        source = "drop"
        for j in small_jobs:
            source = str(j.get("source") or source)
            total_bytes += int(j.get("_ui_total_bytes", 0))
            if j.get("_ui_small_batch") and j.get("file_paths"):
                batches.append(
                    {
                        "file_paths": list(j.get("file_paths") or []),
                        "folder_path": j.get("folder_path"),
                        "member_folder_paths": list(j.get("member_folder_paths") or []),
                    }
                )
            else:
                batches.append(
                    {
                        "file_paths": [j.get("file_path")],
                        "folder_path": j.get("folder_path"),
                        "member_folder_paths": [j.get("folder_path")],
                    }
                )

        session_job = {
            "_ui_small_session": True,
            "batches": batches,
            "folder_path": batches[0].get("folder_path"),
            "source": source,
            "_ui_small_upload": True,
            "_ui_total_bytes": int(total_bytes),
            "_lane": "upload_small",
        }
        return other_jobs + [session_job]

    def _build_pending_upload_jobs(
        self,
        items: list[tuple[str, str]],
        *,
        source: str,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        threshold_bytes = max(1, int(self.config.small_file_threshold_kb) * 1024)
        batch_target_bytes = max(
            threshold_bytes,
            int(self.config.small_file_batch_target_mb) * 1024 * 1024,
        )
        batching_enabled = bool(self.config.small_file_batching_enabled)
        batch_mode = (
            str(getattr(self.config, "small_batch_mode", "global") or "global")
            .strip()
            .lower()
        )
        if batch_mode not in {"global", "per_folder"}:
            batch_mode = "global"
        batch_max_files = max(
            2, int(getattr(self.config, "small_batch_max_files", 256))
        )

        jobs: list[dict[str, Any]] = []
        stats = {
            "accepted_files": 0,
            "skipped_files": 0,
            "batched_jobs": 0,
            "batched_files": 0,
            "trimmed_folder_segments": 0,
        }

        batch_items: list[tuple[str, str, int]] = []
        batch_folder: str | None = None
        batch_bytes = 0

        def flush_small_batch() -> None:
            nonlocal batch_items, batch_folder, batch_bytes
            if not batch_items or batch_folder is None:
                batch_items = []
                batch_folder = None
                batch_bytes = 0
                return
            if len(batch_items) >= 2 and batching_enabled:
                batch_paths = [item[0] for item in batch_items]
                member_folders = [item[1] for item in batch_items]
                jobs.append(
                    {
                        "file_paths": list(batch_paths),
                        "folder_path": batch_folder,
                        "member_folder_paths": list(member_folders),
                        "source": source,
                        "_ui_small_upload": True,
                        "_ui_small_batch": True,
                        "_ui_small_batch_mode": batch_mode,
                        "_ui_small_batch_files": int(len(batch_items)),
                        "_ui_small_batch_bytes": int(batch_bytes),
                        "_ui_total_bytes": int(batch_bytes),
                        "_lane": "upload_small",
                    }
                )
                stats["batched_jobs"] += 1
                stats["batched_files"] += len(batch_items)
            else:
                jobs.append(
                    {
                        "file_path": batch_items[0][0],
                        "folder_path": batch_folder,
                        "source": source,
                        "_ui_small_upload": True,
                        "_ui_total_bytes": int(max(0, batch_items[0][2])),
                        "_lane": "upload_small",
                    }
                )
            batch_items = []
            batch_folder = None
            batch_bytes = 0

        for file_path, folder_path in items:
            try:
                resolved = Path(file_path).expanduser().resolve()
                file_size = int(resolved.stat().st_size)
            except Exception:
                stats["skipped_files"] += 1
                continue
            if not resolved.is_file():
                stats["skipped_files"] += 1
                continue
            try:
                normalized_folder, trimmed_count = self._coerce_upload_folder_path(
                    folder_path
                )
            except Exception:
                stats["skipped_files"] += 1
                continue

            stats["accepted_files"] += 1
            stats["trimmed_folder_segments"] += int(trimmed_count)
            normalized_file = str(resolved)
            is_small = file_size <= threshold_bytes
            if batching_enabled and is_small:
                same_folder = (
                    (batch_folder == normalized_folder)
                    if batch_folder is not None
                    else True
                )
                would_overflow = (
                    (batch_bytes + file_size) > batch_target_bytes
                    if batch_items
                    else False
                )
                files_limit_reached = len(batch_items) >= batch_max_files
                must_flush = bool(
                    batch_items and (would_overflow or files_limit_reached)
                )
                if batch_mode == "per_folder" and batch_items and not same_folder:
                    must_flush = True
                if must_flush:
                    flush_small_batch()
                if not batch_items:
                    batch_folder = normalized_folder
                batch_items.append((normalized_file, normalized_folder, file_size))
                batch_bytes += max(0, file_size)
                continue

            flush_small_batch()
            jobs.append(
                {
                    "file_path": normalized_file,
                    "folder_path": normalized_folder,
                    "source": source,
                    "_ui_small_upload": bool(is_small),
                    "_ui_total_bytes": int(max(0, file_size)),
                    "_lane": "upload_small" if is_small else "upload_large",
                }
            )

        flush_small_batch()
        return jobs, stats

    @classmethod
    def _coerce_upload_folder_path(cls, folder_path: str) -> tuple[str, int]:
        raw = str(folder_path or "").strip()
        if not raw:
            raise ValueError("Folder path cannot be empty")
        try:
            return normalize_folder_path(raw), 0
        except ValueError as exc:
            if "Folder segment is too long" not in str(exc):
                raise

        trimmed_segments = 0
        sanitized_parts: list[str] = []
        for segment in raw.replace("\\", "/").split("/"):
            part = str(segment or "").strip()
            if not part:
                continue
            if part in {".", ".."}:
                raise ValueError("Relative path segments are not allowed")
            if len(part) > cls._FOLDER_SEGMENT_MAX_LEN:
                digest = hashlib.sha1(part.encode("utf-8")).hexdigest()[
                    : cls._FOLDER_SEGMENT_HASH_LEN
                ]
                prefix_len = max(
                    1, cls._FOLDER_SEGMENT_MAX_LEN - (cls._FOLDER_SEGMENT_HASH_LEN + 1)
                )
                part = f"{part[:prefix_len]}_{digest}"
                trimmed_segments += 1
            sanitized_parts.append(part)

        if not sanitized_parts:
            raise ValueError("Folder path cannot be empty")
        normalized = normalize_folder_path("/".join(sanitized_parts))
        return normalized, int(trimmed_segments)

    def _count_inflight_small_uploads(self) -> int:
        total = 0
        for meta in self._inflight_request_meta.values():
            if str(meta.get("job_type")) != JobType.UPLOAD.value:
                continue
            if bool(meta.get("small_upload")):
                total += 1
        return total

    def _on_upload(self) -> None:
        if not self.current_folder:
            QMessageBox.warning(self, self.tr("Upload"), self.tr("Open a folder first"))
            return

        file_paths, _ = QFileDialog.getOpenFileNames(
            self, self.tr("Select files to upload")
        )
        if not file_paths:
            return

        folder = self.current_folder
        assert folder is not None

        queued_items = [(path, folder) for path in file_paths]
        jobs, stats = self._build_pending_upload_jobs(queued_items, source="picker")
        self._pending_upload_jobs.extend(jobs)
        skipped = int(stats.get("skipped_files", 0))
        if jobs:
            batched_jobs = int(stats.get("batched_jobs", 0))
            batched_files = int(stats.get("batched_files", 0))
            self.progress_widget.append_log(
                (
                    f"Queued upload file(s): {len(file_paths)} (jobs: {len(jobs)}"
                    + (
                        f", small batches: {batched_jobs} / files: {batched_files}"
                        if batched_jobs > 0
                        else ""
                    )
                    + ")"
                )
            )
        if skipped > 0:
            self.progress_widget.append_log(
                f"Skipped missing/unreadable selected files: {skipped}"
            )
        self._start_next_pending_upload()
