from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db.models import (
    ALL_SCHEMA_SQL,
    MIGRATE_V2_ADD_NAME_PINNED,
    MIGRATE_V7_ADD_PROXY_BACKUP,
    MIGRATE_V8_ADD_LOST_TS,
    SCHEMA_VERSION,
    CREATE_INDEX_OBJ_FILEKEY,
    CREATE_INDEX_JOBS_STATUS,
    CREATE_INDEX_OBJ_FOLDER_KEY_STATUS,
    CREATE_ACCOUNTS_TABLE,
    CREATE_INDEX_ACCOUNTS_ACTIVE,
    CREATE_FOLDER_SYNC_TABLE,
    CREATE_OBJECT_NOTES_TABLE,
    CREATE_SHARES_TABLE,
    CREATE_TRASH_TABLE,
)
from app.core.utils import ensure_parent_dir


def connect_db(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    ensure_parent_dir(path)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000;")
    _apply_pragmas(conn)
    # Initialize the schema without creating extra transactions
    try:
        init_schema(conn)
    except sqlite3.OperationalError:
        pass  # A migration may get blocked — this isn't critical
    return conn


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA cache_size=-64000;")  # 64MB page cache for faster batch upsert


def init_schema(conn: sqlite3.Connection) -> None:
    current_version = int(conn.execute("PRAGMA user_version;").fetchone()[0])

    # Always check that the accounts table exists (even if the version is current)
    accounts_exists = (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'"
        ).fetchone()
        is not None
    )

    if current_version >= SCHEMA_VERSION and accounts_exists:
        return

    with conn:
        if current_version == 0:
            for sql in ALL_SCHEMA_SQL:
                conn.execute(sql)
        if current_version < 2:
            try:
                conn.execute(MIGRATE_V2_ADD_NAME_PINNED)
            except sqlite3.OperationalError:
                pass  # column already exists (fresh install ran ALL_SCHEMA_SQL above)
        if current_version < 3:
            _migrate_msg_index_to_composite_pk(conn)
            for sql in ALL_SCHEMA_SQL:
                conn.execute(sql)
        if current_version < 4:
            for sql in ALL_SCHEMA_SQL:
                conn.execute(sql)
        if current_version < 5:
            # Add optimized indexes
            for sql in [
                CREATE_INDEX_OBJ_FILEKEY,
                CREATE_INDEX_JOBS_STATUS,
                CREATE_INDEX_OBJ_FOLDER_KEY_STATUS,
            ]:
                conn.execute(sql)
        if current_version < 6:
            # Add the multi-account table
            for sql in [CREATE_ACCOUNTS_TABLE, CREATE_INDEX_ACCOUNTS_ACTIVE]:
                conn.execute(sql)
        if current_version < 7:
            # Backup proxy for each account (fallback chain)
            try:
                conn.execute(MIGRATE_V7_ADD_PROXY_BACKUP)
            except sqlite3.OperationalError:
                pass  # column already exists (fresh install ran CREATE_ACCOUNTS_TABLE)
        if current_version < 8:
            # Timestamp of when a part was found to be lost (lost_ts)
            try:
                conn.execute(MIGRATE_V8_ADD_LOST_TS)
            except sqlite3.OperationalError:
                pass  # column already exists (fresh install ran ALL_SCHEMA_SQL)
        if current_version < 9:
            # User notes on objects (mini-tags)
            conn.execute(CREATE_OBJECT_NOTES_TABLE)
        if current_version < 10:
            # Folders marked for auto-sync
            conn.execute(CREATE_FOLDER_SYNC_TABLE)
        if current_version < 11:
            # Trash (soft-delete)
            conn.execute(CREATE_TRASH_TABLE)
        if current_version < 12:
            # Share links (public token-based access to a file)
            conn.execute(CREATE_SHARES_TABLE)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION};")


def _migrate_msg_index_to_composite_pk(conn: sqlite3.Connection) -> None:
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='msg_index'"
    ).fetchone()
    if table_exists is None:
        return

    columns = conn.execute("PRAGMA table_info(msg_index)").fetchall()
    if not columns:
        return
    pk_cols = [str(row["name"]) for row in columns if int(row["pk"]) > 0]
    if pk_cols == ["chat_id", "msg_id"]:
        return

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS msg_index_v3 (
            msg_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL,
            folder_path TEXT NOT NULL,
            file_key TEXT NOT NULL,
            part_index INTEGER NOT NULL,
            parts_total INTEGER NOT NULL,
            orig_name TEXT NOT NULL,
            file_size INTEGER,
            caption_raw TEXT,
            date_ts INTEGER NOT NULL,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            name_pinned INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, msg_id)
        )
        """
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO msg_index_v3(
            msg_id,
            chat_id,
            folder_path,
            file_key,
            part_index,
            parts_total,
            orig_name,
            file_size,
            caption_raw,
            date_ts,
            is_deleted,
            name_pinned
        )
        SELECT
            msg_id,
            chat_id,
            folder_path,
            file_key,
            part_index,
            parts_total,
            orig_name,
            file_size,
            caption_raw,
            date_ts,
            is_deleted,
            COALESCE(name_pinned, 0)
        FROM msg_index
        """
    )
    conn.execute("DROP TABLE msg_index")
    conn.execute("ALTER TABLE msg_index_v3 RENAME TO msg_index")
