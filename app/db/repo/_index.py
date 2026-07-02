"""DbRepo: состояние сканов, папки, индекс сообщений (вынесено из repo.py)."""

from __future__ import annotations


from app.core.types import (
    FolderEntry,
    PartRecord,
)
from app.core.utils import now_ts, normalize_folder_path
from app.db.repo._sql import _UPSERT_MSG_PART_SQL


class _IndexMixin:
    def get_state(self, chat_id: str) -> dict[str, int | None]:
        row = self.conn.execute(
            "SELECT last_max_msg_id, last_scan_ts FROM state WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        if row is None:
            return {"last_max_msg_id": 0, "last_scan_ts": None}
        return {
            "last_max_msg_id": int(row["last_max_msg_id"]),
            "last_scan_ts": int(row["last_scan_ts"])
            if row["last_scan_ts"] is not None
            else None,
        }

    def update_state_last_max_id(
        self, chat_id: str, msg_id: int, ts: int | None = None
    ) -> None:
        scan_ts = ts if ts is not None else now_ts()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO state(chat_id, last_max_msg_id, last_scan_ts)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  last_max_msg_id = MAX(excluded.last_max_msg_id, state.last_max_msg_id),
                  last_scan_ts = excluded.last_scan_ts
                """,
                (chat_id, msg_id, scan_ts),
            )

    def reset_state(self, chat_id: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO state(chat_id, last_max_msg_id, last_scan_ts)
                VALUES (?, 0, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  last_max_msg_id = 0,
                  last_scan_ts = excluded.last_scan_ts
                """,
                (chat_id, now_ts()),
            )

    def upsert_folder(self, folder_path: str, pinned: int | None = None) -> str:
        normalized = normalize_folder_path(folder_path)
        with self.conn:
            if pinned is None:
                self.conn.execute(
                    """
                    INSERT INTO folders(folder_path, created_ts, pinned)
                    VALUES (?, ?, 0)
                    ON CONFLICT(folder_path) DO NOTHING
                    """,
                    (normalized, now_ts()),
                )
            else:
                self.conn.execute(
                    """
                    INSERT INTO folders(folder_path, created_ts, pinned)
                    VALUES (?, ?, ?)
                    ON CONFLICT(folder_path) DO UPDATE SET
                      pinned = excluded.pinned
                    """,
                    (normalized, now_ts(), int(pinned)),
                )
        return normalized

    def list_folders(self) -> list[FolderEntry]:
        rows = self.conn.execute(
            "SELECT folder_path, created_ts, pinned FROM folders ORDER BY pinned DESC, folder_path COLLATE NOCASE"
        ).fetchall()
        return [
            FolderEntry(
                folder_path=row["folder_path"],
                created_ts=int(row["created_ts"]),
                pinned=int(row["pinned"]),
            )
            for row in rows
        ]

    def upsert_folders_bulk(self, folder_paths: list[str]) -> int:
        if not folder_paths:
            return 0
        normalized_paths = sorted(
            {normalize_folder_path(path) for path in folder_paths}
        )
        if not normalized_paths:
            return 0

        created_ts = now_ts()
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO folders(folder_path, created_ts, pinned)
                VALUES (?, ?, 0)
                ON CONFLICT(folder_path) DO NOTHING
                """,
                ((path, created_ts) for path in normalized_paths),
            )
        return len(normalized_paths)

    def upsert_msg_part(self, part: PartRecord) -> None:
        self.upsert_msg_parts_bulk([part])

    def upsert_msg_parts_bulk(self, parts: list[PartRecord]) -> int:
        if not parts:
            return 0

        rows = [
            (
                part.msg_id,
                part.chat_id,
                part.folder_path,
                part.file_key,
                part.part_index,
                part.parts_total,
                part.orig_name,
                part.file_size,
                part.caption_raw,
                part.date_ts,
            )
            for part in parts
        ]

        with self.conn:
            self.conn.executemany(_UPSERT_MSG_PART_SQL, rows)
        return len(rows)

    def list_msg_ids(self, chat_id: str) -> list[int]:
        rows = self.conn.execute(
            "SELECT msg_id FROM msg_index WHERE chat_id=? AND is_deleted=0",
            (chat_id,),
        ).fetchall()
        return [int(row["msg_id"]) for row in rows]

    def list_all_indexed_chat_ids(self) -> list[str]:
        """Returns all unique chat IDs present in the msg_index table."""
        rows = self.conn.execute("SELECT DISTINCT chat_id FROM msg_index").fetchall()
        return [str(row["chat_id"]) for row in rows]

    def mark_messages_deleted(
        self, msg_ids: list[int], chat_id: str | None = None
    ) -> int:
        if not msg_ids:
            return 0
        if chat_id:
            refs = [(str(chat_id), int(msg_id)) for msg_id in msg_ids]
            return self.mark_messages_deleted_refs(refs)
        total_deleted = 0
        chunk_size = 900
        with self.conn:
            for start in range(0, len(msg_ids), chunk_size):
                batch = msg_ids[start : start + chunk_size]
                placeholders = ",".join("?" for _ in batch)
                cursor = self.conn.execute(
                    f"UPDATE msg_index SET is_deleted=1 WHERE msg_id IN ({placeholders})",
                    tuple(batch),
                )
                total_deleted += max(0, int(cursor.rowcount))
        return total_deleted

    def mark_messages_deleted_refs(self, refs: list[tuple[str, int]]) -> int:
        if not refs:
            return 0
        grouped: dict[str, list[int]] = {}
        for raw_chat_id, raw_msg_id in refs:
            chat_id = str(raw_chat_id or "").strip()
            if not chat_id:
                continue
            try:
                msg_id = int(raw_msg_id)
            except (TypeError, ValueError):
                continue
            grouped.setdefault(chat_id, []).append(msg_id)
        if not grouped:
            return 0

        total_deleted = 0
        chunk_size = 900
        with self.conn:
            for chat_id, chat_msg_ids in grouped.items():
                unique_ids = sorted(set(chat_msg_ids))
                for start in range(0, len(unique_ids), chunk_size):
                    batch = unique_ids[start : start + chunk_size]
                    placeholders = ",".join("?" for _ in batch)
                    cursor = self.conn.execute(
                        f"UPDATE msg_index SET is_deleted=1 WHERE chat_id=? AND msg_id IN ({placeholders})",
                        (chat_id, *tuple(batch)),
                    )
                    total_deleted += max(0, int(cursor.rowcount))
        return total_deleted

    def mark_messages_lost_refs(self, refs: list[tuple[str, int]]) -> int:
        """Пометить части потерянными (lost_ts), не скрывая строки.

        В отличие от ``mark_messages_deleted_refs`` строка остаётся видимой
        (is_deleted=0), чтобы знать, где часть ДОЛЖНА быть — это позволяет
        отличить «битый» файл от «недозалитого». Успешная переиндексация
        сообщения (upsert) сбрасывает lost_ts обратно в NULL.
        """
        if not refs:
            return 0
        grouped: dict[str, list[int]] = {}
        for raw_chat_id, raw_msg_id in refs:
            chat_id = str(raw_chat_id or "").strip()
            if not chat_id:
                continue
            try:
                msg_id = int(raw_msg_id)
            except (TypeError, ValueError):
                continue
            grouped.setdefault(chat_id, []).append(msg_id)
        if not grouped:
            return 0

        ts = now_ts()
        total_marked = 0
        chunk_size = 900
        with self.conn:
            for chat_id, chat_msg_ids in grouped.items():
                unique_ids = sorted(set(chat_msg_ids))
                for start in range(0, len(unique_ids), chunk_size):
                    batch = unique_ids[start : start + chunk_size]
                    placeholders = ",".join("?" for _ in batch)
                    cursor = self.conn.execute(
                        f"UPDATE msg_index SET lost_ts=? "
                        f"WHERE chat_id=? AND msg_id IN ({placeholders}) "
                        f"AND is_deleted=0",
                        (ts, chat_id, *tuple(batch)),
                    )
                    total_marked += max(0, int(cursor.rowcount))
        return total_marked

    def clear_index(self, chat_id: str) -> None:
        scan_ts = now_ts()
        with self.conn:
            self.conn.execute("DELETE FROM msg_index WHERE chat_id=?", (chat_id,))
            blob_rows = self.conn.execute(
                "SELECT blob_key FROM batch_blobs WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchall()
            blob_keys = [str(row["blob_key"]) for row in blob_rows]
            if blob_keys:
                placeholders = ",".join("?" for _ in blob_keys)
                self.conn.execute(
                    f"DELETE FROM batch_members WHERE blob_key IN ({placeholders})",
                    tuple(blob_keys),
                )
                self.conn.execute(
                    f"DELETE FROM batch_blobs WHERE blob_key IN ({placeholders})",
                    tuple(blob_keys),
                )
            # Remove objects and folders that have no remaining live msg_index rows.
            # Safe for single-chat and future multi-chat scenarios alike.
            self.conn.execute(
                "DELETE FROM objects WHERE folder_path NOT IN ("
                "  SELECT DISTINCT folder_path FROM msg_index WHERE is_deleted=0"
                ")"
            )
            self.conn.execute(
                "DELETE FROM folders WHERE folder_path NOT IN ("
                "  SELECT DISTINCT folder_path FROM msg_index WHERE is_deleted=0"
                ")"
            )
            self.conn.execute(
                """
                INSERT INTO state(chat_id, last_max_msg_id, last_scan_ts)
                VALUES (?, 0, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  last_max_msg_id = 0,
                  last_scan_ts = excluded.last_scan_ts
                """,
                (chat_id, scan_ts),
            )

