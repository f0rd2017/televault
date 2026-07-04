"""DbRepo: фоновые задания jobs (вынесено из repo.py)."""

from __future__ import annotations

import json
from typing import Any

from app.core.utils import now_ts


class _JobsMixin:
    def insert_job(
        self, job_type: str, payload: dict[str, Any], status: str = "queued"
    ) -> int:
        now = now_ts()
        payload_raw = json.dumps(payload)
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO jobs(type, payload_json, status, progress, created_ts, updated_ts)
                VALUES (?, ?, ?, 0.0, ?, ?)
                """,
                (job_type, payload_raw, status, now, now),
            )
        return int(cursor.lastrowid)

    def update_job(
        self, job_id: int, status: str, progress: float, error_text: str | None = None
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE jobs
                SET status = ?, progress = ?, error_text = ?, updated_ts = ?
                WHERE id = ?
                """,
                (status, progress, error_text, now_ts(), job_id),
            )

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 1000))
        rows = self.conn.execute(
            """
            SELECT id, type, payload_json, status, progress, created_ts, updated_ts, error_text
            FROM jobs
            ORDER BY id DESC
            LIMIT ?
            """,
            (bounded_limit,),
        ).fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            payload_json = row["payload_json"] or "{}"
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                payload = {"raw": payload_json}
            result.append(
                {
                    "id": int(row["id"]),
                    "type": row["type"],
                    "payload": payload,
                    "status": row["status"],
                    "progress": float(row["progress"]),
                    "created_ts": int(row["created_ts"]),
                    "updated_ts": int(row["updated_ts"]),
                    "error_text": row["error_text"],
                }
            )
        return result

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        """Одна джоба по id (для REST API). None, если не найдена."""
        row = self.conn.execute(
            """
            SELECT id, type, payload_json, status, progress, created_ts, updated_ts, error_text
            FROM jobs
            WHERE id = ?
            """,
            (int(job_id),),
        ).fetchone()
        if row is None:
            return None
        payload_json = row["payload_json"] or "{}"
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            payload = {"raw": payload_json}
        return {
            "id": int(row["id"]),
            "type": row["type"],
            "payload": payload,
            "status": row["status"],
            "progress": float(row["progress"]),
            "created_ts": int(row["created_ts"]),
            "updated_ts": int(row["updated_ts"]),
            "error_text": row["error_text"],
        }

    def list_jobs_by_status(
        self, status: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        """List jobs filtered by status, ordered by id DESC."""
        bounded_limit = max(1, min(int(limit), 5000))
        rows = self.conn.execute(
            """
            SELECT id, type, payload_json, status, progress, created_ts, updated_ts, error_text
            FROM jobs
            WHERE status = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (status, bounded_limit),
        ).fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "id": int(row["id"]),
                    "type": row["type"],
                    "payload_json": row["payload_json"],
                    "status": row["status"],
                    "progress": float(row["progress"]),
                    "created_ts": int(row["created_ts"]),
                    "updated_ts": int(row["updated_ts"]),
                    "error_text": row["error_text"],
                }
            )
        return result
