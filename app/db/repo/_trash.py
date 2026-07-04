"""DbRepo: trash, sharing, folder sync, links, and batch blob keys (split out of repo.py)."""

from __future__ import annotations

from typing import Any

from app.core.types import (
    ObjectEntry,
    PartRecord,
)
from app.core.utils import now_ts, normalize_folder_path


class _TrashShareSyncMixin:
    # === Trash (soft-delete) ===

    def move_to_trash(
        self,
        folder_path: str,
        file_key: str,
        orig_name: str,
        storage_kind: str = "regular",
        total_size: int | None = None,
    ) -> None:
        normalized = normalize_folder_path(folder_path)
        with self.conn:
            self.conn.execute(
                "INSERT INTO trash("
                "  folder_path, file_key, orig_name, storage_kind, total_size, trashed_ts"
                ") VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(folder_path, file_key) DO UPDATE SET "
                "  orig_name=excluded.orig_name, storage_kind=excluded.storage_kind, "
                "  total_size=excluded.total_size, trashed_ts=excluded.trashed_ts",
                (
                    normalized,
                    str(file_key),
                    str(orig_name),
                    str(storage_kind or "regular"),
                    int(total_size) if total_size is not None else None,
                    now_ts(),
                ),
            )

    def list_trash(self) -> list[ObjectEntry]:
        """Trash contents as ObjectEntry (so the grid model can be reused).
        last_seen_ts = the moment the item was moved to trash (for sorting newest-first)."""
        rows = self.conn.execute(
            "SELECT folder_path, file_key, orig_name, storage_kind, total_size, "
            "       trashed_ts FROM trash ORDER BY trashed_ts DESC"
        ).fetchall()
        return [
            ObjectEntry(
                file_key=str(row["file_key"]),
                folder_path=str(row["folder_path"]),
                orig_name=str(row["orig_name"]),
                parts_total=0,
                have_parts=0,
                status="complete",
                total_size=int(row["total_size"])
                if row["total_size"] is not None
                else None,
                last_seen_ts=int(row["trashed_ts"]),
                storage_kind=str(row["storage_kind"] or "regular"),
            )
            for row in rows
        ]

    def restore_from_trash(self, folder_path: str, file_key: str) -> int:
        return self.delete_trash_entry(folder_path, file_key)

    def delete_trash_entry(self, folder_path: str, file_key: str) -> int:
        normalized = normalize_folder_path(folder_path)
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM trash WHERE folder_path=? AND file_key=?",
                (normalized, str(file_key)),
            )
        return max(0, int(cursor.rowcount))

    def count_trash(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM trash").fetchone()
        return int(row["c"]) if row is not None else 0

    # ── Share links ───────────────────────────────────────────────────────────
    @staticmethod
    def _share_row_to_dict(row) -> dict[str, Any]:
        return {
            "token": str(row["token"]),
            "folder_path": str(row["folder_path"]),
            "file_key": str(row["file_key"]),
            "orig_name": str(row["orig_name"]),
            "total_size": int(row["total_size"])
            if row["total_size"] is not None
            else None,
            "has_password": bool(str(row["password_hash"] or "")),
            "password_hash": str(row["password_hash"] or ""),
            "expires_ts": int(row["expires_ts"] or 0),
            "revoked": bool(int(row["revoked"] or 0)),
            "download_count": int(row["download_count"] or 0),
            "created_ts": int(row["created_ts"] or 0),
        }

    def create_share(
        self,
        token: str,
        folder_path: str,
        file_key: str,
        orig_name: str,
        *,
        total_size: int | None = None,
        password_hash: str = "",
        expires_ts: int = 0,
    ) -> str:
        normalized = normalize_folder_path(folder_path)
        with self.conn:
            self.conn.execute(
                "INSERT INTO shares("
                "  token, folder_path, file_key, orig_name, total_size, "
                "  password_hash, expires_ts, revoked, download_count, created_ts"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?)",
                (
                    str(token),
                    normalized,
                    str(file_key),
                    str(orig_name),
                    int(total_size) if total_size is not None else None,
                    str(password_hash or ""),
                    int(expires_ts or 0),
                    now_ts(),
                ),
            )
        return str(token)

    def get_share(self, token: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM shares WHERE token=?", (str(token),)
        ).fetchone()
        return self._share_row_to_dict(row) if row is not None else None

    def list_shares(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM shares ORDER BY created_ts DESC"
        ).fetchall()
        return [self._share_row_to_dict(r) for r in rows]

    def revoke_share(self, token: str) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE shares SET revoked=1 WHERE token=?", (str(token),)
            )
        return max(0, int(cursor.rowcount))

    def delete_share(self, token: str) -> int:
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM shares WHERE token=?", (str(token),)
            )
        return max(0, int(cursor.rowcount))

    def increment_share_downloads(self, token: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE shares SET download_count=download_count+1 WHERE token=?",
                (str(token),),
            )

    def set_folder_sync(self, folder_path: str, enabled: bool) -> None:
        normalized = normalize_folder_path(folder_path)
        with self.conn:
            self.conn.execute(
                "INSERT INTO folder_sync(folder_path, enabled, updated_ts) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(folder_path) DO UPDATE SET "
                "enabled=excluded.enabled, updated_ts=excluded.updated_ts",
                (normalized, 1 if enabled else 0, now_ts()),
            )

    def is_folder_synced(self, folder_path: str) -> bool:
        normalized = normalize_folder_path(folder_path)
        row = self.conn.execute(
            "SELECT enabled FROM folder_sync WHERE folder_path=?",
            (normalized,),
        ).fetchone()
        return bool(row is not None and int(row["enabled"]) == 1)

    def list_synced_folders(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT folder_path FROM folder_sync WHERE enabled=1 ORDER BY folder_path"
        ).fetchall()
        return [str(row["folder_path"]) for row in rows]

    def get_all_msg_index_refs_for_object(
        self, folder_path: str, file_key: str
    ) -> list[tuple[str, int]]:
        """Get ALL live message references (chat_id, msg_id) for an object.

        Unlike get_parts_for_object, this returns everything regardless of consistency
        or 'latest' revision. Used for full remote deletion.
        """
        normalized_folder = normalize_folder_path(folder_path)
        rows = self.conn.execute(
            "SELECT chat_id, msg_id FROM msg_index WHERE folder_path=? AND file_key=? AND is_deleted=0",
            (normalized_folder, file_key),
        ).fetchall()
        return [(str(row["chat_id"]), int(row["msg_id"])) for row in rows]

    def get_live_msg_refs_for_folder(
        self, folder_path: str, *, recursive: bool = True
    ) -> list[tuple[str, int]]:
        """Return (chat_id, msg_id) for all live messages in a folder subtree.

        Sourced directly from msg_index (is_deleted=0), so it reflects what the
        last reconcile actually saw in the channel — covering both regular file
        parts and batch-blob messages uniformly. Used for folder deletion so a
        retry re-deletes anything a previous failed attempt left in the channel
        (the batch_blobs.is_deleted flag can get out of sync with reality).
        """
        normalized = normalize_folder_path(folder_path)
        if recursive:
            rows = self.conn.execute(
                "SELECT chat_id, msg_id FROM msg_index "
                "WHERE is_deleted = 0 AND (folder_path = ? OR folder_path LIKE ?)",
                (normalized, f"{normalized}/%"),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT chat_id, msg_id FROM msg_index "
                "WHERE is_deleted = 0 AND folder_path = ?",
                (normalized,),
            ).fetchall()
        return [(str(row["chat_id"]), int(row["msg_id"])) for row in rows]

    def mark_folder_batch_blobs_deleted(
        self, folder_path: str, *, recursive: bool = True
    ) -> int:
        """Mark every batch blob in a folder subtree as deleted (local cleanup)."""
        normalized = normalize_folder_path(folder_path)
        with self.conn:
            if recursive:
                cursor = self.conn.execute(
                    "UPDATE batch_blobs SET is_deleted = 1, last_seen_ts = ? "
                    "WHERE folder_path = ? OR folder_path LIKE ?",
                    (now_ts(), normalized, f"{normalized}/%"),
                )
            else:
                cursor = self.conn.execute(
                    "UPDATE batch_blobs SET is_deleted = 1, last_seen_ts = ? "
                    "WHERE folder_path = ?",
                    (now_ts(), normalized),
                )
        return int(cursor.rowcount or 0)

    def list_batch_blob_keys_by_folder(
        self, folder_path: str, *, recursive: bool = True
    ) -> list[tuple[str, str]]:
        """Return (blob_key, folder_path) for live batch blobs in a folder.

        Includes blobs whose members were already logically deleted — the blob
        message itself may still live in the channel (is_deleted=0) and must be
        removed so a later reconcile does not resurrect the folder.
        """
        normalized = normalize_folder_path(folder_path)
        if recursive:
            rows = self.conn.execute(
                "SELECT blob_key, folder_path FROM batch_blobs "
                "WHERE is_deleted = 0 AND (folder_path = ? OR folder_path LIKE ?)",
                (normalized, f"{normalized}/%"),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT blob_key, folder_path FROM batch_blobs "
                "WHERE is_deleted = 0 AND folder_path = ?",
                (normalized,),
            ).fetchall()
        return [(str(row["blob_key"]), str(row["folder_path"])) for row in rows]

    def get_parts_for_blob(self, blob_key: str) -> list[PartRecord]:
        row = self.conn.execute(
            "SELECT folder_path FROM batch_blobs WHERE blob_key = ? AND is_deleted = 0",
            (str(blob_key),),
        ).fetchone()
        if row is None:
            return []
        return self.get_parts_for_object(str(row["folder_path"]), str(blob_key))
