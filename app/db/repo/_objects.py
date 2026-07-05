"""DbRepo: objects and their aggregates (split out of repo.py)."""

from __future__ import annotations

from typing import Any

from app.core.types import (
    ObjectEntry,
    PartRecord,
)
from app.core.utils import now_ts, normalize_folder_path
from app.db.repo._sql import _REBUILD_ALL_OBJECTS_SQL, _REBUILD_SINGLE_OBJECT_SQL


class _ObjectsMixin:
    def rebuild_objects_aggregates(self) -> None:
        """Rebuild all object aggregates from the msg_index table.

        Deletes and recreates all rows in objects. Used after a scan.
        """
        with self.conn:
            self.conn.execute("DELETE FROM objects")
            self.conn.execute(_REBUILD_ALL_OBJECTS_SQL)

    def rebuild_object_aggregate(
        self, chat_id: str, folder_path: str, file_key: str
    ) -> None:
        normalized_folder = normalize_folder_path(folder_path)
        with self.conn:
            self.conn.execute(
                "DELETE FROM objects WHERE folder_path=? AND file_key=?",
                (normalized_folder, file_key),
            )
            self.conn.execute(
                _REBUILD_SINGLE_OBJECT_SQL,
                (normalized_folder, file_key),
            )

    def list_objects(
        self,
        folder_path: str | None = None,
        search: str | None = None,
        status: str | None = None,
    ) -> list[ObjectEntry]:
        return self.list_objects_unified(
            folder_path=folder_path,
            search=search,
            status=status,
            recursive=False,
        )

    def list_objects_by_folder(
        self,
        folder_path: str,
        search: str | None = None,
        status: str | None = None,
    ) -> list[ObjectEntry]:
        return self.list_objects(folder_path=folder_path, search=search, status=status)

    def list_objects_unified(
        self,
        folder_path: str | None = None,
        search: str | None = None,
        status: str | None = None,
        *,
        recursive: bool = False,
    ) -> list[ObjectEntry]:
        normalized_folder: str | None = None
        if folder_path is not None:
            normalized_folder = normalize_folder_path(folder_path)
        normalized_search = str(search or "").strip() or None
        normalized_status = str(status or "").strip() or None

        regular_clauses = [
            "NOT EXISTS (SELECT 1 FROM batch_blobs bb WHERE bb.blob_key = objects.file_key AND bb.is_deleted = 0)",
            "NOT EXISTS (SELECT 1 FROM trash tr WHERE tr.folder_path = objects.folder_path AND tr.file_key = objects.file_key)",
        ]
        regular_params: list[Any] = []
        if normalized_folder is not None:
            if recursive:
                regular_clauses.append(
                    "(objects.folder_path = ? OR objects.folder_path LIKE ?)"
                )
                regular_params.extend([normalized_folder, f"{normalized_folder}/%"])
            else:
                regular_clauses.append("objects.folder_path = ?")
                regular_params.append(normalized_folder)
        if normalized_search:
            regular_clauses.append("LOWER(objects.orig_name) LIKE LOWER(?)")
            regular_params.append(f"%{normalized_search}%")
        if normalized_status:
            regular_clauses.append("objects.status = ?")
            regular_params.append(normalized_status)
        regular_query = (
            "SELECT file_key, folder_path, orig_name, parts_total, have_parts, status, total_size, last_seen_ts "
            "FROM objects "
            "WHERE " + " AND ".join(regular_clauses)
        )
        regular_rows = self.conn.execute(
            regular_query, tuple(regular_params)
        ).fetchall()
        result: list[ObjectEntry] = [
            ObjectEntry(
                file_key=row["file_key"],
                folder_path=row["folder_path"],
                orig_name=row["orig_name"],
                parts_total=int(row["parts_total"]),
                have_parts=int(row["have_parts"]),
                status=row["status"],
                total_size=int(row["total_size"])
                if row["total_size"] is not None
                else None,
                last_seen_ts=int(row["last_seen_ts"]),
                storage_kind="regular",
            )
            for row in regular_rows
        ]

        if normalized_status and normalized_status != "complete":
            return sorted(
                result,
                key=lambda item: (-int(item.last_seen_ts), str(item.orig_name).lower()),
            )

        member_clauses = [
            "bm.deleted_ts IS NULL",
            "bb.is_deleted = 0",
            "NOT EXISTS (SELECT 1 FROM trash tr WHERE tr.folder_path = bm.folder_path AND tr.file_key = bm.file_key)",
        ]
        member_params: list[Any] = []
        if normalized_folder is not None:
            if recursive:
                member_clauses.append("(bm.folder_path = ? OR bm.folder_path LIKE ?)")
                member_params.extend([normalized_folder, f"{normalized_folder}/%"])
            else:
                member_clauses.append("bm.folder_path = ?")
                member_params.append(normalized_folder)
        if normalized_search:
            member_clauses.append("LOWER(bm.orig_name) LIKE LOWER(?)")
            member_params.append(f"%{normalized_search}%")

        member_query = (
            "SELECT "
            "  bm.file_key AS file_key, "
            "  bm.folder_path AS folder_path, "
            "  bm.orig_name AS orig_name, "
            "  bm.member_size AS total_size, "
            "  bm.updated_ts AS last_seen_ts, "
            "  bm.blob_key AS blob_key "
            "FROM batch_members bm "
            "JOIN batch_blobs bb ON bb.blob_key = bm.blob_key "
            "WHERE " + " AND ".join(member_clauses)
        )
        member_rows = self.conn.execute(member_query, tuple(member_params)).fetchall()
        result.extend(
            [
                ObjectEntry(
                    file_key=row["file_key"],
                    folder_path=row["folder_path"],
                    orig_name=row["orig_name"],
                    parts_total=1,
                    have_parts=1,
                    status="complete",
                    total_size=int(row["total_size"])
                    if row["total_size"] is not None
                    else None,
                    last_seen_ts=int(row["last_seen_ts"]),
                    storage_kind="batch_member",
                    blob_key=str(row["blob_key"]),
                )
                for row in member_rows
            ]
        )
        return sorted(
            result,
            key=lambda item: (-int(item.last_seen_ts), str(item.orig_name).lower()),
        )

    def count_objects_recursive(self, folder_path: str) -> int:
        """Cheap COUNT of visible files under a folder (regular objects +
        batch members, minus trash), mirroring list_objects_unified filters.
        Used by the UI to skip expensive scans on very large trees without
        materializing every row first."""
        normalized_folder = normalize_folder_path(folder_path)
        params = (normalized_folder, f"{normalized_folder}/%")
        regular = self.conn.execute(
            """
            SELECT COUNT(*) FROM objects
            WHERE (folder_path = ? OR folder_path LIKE ?)
              AND NOT EXISTS (SELECT 1 FROM batch_blobs bb
                              WHERE bb.blob_key = objects.file_key AND bb.is_deleted = 0)
              AND NOT EXISTS (SELECT 1 FROM trash tr
                              WHERE tr.folder_path = objects.folder_path
                                AND tr.file_key = objects.file_key)
            """,
            params,
        ).fetchone()[0]
        members = self.conn.execute(
            """
            SELECT COUNT(*) FROM batch_members bm
            JOIN batch_blobs bb ON bb.blob_key = bm.blob_key
            WHERE bm.deleted_ts IS NULL AND bb.is_deleted = 0
              AND (bm.folder_path = ? OR bm.folder_path LIKE ?)
              AND NOT EXISTS (SELECT 1 FROM trash tr
                              WHERE tr.folder_path = bm.folder_path
                                AND tr.file_key = bm.file_key)
            """,
            params,
        ).fetchone()[0]
        return int(regular) + int(members)

    def get_parts_for_object(self, folder_path: str, file_key: str) -> list[PartRecord]:
        query = """
        WITH latest AS (
            SELECT parts_total, orig_name
            FROM msg_index
            WHERE folder_path = ?
              AND file_key = ?
              AND is_deleted = 0
            ORDER BY date_ts DESC, msg_id DESC, chat_id DESC
            LIMIT 1
        ),
        ranked AS (
            SELECT
                m.msg_id,
                m.chat_id,
                m.folder_path,
                m.file_key,
                m.part_index,
                m.parts_total,
                m.orig_name,
                m.file_size,
                m.caption_raw,
                m.date_ts,
                m.lost_ts,
                ROW_NUMBER() OVER (
                    PARTITION BY m.part_index
                    ORDER BY m.date_ts DESC, m.msg_id DESC, m.chat_id DESC
                ) AS rn
            FROM msg_index m
            JOIN latest l
              ON m.parts_total = l.parts_total
             AND m.orig_name = l.orig_name
            WHERE m.folder_path = ?
              AND m.file_key = ?
              AND m.is_deleted = 0
        )
        SELECT msg_id, chat_id, folder_path, file_key, part_index, parts_total,
               orig_name, file_size, caption_raw, date_ts, lost_ts
        FROM ranked
        WHERE rn = 1
        ORDER BY part_index ASC;
        """
        normalized_folder = normalize_folder_path(folder_path)
        rows = self.conn.execute(
            query,
            (normalized_folder, file_key, normalized_folder, file_key),
        ).fetchall()
        return [
            PartRecord(
                msg_id=int(row["msg_id"]),
                chat_id=row["chat_id"],
                folder_path=row["folder_path"],
                file_key=row["file_key"],
                part_index=int(row["part_index"]),
                parts_total=int(row["parts_total"]),
                orig_name=row["orig_name"],
                file_size=int(row["file_size"])
                if row["file_size"] is not None
                else None,
                caption_raw=row["caption_raw"],
                date_ts=int(row["date_ts"]),
                lost_ts=int(row["lost_ts"]) if row["lost_ts"] is not None else None,
            )
            for row in rows
        ]

    def get_part_chat_ids_by_folder(self, folder_path: str) -> dict[str, set[str]]:
        """file_key -> the set of chat_ids of its (non-deleted) parts, per folder.

        A cheap aggregate for computing offline/damaged in the grid without N queries.
        """
        normalized = normalize_folder_path(folder_path)
        rows = self.conn.execute(
            "SELECT file_key, chat_id FROM msg_index "
            "WHERE folder_path=? AND is_deleted=0",
            (normalized,),
        ).fetchall()
        result: dict[str, set[str]] = {}
        for row in rows:
            result.setdefault(str(row["file_key"]), set()).add(str(row["chat_id"]))
        return result

    def get_lost_file_keys_by_folder(self, folder_path: str) -> set[str]:
        """The set of file_keys with at least one lost (lost_ts) part, per folder."""
        normalized = normalize_folder_path(folder_path)
        rows = self.conn.execute(
            "SELECT DISTINCT file_key FROM msg_index "
            "WHERE folder_path=? AND is_deleted=0 AND lost_ts IS NOT NULL",
            (normalized,),
        ).fetchall()
        return {str(row["file_key"]) for row in rows}

    def set_object_note(self, folder_path: str, file_key: str, note: str) -> None:
        normalized = normalize_folder_path(folder_path)
        ts = now_ts()
        with self.conn:
            self.conn.execute(
                "INSERT INTO object_notes(folder_path, file_key, note, updated_ts) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(folder_path, file_key) DO UPDATE SET "
                "note=excluded.note, updated_ts=excluded.updated_ts",
                (normalized, str(file_key), str(note or ""), ts),
            )

    def get_object_note(self, folder_path: str, file_key: str) -> str:
        normalized = normalize_folder_path(folder_path)
        row = self.conn.execute(
            "SELECT note FROM object_notes WHERE folder_path=? AND file_key=?",
            (normalized, str(file_key)),
        ).fetchone()
        return str(row["note"]) if row is not None else ""

    def get_object_notes_by_folder(self, folder_path: str) -> dict[str, str]:
        normalized = normalize_folder_path(folder_path)
        rows = self.conn.execute(
            "SELECT file_key, note FROM object_notes WHERE folder_path=?",
            (normalized,),
        ).fetchall()
        return {str(row["file_key"]): str(row["note"]) for row in rows}
