"""Job events mixin: job event handling, enqueue, retry, batch tracking, toasts."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QSystemTrayIcon

from app.core.types import JobEvent, JobStatus, JobType
from app.core.utils import to_human_size
from app.ui.job_toasts import JobToastCard


class JobEventsMixin:
    """Methods for job event handling, enqueue, retry, and batch tracking."""

    def _enqueue_job(self, job_type: str, payload: dict) -> None:
        request_id = uuid.uuid4().hex
        channel_scope = (
            "multi"
            if (
                self._count_account_channels() > 1
                and str(self.config.channel_sharding_mode).strip().lower()
                == "part_striping"
            )
            else "single"
        )
        enriched_payload = {
            **payload,
            "_ui_request_id": request_id,
            "_ui_channel_scope": channel_scope,
        }
        self._inflight_requests.add(request_id)
        self._inflight_request_meta[request_id] = {
            "job_type": str(job_type),
            "small_upload": bool(enriched_payload.get("_ui_small_upload")),
        }
        self._set_transfer_state_from_job_payload(
            job_type, enriched_payload, "downloading"
        )

        batch_id = str(enriched_payload.get("_ui_batch_id") or "").strip() or None
        suppress_individual_toast = self._should_suppress_individual_toast(batch_id)

        toast: JobToastCard | None = None
        if not suppress_individual_toast:
            toast_title = self._toast_title_from_payload(job_type, enriched_payload)
            toast = self._toast_overlay.add_toast(
                toast_title, cancel_cb=self._on_cancel_job
            )
            self._toast_by_request_id[request_id] = toast
            toast.update_event(
                JobEvent(
                    job_id=-1,
                    job_type=job_type,
                    status=JobStatus.QUEUED,
                    progress=0.0,
                    message="Queued",
                    payload=enriched_payload,
                )
            )
        self.progress_widget.append_log(f"Queued: {job_type}")
        self.progress_widget.set_progress(int(self._calc_global_progress()))
        self.progress_widget.set_status_text(
            f"{self._activity_for_job(job_type)} queued"
        )
        self._sync_busy_state(self._activity_for_job(job_type))
        if self._local_presence_timer.isActive():
            self._local_presence_timer.stop()
        submitted = bool(self.worker.submit_job(job_type, enriched_payload))
        if submitted:
            self._pending_enqueue_retries.pop(request_id, None)
            return

        if self._should_retry_enqueue_when_worker_unavailable(job_type):
            self._pending_enqueue_retries[request_id] = {
                "job_type": str(job_type),
                "payload": dict(enriched_payload),
                "batch_id": batch_id,
                "attempts": 0,
                "toast": toast,
            }
            self.progress_widget.append_log(
                f"Worker is reconnecting; retrying queued '{job_type}' automatically"
            )
            self.progress_widget.set_status_text("Waiting for Telegram reconnect...")
            if toast is not None:
                toast.update_event(
                    JobEvent(
                        job_id=-1,
                        job_type=job_type,
                        status=JobStatus.QUEUED,
                        progress=0.0,
                        message="Queued (waiting for reconnect)",
                        payload=enriched_payload,
                    )
                )
            if not self._enqueue_retry_timer.isActive():
                self._enqueue_retry_timer.start()
            self._sync_busy_state("Waiting for reconnect")
            return

        self._fail_enqueue_request(
            request_id=request_id,
            job_type=job_type,
            payload=enriched_payload,
            batch_id=batch_id,
            error_message="Worker is not ready",
        )

    def _should_retry_enqueue_when_worker_unavailable(self, job_type: str) -> bool:
        return job_type in {JobType.DELETE.value, JobType.DELETE_FOLDER.value}

    def _fail_enqueue_request(
        self,
        *,
        request_id: str,
        job_type: str,
        payload: dict[str, Any],
        batch_id: str | None,
        error_message: str,
    ) -> None:
        toast = self._toast_by_request_id.pop(request_id, None)
        self._pending_enqueue_retries.pop(request_id, None)
        self._inflight_requests.discard(request_id)
        self._inflight_request_meta.pop(request_id, None)
        self._set_transfer_state_from_job_payload(job_type, payload, None)
        if self._toast_overlay.is_card_alive(toast):
            toast.update_event(
                JobEvent(
                    job_id=-1,
                    job_type=job_type,
                    status=JobStatus.ERROR,
                    progress=0.0,
                    error=error_message,
                    payload=payload,
                )
            )
        if batch_id:
            self._mark_batch_enqueue_failure(batch_id, error_message)
        self.progress_widget.append_log(
            f"Failed to queue '{job_type}': {error_message.lower()}"
        )
        self.progress_widget.set_progress(int(self._calc_global_progress()))
        self._sync_busy_state()
        if (
            not self._inflight_requests
            and not self._active_jobs
            and not self._pending_upload_jobs
            and not self._pending_enqueue_retries
            and not self._local_presence_timer.isActive()
        ):
            self._reset_global_transfer_window()
            self._local_presence_timer.start()

    def _process_pending_enqueue_retries(self, force: bool = False) -> None:
        _ = force
        if not self._pending_enqueue_retries:
            if self._enqueue_retry_timer.isActive():
                self._enqueue_retry_timer.stop()
            return

        max_attempts = max(1, int(self._ENQUEUE_RETRY_MAX_ATTEMPTS))
        request_ids = list(self._pending_enqueue_retries.keys())
        for request_id in request_ids:
            record = self._pending_enqueue_retries.get(request_id)
            if not isinstance(record, dict):
                self._pending_enqueue_retries.pop(request_id, None)
                continue

            job_type = str(record.get("job_type") or "")
            payload = dict(record.get("payload") or {})
            batch_id = str(record.get("batch_id") or "").strip() or None
            attempts = int(record.get("attempts", 0))
            if attempts >= max_attempts:
                self._fail_enqueue_request(
                    request_id=request_id,
                    job_type=job_type,
                    payload=payload,
                    batch_id=batch_id,
                    error_message="Worker is not ready (retry timeout)",
                )
                continue

            submitted = bool(self.worker.submit_job(job_type, payload))
            if submitted:
                self._pending_enqueue_retries.pop(request_id, None)
                self.progress_widget.append_log(
                    f"Queued '{job_type}' after reconnect (retry {attempts}/{max_attempts})"
                )
                continue

            attempts += 1
            record["attempts"] = attempts
            self._pending_enqueue_retries[request_id] = record
            if attempts in {1, 5, 10, 20}:
                self.progress_widget.append_log(
                    (
                        f"Waiting for worker: '{job_type}' retry {attempts}/{max_attempts}"
                    )
                )

        if not self._pending_enqueue_retries and self._enqueue_retry_timer.isActive():
            self._enqueue_retry_timer.stop()

    def _on_cancel_job(self, job_id: int | None = None) -> None:
        self._pending_upload_jobs.clear()
        if job_id is not None:
            self.worker.cancel_job(int(job_id))
            self.progress_widget.append_log(f"Cancel requested for job #{job_id}")
            return

        if self._pending_enqueue_retries:
            pending_ids = list(self._pending_enqueue_retries.keys())
            for request_id in pending_ids:
                record = self._pending_enqueue_retries.get(request_id) or {}
                job_type = str(record.get("job_type") or "job")
                payload = dict(record.get("payload") or {})
                batch_id = str(record.get("batch_id") or "").strip() or None
                self._fail_enqueue_request(
                    request_id=request_id,
                    job_type=job_type,
                    payload=payload,
                    batch_id=batch_id,
                    error_message="Cancelled before enqueue",
                )
            self._pending_enqueue_retries.clear()
            if self._enqueue_retry_timer.isActive():
                self._enqueue_retry_timer.stop()

        for active_job_id in list(self._active_jobs):
            self.worker.cancel_job(active_job_id)
            self.progress_widget.append_log(
                f"Cancel requested for job #{active_job_id}"
            )

    def _on_job_event(self, event: JobEvent) -> None:
        payload = event.payload or {}
        request_id = str(payload.get("_ui_request_id") or "").strip()
        batch_id = str(payload.get("_ui_batch_id") or "").strip() or None
        suppress_individual_toast = self._should_suppress_individual_toast(batch_id)

        if event.job_id >= 0:
            self._job_last_update_ts[event.job_id] = time.monotonic()
            self._stale_notified_jobs.discard(event.job_id)
            if event.status == JobStatus.RUNNING:
                self._running_jobs.add(event.job_id)

        if not suppress_individual_toast:
            toast = self._toast_by_job_id.get(event.job_id)
            if not self._toast_overlay.is_card_alive(toast):
                # The card is missing or was evicted by the visible-notification
                # limit — try to recover it by request_id, otherwise create a
                # new one so the active process is visible again.
                self._toast_by_job_id.pop(event.job_id, None)
                toast = None
                if request_id and request_id in self._toast_by_request_id:
                    candidate = self._toast_by_request_id.pop(request_id)
                    if self._toast_overlay.is_card_alive(candidate):
                        toast = candidate
                if toast is None:
                    toast_title = self._toast_title_from_payload(
                        event.job_type, payload
                    )
                    toast = self._toast_overlay.add_toast(
                        toast_title, cancel_cb=self._on_cancel_job
                    )
                toast.job_id = event.job_id
                toast.set_cancel_callback(self._on_cancel_job)
                self._toast_by_job_id[event.job_id] = toast
            toast.update_event(event)
        elif request_id:
            self._toast_by_request_id.pop(request_id, None)

        if batch_id:
            self._update_batch_tracking(batch_id, event)

        if event.status == JobStatus.RUNNING:
            self._job_type_by_id[event.job_id] = str(event.job_type)
            self._pending_running_events[event.job_id] = event
            if not self._running_event_flush_timer.isActive():
                self._running_event_flush_timer.start()
            return

        self._flush_pending_running_event(force=True)

        if event.status in {JobStatus.QUEUED, JobStatus.STARTED}:
            self._job_type_by_id[event.job_id] = str(event.job_type)
            self._active_jobs.add(event.job_id)
            self._job_progress.setdefault(event.job_id, float(event.progress))
            self._job_progress_weight.setdefault(
                event.job_id,
                self._progress_weight_from_payload(event.job_type, payload),
            )
            self.progress_widget.set_progress(int(self._calc_global_progress()))
            global_status = self._build_global_progress_status()
            self.progress_widget.set_status_text(
                global_status
                or event.message
                or f"{self._activity_for_job(event.job_type)} starting"
            )
            self._sync_busy_state(self._activity_for_job(event.job_type))
            if event.message and self._should_log_event_message(event):
                self.progress_widget.append_log(event.message)
            return

        if event.status in {JobStatus.DONE, JobStatus.CANCELLED, JobStatus.ERROR}:
            # The initial reconciliation finished (success or failure) — hide
            # the startup loading screen: data has been loaded/reconciled.
            if payload.get("_ui_initial_load") and hasattr(
                self, "_finish_startup_overlay"
            ):
                self._finish_startup_overlay()
            self._accumulate_finished_transfer_job(event, payload)
            self._active_jobs.discard(event.job_id)
            self._job_progress.pop(event.job_id, None)
            self._job_progress_weight.pop(event.job_id, None)
            self._job_type_by_id.pop(event.job_id, None)
            self._pending_running_events.pop(event.job_id, None)
            self._running_jobs.discard(event.job_id)
            self._toast_by_job_id.pop(event.job_id, None)
            self._set_transfer_state_from_job_payload(event.job_type, payload, None)
            self._clear_job_tracking(event.job_id)

            if request_id:
                self._inflight_requests.discard(request_id)
                self._inflight_request_meta.pop(request_id, None)
                self._toast_by_request_id.pop(request_id, None)

            still_busy = bool(
                self._inflight_requests
                or self._active_jobs
                or self._pending_upload_jobs
                or self._pending_enqueue_retries
            )

            if event.status == JobStatus.DONE:
                self.progress_widget.append_log(f"Job #{event.job_id} done")
                if not still_busy:
                    self.progress_widget.set_status_text(
                        f"{self._activity_for_job(event.job_type)} done"
                    )
                if event.result is not None:
                    self._append_job_result(event.job_type, event.result)
                if payload.get("for_export"):
                    self.progress_widget.append_log(
                        "File downloaded to cache. Drag it again to export to PC."
                    )
                if (
                    event.job_type == JobType.DOWNLOAD.value
                    and self.config.cache_max_size_mb > 0
                ):
                    repo = self.repo
                    cache_dir = self.config.cache_dir
                    cache_max_bytes = self.config.cache_max_size_mb * 1024 * 1024
                    threading.Thread(
                        target=self._run_cache_cleanup,
                        args=(repo, cache_dir, cache_max_bytes),
                        daemon=True,
                    ).start()
                # Notify once per finished operation, not per file/job: fire only
                # when all work has drained (folder downloads, sync and batch
                # uploads enqueue many jobs — this avoids per-file spam).
                if not still_busy:
                    self._tray.showMessage(
                        "TG Cloud",
                        self.tr("Operation completed"),
                        QSystemTrayIcon.MessageIcon.Information,
                        3000,
                    )
            elif event.status == JobStatus.CANCELLED:
                self.progress_widget.append_log(f"Job #{event.job_id} cancelled")
                if not still_busy:
                    self.progress_widget.set_status_text(
                        f"{self._activity_for_job(event.job_type)} cancelled"
                    )
            elif event.status == JobStatus.ERROR:
                self.progress_widget.append_log(
                    f"Job #{event.job_id} failed: {event.error}"
                )
                if not still_busy:
                    self.progress_widget.set_status_text(
                        f"{self._activity_for_job(event.job_type)} failed"
                    )
                # Like the success case: surface one notification when the whole
                # operation settles, not one per failed file (the grouped error
                # dialog still lists every failure).
                if not still_busy:
                    self._tray.showMessage(
                        "TG Cloud",
                        self.tr("Operation completed with errors"),
                        QSystemTrayIcon.MessageIcon.Warning,
                        3000,
                    )
                self._queue_error_dialog(
                    event.job_id,
                    event.job_type,
                    event.error or "Unknown error",
                )

            self.progress_widget.set_progress(int(self._calc_global_progress()))
            if still_busy:
                global_status = self._build_global_progress_status()
                if global_status:
                    self.progress_widget.set_status_text(global_status)
            self._sync_busy_state(
                "Working" if still_busy else self._activity_for_job(event.job_type)
            )
            self._schedule_reload_all()
            self._start_next_pending_upload()

            if (
                not self._inflight_requests
                and not self._active_jobs
                and not self._pending_upload_jobs
                and not self._pending_enqueue_retries
            ):
                self._reset_global_transfer_window()
                self.progress_widget.set_progress(0)
                self.progress_widget.set_status_text(None)
                if not self._local_presence_timer.isActive():
                    self._local_presence_timer.start()
            return

    def _append_job_result(self, job_type: str, result: Any) -> None:
        if not isinstance(result, dict):
            self.progress_widget.append_log(str(result))
            return

        analytics = result.get("analytics")
        if isinstance(analytics, dict):
            self._append_transfer_analytics(job_type, analytics)

        summary = {key: value for key, value in result.items() if key != "analytics"}
        if summary:
            self.progress_widget.append_log(str(summary))

    def _append_transfer_analytics(
        self, job_type: str, analytics: dict[str, Any]
    ) -> None:
        phase = (
            analytics.get("phase_seconds", {}) if isinstance(analytics, dict) else {}
        )
        speed = analytics.get("speed_mbps", {}) if isinstance(analytics, dict) else {}
        bytes_info = analytics.get("bytes", {}) if isinstance(analytics, dict) else {}

        def sec(name: str) -> float:
            return self._as_float(phase.get(name))

        net_sec = sec("network_send") or sec("network_download")
        disk_sec = sec("read") + sec("merge") + sec("decrypt")
        hash_sec = sec("prehash") + sec("integrity_check")
        db_sec = sec("db_upsert") + sec("db_rebuild") + sec("manifest_write")

        total_sec = sec("total")
        transfer_sec = sec("transfer")
        self.progress_widget.append_log(
            (
                f"Analytics [{job_type}]: total {total_sec:.2f}s | transfer {transfer_sec:.2f}s "
                f"| net {net_sec:.2f}s | disk {disk_sec:.2f}s | hash {hash_sec:.2f}s | db/io {db_sec:.2f}s"
            )
        )

        transfer_speed = (
            self._as_float(speed.get("transfer_payload"))
            or self._as_float(speed.get("transfer_output"))
            or self._as_float(speed.get("total_payload"))
        )
        total_speed = (
            self._as_float(speed.get("total_payload"))
            or self._as_float(speed.get("total_output"))
            or self._as_float(speed.get("total_source"))
            or transfer_speed
        )
        self.progress_widget.append_log(
            f"Speed [{job_type}]: transfer {transfer_speed:.2f} MB/s | end-to-end {total_speed:.2f} MB/s"
        )

        if isinstance(bytes_info, dict) and bytes_info:
            source_total = bytes_info.get("source_total")
            payload_total = bytes_info.get("payload_total")
            output_total = bytes_info.get("output_total")
            resume_total = bytes_info.get("resume_completed")
            if source_total is not None:
                self.progress_widget.append_log(
                    (
                        f"Bytes [{job_type}]: source {to_human_size(int(source_total))}"
                        + (
                            f" | payload {to_human_size(int(payload_total))}"
                            if payload_total is not None
                            else ""
                        )
                    )
                )
            elif output_total is not None:
                self.progress_widget.append_log(
                    (
                        f"Bytes [{job_type}]: output {to_human_size(int(output_total))}"
                        + (
                            f" | resumed {to_human_size(int(resume_total))}"
                            if resume_total is not None
                            else ""
                        )
                    )
                )

        performance = (
            analytics.get("performance") if isinstance(analytics, dict) else None
        )
        if isinstance(performance, dict):
            files_per_sec = self._as_float(performance.get("files_per_sec"))
            payload_mbps = self._as_float(performance.get("payload_mbps"))
            requests_per_file = self._as_float(performance.get("requests_per_file"))
            batch_hit_ratio = self._as_float(performance.get("batch_hit_ratio"))
            blob_reuse_ratio = self._as_float(performance.get("blob_reuse_ratio"))
            self.progress_widget.append_log(
                (
                    f"Perf [{job_type}]: files/s {files_per_sec:.2f} | payload {payload_mbps:.2f} MB/s "
                    f"| req/file {requests_per_file:.2f} | batch_hit {batch_hit_ratio:.2f} "
                    f"| blob_reuse {blob_reuse_ratio:.2f}"
                )
            )

        tg_limits = analytics.get("tg_limits") if isinstance(analytics, dict) else None
        if isinstance(tg_limits, dict):
            request_size = tg_limits.get("request_size_bytes")
            max_file_size = tg_limits.get("max_file_size_bytes")
            max_fileparts = tg_limits.get("max_fileparts")
            tier = "premium" if bool(tg_limits.get("is_premium")) else "regular"

            details = [f"tier {tier}"]
            if request_size is not None:
                details.append(f"req {to_human_size(int(request_size))}")
            if max_file_size is not None:
                details.append(f"file limit {to_human_size(int(max_file_size))}")
            if max_fileparts is not None:
                details.append(f"max parts {int(max_fileparts)}")

            stride_streams = tg_limits.get("stride_streams")
            part_concurrency_cap = tg_limits.get("part_concurrency_cap")
            cryptg_enabled = tg_limits.get("cryptg")
            if stride_streams is not None:
                details.append(f"stride {int(stride_streams)}")
            if part_concurrency_cap is not None:
                details.append(f"part cap {int(part_concurrency_cap)}")
            if cryptg_enabled is not None:
                details.append("cryptg on" if bool(cryptg_enabled) else "cryptg off")

            adaptive = tg_limits.get("adaptive")
            if isinstance(adaptive, dict):
                init_part = adaptive.get("initial_part_concurrency")
                final_part = adaptive.get("final_part_concurrency")
                init_stride = adaptive.get("initial_stride_streams")
                eff_stride = adaptive.get("effective_stride_streams")
                flood_count = adaptive.get("flood_wait_count")
                if init_part is not None and final_part is not None:
                    details.append(f"adaptive part {int(init_part)}->{int(final_part)}")
                if init_stride is not None and eff_stride is not None:
                    details.append(
                        f"adaptive stride {int(init_stride)}->{int(eff_stride)}"
                    )
                if flood_count is not None:
                    details.append(f"floodwait {int(flood_count)}")
            send_limiter = tg_limits.get("send_media_limiter")
            if isinstance(send_limiter, dict):
                rate = self._as_float(send_limiter.get("rate"))
                flood = int(self._as_float(send_limiter.get("flood_wait_count")))
                details.append(f"send-limiter {rate:.2f}/s fw={flood}")
            get_limiter = tg_limits.get("get_file_limiter")
            if isinstance(get_limiter, dict):
                rate = self._as_float(get_limiter.get("rate"))
                flood = int(self._as_float(get_limiter.get("flood_wait_count")))
                details.append(f"get-limiter {rate:.2f}/s fw={flood}")

            self.progress_widget.append_log(
                f"TG limits [{job_type}]: " + " | ".join(details)
            )

    @staticmethod
    def _as_float(value: Any) -> float:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 0.0

    def _should_log_event_message(self, event: JobEvent) -> bool:
        message = (event.message or "").strip()
        if not message:
            return False

        lower = message.lower()
        if lower.startswith("uploading ") or lower.startswith("downloading "):
            bucket = int(max(0.0, min(100.0, event.progress)) // 5)
            prev_bucket = self._job_log_bucket.get(event.job_id)
            if prev_bucket == bucket:
                return False
            self._job_log_bucket[event.job_id] = bucket
        return True

    def _clear_job_tracking(self, job_id: int) -> None:
        self._job_log_bucket.pop(job_id, None)
        self._pending_running_events.pop(job_id, None)
        self._job_progress_weight.pop(job_id, None)
        self._job_type_by_id.pop(job_id, None)
        self._job_last_update_ts.pop(job_id, None)
        self._stale_notified_jobs.discard(job_id)
        self._running_jobs.discard(job_id)

    def _count_account_channels(self) -> int:
        """Count distinct active account chat_targets."""
        try:
            accounts = self.repo.list_accounts()
            return len(
                [
                    a.chat_target
                    for a in accounts
                    if a.is_active and a.chat_target.strip()
                ]
            )
        except Exception:
            return 0

    def _progress_weight_from_payload(
        self, job_type: str, payload: dict[str, Any] | None
    ) -> float:
        if not isinstance(payload, dict):
            return 1.0
        if job_type not in {JobType.UPLOAD.value, JobType.DOWNLOAD.value}:
            return 1.0
        raw = payload.get("_ui_total_bytes")
        try:
            total_bytes = int(raw)
        except (TypeError, ValueError):
            total_bytes = 0
        if total_bytes <= 0:
            return 1.0
        return float(total_bytes)

    def _flush_pending_running_event(self, force: bool = False) -> None:
        if not self._pending_running_events:
            if force:
                self._running_event_flush_timer.stop()
            return

        pending = list(self._pending_running_events.values())
        self._pending_running_events.clear()
        last_message = ""
        for event in pending:
            self._active_jobs.add(event.job_id)
            self._job_type_by_id[event.job_id] = str(event.job_type)
            self._job_progress[event.job_id] = float(event.progress)
            self._job_progress_weight.setdefault(
                event.job_id,
                self._progress_weight_from_payload(event.job_type, event.payload or {}),
            )
            last_message = str(event.message or "").strip()
            if event.message and self._should_log_event_message(event):
                self.progress_widget.append_log(event.message)

        self.progress_widget.set_progress(int(self._calc_global_progress()))
        global_status = self._build_global_progress_status()
        self.progress_widget.set_status_text(global_status or last_message or "Working")
        self._sync_busy_state("Working")
        self._running_event_flush_timer.stop()

    def _start_batch_tracking(self, job_type: str, expected_count: int) -> str:
        expected = max(1, int(expected_count))
        batch_id = uuid.uuid4().hex
        activity = self._activity_for_job(job_type)
        show_batch_toast = not self._is_transfer_job_type(job_type)
        if show_batch_toast:
            toast = self._toast_overlay.add_toast(
                f"{activity} batch", cancel_cb=self._on_cancel_job
            )
            toast.set_cancel_callback(self._on_cancel_job)
            if hasattr(toast, "set_global_cancel_fallback"):
                toast.set_global_cancel_fallback(True)
            self._batch_toast_by_id[batch_id] = toast
        self._batch_state_by_id[batch_id] = {
            "job_type": job_type,
            "activity": activity,
            "show_toast": bool(show_batch_toast),
            "expected": expected,
            "done": 0,
            "error": 0,
            "cancelled": 0,
            "finished_jobs": set(),
            "progress_by_job": {},
        }
        self._render_batch_toast(batch_id, status=JobStatus.QUEUED)
        return batch_id

    def _should_suppress_individual_toast(self, batch_id: str | None) -> bool:
        if not batch_id:
            return False
        state = self._batch_state_by_id.get(batch_id)
        if not isinstance(state, dict):
            return False
        if self._is_transfer_job_type(str(state.get("job_type") or "")):
            return False
        return int(state.get("expected", 0)) > 1

    def _update_batch_tracking(self, batch_id: str, event: JobEvent) -> None:
        state = self._batch_state_by_id.get(batch_id)
        if not isinstance(state, dict):
            return

        job_id = int(event.job_id)
        progress_by_job: dict[int, float] = state["progress_by_job"]
        finished_jobs: set[int] = state["finished_jobs"]

        if event.status in {JobStatus.QUEUED, JobStatus.STARTED, JobStatus.RUNNING}:
            if job_id >= 0:
                progress_by_job[job_id] = max(0.0, min(100.0, float(event.progress)))
            self._render_batch_toast(batch_id, status=JobStatus.RUNNING)
            return

        if event.status in {JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED}:
            if job_id >= 0 and job_id in finished_jobs:
                return
            if job_id >= 0:
                finished_jobs.add(job_id)
                progress_by_job.pop(job_id, None)
            if event.status == JobStatus.DONE:
                state["done"] = int(state["done"]) + 1
            elif event.status == JobStatus.ERROR:
                state["error"] = int(state["error"]) + 1
            else:
                state["cancelled"] = int(state["cancelled"]) + 1
            self._render_batch_toast(batch_id)

    def _mark_batch_enqueue_failure(self, batch_id: str, error_message: str) -> None:
        state = self._batch_state_by_id.get(batch_id)
        if not isinstance(state, dict):
            return
        state["error"] = int(state["error"]) + 1
        self._render_batch_toast(batch_id)
        self.progress_widget.append_log(
            f"Batch enqueue failure [{state['job_type']}]: {error_message}"
        )

    def _render_batch_toast(
        self, batch_id: str, status: JobStatus | None = None
    ) -> None:
        state = self._batch_state_by_id.get(batch_id)
        toast = self._batch_toast_by_id.get(batch_id)
        if not isinstance(state, dict):
            return

        expected = max(1, int(state["expected"]))
        done = int(state["done"])
        error = int(state["error"])
        cancelled = int(state["cancelled"])
        finished = done + error + cancelled
        progress_by_job: dict[int, float] = state["progress_by_job"]
        running_progress = sum(
            max(0.0, min(100.0, p)) for p in progress_by_job.values()
        )
        pct = max(
            0.0, min(100.0, ((finished * 100.0) + running_progress) / float(expected))
        )

        terminal_status: JobStatus | None = None
        if finished >= expected:
            if error > 0:
                terminal_status = JobStatus.ERROR
            elif cancelled > 0 and done == 0:
                terminal_status = JobStatus.CANCELLED
            else:
                terminal_status = JobStatus.DONE

        effective_status = terminal_status or status or JobStatus.RUNNING
        message = (
            f"{state['activity']} {finished}/{expected} | "
            f"ok {done} err {error} cancel {cancelled}"
        )
        show_toast = bool(state.get("show_toast", True))
        if show_toast and self._toast_overlay.is_card_alive(toast):
            err_text = (
                f"{error} item(s) failed"
                if terminal_status == JobStatus.ERROR
                else None
            )
            toast.update_event(
                JobEvent(
                    job_id=-1,
                    job_type=str(state["job_type"]),
                    status=effective_status,
                    progress=float(pct),
                    message=message,
                    error=err_text,
                    payload={"_ui_batch_id": batch_id},
                )
            )

        if terminal_status is not None:
            self._batch_state_by_id.pop(batch_id, None)
            if toast is not None:
                self._batch_toast_by_id.pop(batch_id, None)

    def _toast_title_from_payload(
        self, job_type: str, payload: dict[str, Any] | None
    ) -> str:
        normalized_type = str(job_type or "").strip().lower()
        if isinstance(payload, dict):
            if normalized_type == JobType.UPLOAD.value:
                file_path = str(payload.get("file_path") or "").strip()
                if file_path:
                    return Path(file_path).name or "Upload"
                paths = payload.get("file_paths")
                if isinstance(paths, list):
                    names = [Path(str(p)).name for p in paths if str(p or "").strip()]
                    if len(names) == 1:
                        return names[0]
                    if len(names) > 1:
                        return f"{names[0]} +{len(names) - 1}"
            elif normalized_type == JobType.DOWNLOAD.value:
                orig_name = str(payload.get("orig_name") or "").strip()
                if orig_name:
                    return orig_name
                file_key = str(payload.get("file_key") or "").strip()
                if file_key:
                    return file_key
            elif normalized_type == JobType.DELETE.value:
                orig_name = str(payload.get("orig_name") or "").strip()
                if orig_name:
                    return orig_name
                file_key = str(payload.get("file_key") or "").strip()
                if file_key:
                    return file_key
        return self._activity_for_job(job_type)

    def _check_stalled_jobs(self) -> None:
        if not self._running_jobs:
            return
        now = time.monotonic()
        stale_jobs: list[int] = []
        for job_id in sorted(self._running_jobs):
            last_update = self._job_last_update_ts.get(job_id)
            if last_update is None:
                continue
            if (now - last_update) < self._STALE_JOB_SECONDS:
                continue
            if job_id in self._stale_notified_jobs:
                continue
            self._stale_notified_jobs.add(job_id)
            stale_jobs.append(job_id)

        if not stale_jobs:
            return
        ids = ", ".join(f"#{job_id}" for job_id in stale_jobs[:6])
        if len(stale_jobs) > 6:
            ids += f", +{len(stale_jobs) - 6}"
        self.progress_widget.append_log(
            f"Watchdog: possible stalled job(s) {ids}. No progress updates for >{int(self._STALE_JOB_SECONDS)}s."
        )
        self.statusBar().showMessage(
            self.tr("Some tasks appear stalled; try cancelling and starting again")
        )
