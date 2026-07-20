"""DbRepo: renaming/deleting objects, and accounts (split out of repo.py)."""

from __future__ import annotations


from televault.core.types import (
    ObjectEntry,
    TelegramAccount,
)
from televault.core.utils import now_ts, normalize_folder_path


class _TailMixin:
    def rename_object(self, folder_path: str, file_key: str, new_name: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE objects SET orig_name = ? WHERE folder_path = ? AND file_key = ?",
                (new_name, folder_path, file_key),
            )
            # Set name_pinned=1 so future scans never overwrite the renamed value
            self.conn.execute(
                "UPDATE msg_index SET orig_name = ?, name_pinned = 1 WHERE folder_path = ? AND file_key = ?",
                (new_name, folder_path, file_key),
            )

    def update_caption_raw(
        self, msg_id: int, caption_raw: str, chat_id: str | None = None
    ) -> None:
        with self.conn:
            if chat_id:
                self.conn.execute(
                    "UPDATE msg_index SET caption_raw = ? WHERE chat_id = ? AND msg_id = ?",
                    (caption_raw, str(chat_id), msg_id),
                )
            else:
                self.conn.execute(
                    "UPDATE msg_index SET caption_raw = ? WHERE msg_id = ?",
                    (caption_raw, msg_id),
                )

    def list_objects_recursive(self, folder_path: str) -> list[ObjectEntry]:
        return self.list_objects_unified(folder_path=folder_path, recursive=True)

    def delete_folder(self, folder_path: str) -> None:
        normalized = normalize_folder_path(folder_path)
        ts = now_ts()
        with self.conn:
            self.conn.execute(
                "DELETE FROM objects WHERE folder_path = ? OR folder_path LIKE ?",
                (normalized, f"{normalized}/%"),
            )
            self.conn.execute(
                "DELETE FROM folders WHERE folder_path = ? OR folder_path LIKE ?",
                (normalized, f"{normalized}/%"),
            )
            self.conn.execute(
                """
                UPDATE batch_members
                SET deleted_ts = COALESCE(deleted_ts, ?), updated_ts = ?
                WHERE folder_path = ? OR folder_path LIKE ?
                """,
                (ts, ts, normalized, f"{normalized}/%"),
            )

    # === Account Management ===

    def list_accounts(self) -> list[TelegramAccount]:
        rows = self.conn.execute(
            "SELECT * FROM accounts ORDER BY is_primary DESC, id ASC"
        ).fetchall()
        return [
            TelegramAccount(
                id=int(row["id"]),
                label=str(row["label"]),
                session_path=str(row["session_path"]),
                tg_api_id=int(row["tg_api_id"]),
                tg_api_hash=str(row["tg_api_hash"]),
                chat_target=str(row["chat_target"]),
                is_active=bool(row["is_active"]),
                is_primary=bool(row["is_primary"]),
                proxy=str(row["proxy"] or ""),
                phone_masked=str(row["phone_masked"] or ""),
                user_id=int(row["user_id"] or 0),
                username=str(row["username"] or ""),
                is_premium=bool(row["is_premium"]),
            )
            for row in rows
        ]

    def get_account(self, account_id: int) -> TelegramAccount | None:
        row = self.conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None:
            return None
        return TelegramAccount(
            id=int(row["id"]),
            label=str(row["label"]),
            session_path=str(row["session_path"]),
            tg_api_id=int(row["tg_api_id"]),
            tg_api_hash=str(row["tg_api_hash"]),
            chat_target=str(row["chat_target"]),
            is_active=bool(row["is_active"]),
            is_primary=bool(row["is_primary"]),
            proxy=str(row["proxy"] or ""),
            proxy_backup=str(row["proxy_backup"] or ""),
            phone_masked=str(row["phone_masked"] or ""),
            user_id=int(row["user_id"] or 0),
            username=str(row["username"] or ""),
            is_premium=bool(row["is_premium"]),
        )

    def insert_account(self, account: TelegramAccount) -> int:
        ts = now_ts()
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO accounts(
                    label, session_path, tg_api_id, tg_api_hash, chat_target,
                    is_active, is_primary, proxy, proxy_backup, phone_masked, user_id,
                    username, is_premium, created_ts, updated_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account.label,
                    account.session_path,
                    account.tg_api_id,
                    account.tg_api_hash,
                    account.chat_target,
                    1 if account.is_active else 0,
                    1 if account.is_primary else 0,
                    account.proxy,
                    account.proxy_backup,
                    account.phone_masked,
                    account.user_id,
                    account.username,
                    1 if account.is_premium else 0,
                    ts,
                    ts,
                ),
            )
            return int(cursor.lastrowid)

    def update_account(self, account_id: int, **kwargs) -> None:
        if not kwargs:
            return
        _ALLOWED_ACCOUNT_COLS = {
            "label",
            "session_path",
            "tg_api_id",
            "tg_api_hash",
            "chat_target",
            "is_active",
            "is_primary",
            "proxy",
            "proxy_backup",
            "phone_masked",
            "user_id",
            "username",
            "is_premium",
            "updated_ts",
        }
        ts = now_ts()
        set_clauses = []
        values = []
        for key, value in kwargs.items():
            if key not in _ALLOWED_ACCOUNT_COLS:
                raise ValueError(f"Unknown account column: {key}")
            set_clauses.append(f"{key} = ?")
            values.append(value)
        set_clauses.append("updated_ts = ?")
        values.append(ts)
        values.append(account_id)

        with self.conn:
            self.conn.execute(
                f"UPDATE accounts SET {', '.join(set_clauses)} WHERE id = ?",
                tuple(values),
            )

    def delete_account(self, account_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    def get_active_accounts(self) -> list[TelegramAccount]:
        return [acc for acc in self.list_accounts() if acc.is_active]

    def get_next_account_id(self) -> int:
        row = self.conn.execute("SELECT MAX(id) FROM accounts").fetchone()
        return int(row[0] or 0) + 1
