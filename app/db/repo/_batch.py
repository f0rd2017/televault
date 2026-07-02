"""DbRepo: батч-блобы и их участники (вынесено из repo.py)."""

from __future__ import annotations

from typing import Any

from app.core.types import (
    BatchBlobEntry,
    BatchMemberEntry,
)
from app.core.utils import now_ts, normalize_folder_path
from app.db.repo._sql import _UPSERT_BATCH_BLOB_SQL, _UPSERT_BATCH_MEMBER_SQL


class _BatchMixin:
    def upsert_batch_blob(
        self,
        *,
        blob_key: str,
        folder_path: str,
        chat_id: str,
        msg_id: int,
        blob_name: str,
        blob_size: int | None,
        blob_sha256: str | None,
        manifest_json: str,
        is_deleted: int = 0,
    ) -> None:
        created_ts = now_ts()
        normalized_folder = normalize_folder_path(folder_path)
        with self.conn:
            self.conn.execute(
                _UPSERT_BATCH_BLOB_SQL,
                (
                    str(blob_key),
                    normalized_folder,
                    str(chat_id),
                    int(msg_id),
                    str(blob_name),
                    int(blob_size) if blob_size is not None else None,
                    str(blob_sha256).lower() if blob_sha256 else None,
                    str(manifest_json),
                    int(is_deleted),
                    created_ts,
                    created_ts,
                ),
            )

    def upsert_batch_members_bulk(self, members: list[dict[str, Any]]) -> int:
        if not members:
            return 0
        now = now_ts()
        rows: list[tuple[Any, ...]] = []
        for member in members:
            raw_folder = str(member.get("folder_path") or "").strip()
            raw_key = str(member.get("file_key") or "").strip()
            raw_blob_key = str(member.get("blob_key") or "").strip()
            raw_name = str(member.get("orig_name") or "").strip()
            if not raw_folder or not raw_key or not raw_blob_key or not raw_name:
                continue
            rows.append(
                (
                    normalize_folder_path(raw_folder),
                    raw_key,
                    raw_blob_key,
                    raw_name,
                    int(member.get("member_index", 0)),
                    int(member["member_size"])
                    if member.get("member_size") is not None
                    else None,
                    str(member["member_sha256"]).lower()
                    if member.get("member_sha256")
                    else None,
                    int(member["deleted_ts"])
                    if member.get("deleted_ts") is not None
                    else None,
                    int(member.get("name_pinned", 0)),
                    int(member.get("created_ts", now)),
                    int(member.get("updated_ts", now)),
                )
            )
        if not rows:
            return 0
        with self.conn:
            self.conn.executemany(_UPSERT_BATCH_MEMBER_SQL, rows)
        return len(rows)

    def get_batch_blob(self, blob_key: str) -> BatchBlobEntry | None:
        row = self.conn.execute(
            """
            SELECT blob_key, folder_path, chat_id, msg_id, blob_name, blob_size, blob_sha256,
                   manifest_json, is_deleted, created_ts, last_seen_ts
            FROM batch_blobs
            WHERE blob_key = ?
            """,
            (str(blob_key),),
        ).fetchone()
        if row is None:
            return None
        return BatchBlobEntry(
            blob_key=str(row["blob_key"]),
            folder_path=str(row["folder_path"]),
            chat_id=str(row["chat_id"]),
            msg_id=int(row["msg_id"]),
            blob_name=str(row["blob_name"]),
            blob_size=int(row["blob_size"]) if row["blob_size"] is not None else None,
            blob_sha256=str(row["blob_sha256"])
            if row["blob_sha256"] is not None
            else None,
            manifest_json=str(row["manifest_json"]),
            is_deleted=int(row["is_deleted"]),
            created_ts=int(row["created_ts"]),
            last_seen_ts=int(row["last_seen_ts"]),
        )

    def get_batch_member(
        self, folder_path: str, file_key: str
    ) -> BatchMemberEntry | None:
        normalized_folder = normalize_folder_path(folder_path)
        row = self.conn.execute(
            """
            SELECT folder_path, file_key, blob_key, orig_name, member_index, member_size, member_sha256,
                   deleted_ts, name_pinned, created_ts, updated_ts
            FROM batch_members
            WHERE folder_path = ? AND file_key = ?
            """,
            (normalized_folder, str(file_key)),
        ).fetchone()
        if row is None:
            return None
        return BatchMemberEntry(
            folder_path=str(row["folder_path"]),
            file_key=str(row["file_key"]),
            blob_key=str(row["blob_key"]),
            orig_name=str(row["orig_name"]),
            member_index=int(row["member_index"]),
            member_size=int(row["member_size"])
            if row["member_size"] is not None
            else None,
            member_sha256=(
                str(row["member_sha256"]) if row["member_sha256"] is not None else None
            ),
            deleted_ts=int(row["deleted_ts"])
            if row["deleted_ts"] is not None
            else None,
            name_pinned=int(row["name_pinned"]),
            created_ts=int(row["created_ts"]),
            updated_ts=int(row["updated_ts"]),
        )

    def list_batch_members_by_blob(self, blob_key: str) -> list[BatchMemberEntry]:
        rows = self.conn.execute(
            """
            SELECT folder_path, file_key, blob_key, orig_name, member_index, member_size, member_sha256,
                   deleted_ts, name_pinned, created_ts, updated_ts
            FROM batch_members
            WHERE blob_key = ?
            ORDER BY member_index ASC, folder_path COLLATE NOCASE, file_key
            """,
            (str(blob_key),),
        ).fetchall()
        return [
            BatchMemberEntry(
                folder_path=str(row["folder_path"]),
                file_key=str(row["file_key"]),
                blob_key=str(row["blob_key"]),
                orig_name=str(row["orig_name"]),
                member_index=int(row["member_index"]),
                member_size=int(row["member_size"])
                if row["member_size"] is not None
                else None,
                member_sha256=(
                    str(row["member_sha256"])
                    if row["member_sha256"] is not None
                    else None
                ),
                deleted_ts=int(row["deleted_ts"])
                if row["deleted_ts"] is not None
                else None,
                name_pinned=int(row["name_pinned"]),
                created_ts=int(row["created_ts"]),
                updated_ts=int(row["updated_ts"]),
            )
            for row in rows
        ]

    def resolve_object_storage(self, folder_path: str, file_key: str) -> str:
        normalized_folder = normalize_folder_path(folder_path)
        batch_row = self.conn.execute(
            """
            SELECT 1
            FROM batch_members bm
            JOIN batch_blobs bb ON bb.blob_key = bm.blob_key
            WHERE bm.folder_path = ?
              AND bm.file_key = ?
              AND bm.deleted_ts IS NULL
              AND bb.is_deleted = 0
            LIMIT 1
            """,
            (normalized_folder, str(file_key)),
        ).fetchone()
        if batch_row is not None:
            return "batch_member"
        return "regular"

    def mark_batch_member_deleted(self, folder_path: str, file_key: str) -> int:
        normalized_folder = normalize_folder_path(folder_path)
        ts = now_ts()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE batch_members
                SET deleted_ts = COALESCE(deleted_ts, ?),
                    updated_ts = ?
                WHERE folder_path = ?
                  AND file_key = ?
                  AND deleted_ts IS NULL
                """,
                (ts, ts, normalized_folder, str(file_key)),
            )
        return max(0, int(cursor.rowcount))

    def supersede_batch_members_by_name(
        self, folder_path: str, orig_name: str, keep_file_key: str
    ) -> int:
        """Логически удалить старые версии мелкого файла: те же папка+имя, но
        другой ключ (другое содержимое или перекомпонованный батч). Оставляет
        текущую версию (keep_file_key). Так обновлённый мелкий файл вытесняет
        старую версию вместо появления дубля. Сообщение старого блоба не трогаем
        — в нём могут быть живые члены; осиротевший блоб подчистится при удалении
        папки/реконсиляции."""
        normalized_folder = normalize_folder_path(folder_path)
        ts = now_ts()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE batch_members
                SET deleted_ts = COALESCE(deleted_ts, ?),
                    updated_ts = ?
                WHERE folder_path = ?
                  AND orig_name = ?
                  AND file_key != ?
                  AND deleted_ts IS NULL
                """,
                (ts, ts, normalized_folder, str(orig_name), str(keep_file_key)),
            )
        return max(0, int(cursor.rowcount))

    def mark_batch_members_deleted_by_folder(self, folder_path: str) -> int:
        normalized_folder = normalize_folder_path(folder_path)
        ts = now_ts()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE batch_members
                SET deleted_ts = COALESCE(deleted_ts, ?),
                    updated_ts = ?
                WHERE deleted_ts IS NULL
                  AND (folder_path = ? OR folder_path LIKE ?)
                """,
                (ts, ts, normalized_folder, f"{normalized_folder}/%"),
            )
        return max(0, int(cursor.rowcount))

    def rename_batch_member(
        self, folder_path: str, file_key: str, new_name: str
    ) -> int:
        normalized_folder = normalize_folder_path(folder_path)
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE batch_members
                SET orig_name = ?,
                    name_pinned = 1,
                    updated_ts = ?
                WHERE folder_path = ?
                  AND file_key = ?
                  AND deleted_ts IS NULL
                """,
                (str(new_name), now_ts(), normalized_folder, str(file_key)),
            )
        return max(0, int(cursor.rowcount))

    def count_active_batch_members(self, blob_key: str) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(1) AS cnt
            FROM batch_members bm
            JOIN batch_blobs bb ON bb.blob_key = bm.blob_key
            WHERE bm.blob_key = ?
              AND bm.deleted_ts IS NULL
              AND bb.is_deleted = 0
            """,
            (str(blob_key),),
        ).fetchone()
        if row is None:
            return 0
        return int(row["cnt"])

    def mark_batch_blob_deleted(self, blob_key: str) -> int:
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE batch_blobs
                SET is_deleted = 1, last_seen_ts = ?
                WHERE blob_key = ?
                """,
                (now_ts(), str(blob_key)),
            )
        return max(0, int(cursor.rowcount))

