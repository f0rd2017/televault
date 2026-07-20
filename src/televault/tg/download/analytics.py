"""Single builder of the ``analytics`` block for the download branches.

Mirrors `televault/tg/upload/analytics.py`: the common scaffolding (phase_seconds,
speed_mbps, bytes, performance, tg_limits) is built once, while
`chunked_download` and `_download_batch_member` only pass in their measured
phases, their own ``download_profile``, and the variable concurrency values.
"""

from __future__ import annotations

from televault.core.utils import has_cryptg

_MB = 1024.0 * 1024.0


class _DownloadAnalyticsMixin:
    def _build_download_analytics(
        self,
        *,
        phase_seconds: dict[str, float],
        output_total_bytes: int,
        resume_completed_bytes: int,
        transfer_elapsed: float,
        total_elapsed: float,
        download_profile: dict[str, object],
        requests_per_file: float,
        batch_hit_ratio: float,
        blob_reuse_ratio: float,
        effective_part_concurrency: int,
        effective_stride_streams: int,
        adaptive: dict[str, object],
        payload_by_channel: dict[str, int] | None = None,
        resume: dict[str, int] | None = None,
    ) -> dict[str, object]:
        transfer_elapsed = max(0.001, float(transfer_elapsed))
        total_elapsed = max(0.001, float(total_elapsed))
        output_total = int(output_total_bytes)

        bytes_block: dict[str, object] = {
            "output_total": output_total,
            "resume_completed": int(resume_completed_bytes),
        }
        if payload_by_channel is not None:
            bytes_block["payload_by_channel"] = {
                str(chat_id): int(value)
                for chat_id, value in payload_by_channel.items()
            }

        analytics: dict[str, object] = {
            "phase_seconds": dict(phase_seconds),
            "speed_mbps": {
                "transfer_output": float(output_total) / transfer_elapsed / _MB,
                "total_output": float(output_total) / total_elapsed / _MB,
            },
            "bytes": bytes_block,
        }
        if resume is not None:
            analytics["resume"] = {str(k): int(v) for k, v in resume.items()}
        analytics["download_profile"] = download_profile
        analytics["performance"] = {
            "files_per_sec": float(1.0 / transfer_elapsed),
            "payload_mbps": float(output_total) / transfer_elapsed / _MB,
            "requests_per_file": float(requests_per_file),
            "batch_hit_ratio": float(batch_hit_ratio),
            "blob_reuse_ratio": float(blob_reuse_ratio),
        }
        analytics["tg_limits"] = {
            "is_premium": bool(self.transfer_limits.is_premium),
            "request_size_bytes": int(self._tg_request_size),
            "part_concurrency_cap": int(self._download_part_concurrency_cap),
            "stride_streams": int(self._stride_streams),
            "total_stream_budget": int(self._download_total_stream_budget),
            "effective_part_concurrency": int(effective_part_concurrency),
            "effective_stride_streams": int(effective_stride_streams),
            "adaptive": adaptive,
            "get_file_limiter": self._get_file_limiter.snapshot(),
            "cryptg": bool(has_cryptg()),
        }
        return analytics
