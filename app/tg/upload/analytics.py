"""Single builder of the ``analytics`` block for all upload branches.

Previously, a ~80-line dict was assembled separately in `_single_part_upload`,
`chunked_upload` (in-memory multipart), and `_multipart_upload_from_disk` —
three nearly identical copies that drifted apart over time (see the history
around the "dead" adaptive controller bug). Here the common part is built
once; each branch only passes in its measured phases and its own
``upload_profile`` (the most variable part is left to the caller).
"""

from __future__ import annotations

from app.core.utils import has_cryptg

_MB = 1024.0 * 1024.0


class _UploadAnalyticsMixin:
    def _build_upload_analytics(
        self,
        *,
        phase_seconds: dict[str, float],
        payload_total_bytes: int,
        source_total_bytes: int,
        source_original_bytes: int,
        transfer_elapsed: float,
        total_elapsed: float,
        compression_mode: str,
        compression_used: bool,
        compression_seconds: float,
        compression_ratio: float | None,
        safe_limit_bytes: int,
        flood_wait_count: int,
        flood_wait_seconds: float,
        adaptive_block: dict[str, object],
        upload_profile: dict[str, object],
        payload_by_channel: dict[str, int] | None = None,
    ) -> dict[str, object]:
        transfer_elapsed = max(0.001, float(transfer_elapsed))
        total_elapsed = max(0.001, float(total_elapsed))
        payload_total = int(payload_total_bytes)
        source_total = int(source_total_bytes)

        is_premium = bool(self.transfer_limits.is_premium)
        part_concurrency_cap = (
            self._PREMIUM_CONCURRENCY_CAP
            if is_premium
            else self._REGULAR_CONCURRENCY_CAP
        )

        bytes_block: dict[str, object] = {
            "source_total": source_total,
            "source_original": int(source_original_bytes),
            "payload_total": payload_total,
        }
        if payload_by_channel is not None:
            bytes_block["payload_by_channel"] = {
                str(chat_id): int(value)
                for chat_id, value in payload_by_channel.items()
            }

        return {
            "phase_seconds": dict(phase_seconds),
            "speed_mbps": {
                "transfer_payload": float(payload_total) / transfer_elapsed / _MB,
                "total_payload": float(payload_total) / total_elapsed / _MB,
                "total_source": float(source_total) / total_elapsed / _MB,
            },
            "bytes": bytes_block,
            "compression": {
                "mode": str(compression_mode).strip().lower(),
                "used": bool(compression_used),
                "algorithm": "zip_deflate_fast" if compression_used else None,
                "seconds": float(compression_seconds),
                "ratio": float(compression_ratio)
                if compression_ratio is not None
                else None,
            },
            "upload_profile": upload_profile,
            "performance": {
                "files_per_sec": float(1.0 / transfer_elapsed),
                "payload_mbps": float(payload_total) / transfer_elapsed / _MB,
                "requests_per_file": 1.0,
                "batch_hit_ratio": 0.0,
                "blob_reuse_ratio": 0.0,
            },
            "tg_limits": {
                "is_premium": is_premium,
                "request_size_bytes": int(self.transfer_limits.request_size_bytes),
                "max_fileparts": int(self.transfer_limits.max_fileparts),
                "max_file_size_bytes": int(self.transfer_limits.max_file_size_bytes),
                "safe_limit_bytes": int(safe_limit_bytes),
                "part_concurrency_cap": int(part_concurrency_cap),
                "flood_wait_count": int(flood_wait_count),
                "flood_wait_seconds": float(flood_wait_seconds),
                "adaptive": adaptive_block,
                "send_media_limiter": self._send_media_limiter.snapshot(),
                "cryptg": bool(has_cryptg()),
            },
        }
