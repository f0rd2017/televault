from __future__ import annotations

SCHEMA_VERSION = 12

CREATE_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS state (
    chat_id TEXT PRIMARY KEY,
    last_max_msg_id INTEGER NOT NULL DEFAULT 0,
    last_scan_ts INTEGER
);
"""

CREATE_FOLDERS_TABLE = """
CREATE TABLE IF NOT EXISTS folders (
    folder_path TEXT PRIMARY KEY,
    created_ts INTEGER NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0
);
"""

CREATE_MSG_INDEX_TABLE = """
CREATE TABLE IF NOT EXISTS msg_index (
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
    lost_ts INTEGER,
    PRIMARY KEY (chat_id, msg_id)
);
"""

MIGRATE_V2_ADD_NAME_PINNED = """
ALTER TABLE msg_index ADD COLUMN name_pinned INTEGER NOT NULL DEFAULT 0
"""

MIGRATE_V7_ADD_PROXY_BACKUP = """
ALTER TABLE accounts ADD COLUMN proxy_backup TEXT NOT NULL DEFAULT ''
"""

MIGRATE_V8_ADD_LOST_TS = """
ALTER TABLE msg_index ADD COLUMN lost_ts INTEGER
"""

CREATE_OBJECTS_TABLE = """
CREATE TABLE IF NOT EXISTS objects (
    file_key TEXT NOT NULL,
    folder_path TEXT NOT NULL,
    orig_name TEXT NOT NULL,
    parts_total INTEGER NOT NULL,
    have_parts INTEGER NOT NULL,
    status TEXT NOT NULL,
    total_size INTEGER,
    last_seen_ts INTEGER NOT NULL,
    PRIMARY KEY (file_key, folder_path)
);
"""

CREATE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    error_text TEXT
);
"""

CREATE_BATCH_BLOBS_TABLE = """
CREATE TABLE IF NOT EXISTS batch_blobs (
    blob_key TEXT PRIMARY KEY,
    folder_path TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    msg_id INTEGER NOT NULL,
    blob_name TEXT NOT NULL,
    blob_size INTEGER,
    blob_sha256 TEXT,
    manifest_json TEXT NOT NULL,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    created_ts INTEGER NOT NULL,
    last_seen_ts INTEGER NOT NULL
);
"""

CREATE_BATCH_MEMBERS_TABLE = """
CREATE TABLE IF NOT EXISTS batch_members (
    folder_path TEXT NOT NULL,
    file_key TEXT NOT NULL,
    blob_key TEXT NOT NULL,
    orig_name TEXT NOT NULL,
    member_index INTEGER NOT NULL,
    member_size INTEGER,
    member_sha256 TEXT,
    deleted_ts INTEGER,
    name_pinned INTEGER NOT NULL DEFAULT 0,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    PRIMARY KEY (folder_path, file_key)
);
"""

CREATE_INDEX_MSG_CHAT_MSGID = """
CREATE INDEX IF NOT EXISTS idx_msg_chat_msgid
ON msg_index(chat_id, msg_id);
"""

CREATE_INDEX_MSG_MSGID = """
CREATE INDEX IF NOT EXISTS idx_msg_msgid
ON msg_index(msg_id);
"""

CREATE_INDEX_MSG_OBJECT = """
CREATE INDEX IF NOT EXISTS idx_msg_object
ON msg_index(folder_path, file_key, is_deleted);
"""

CREATE_INDEX_MSG_SCAN = """
CREATE INDEX IF NOT EXISTS idx_msg_scan
ON msg_index(chat_id, is_deleted, date_ts);
"""

CREATE_INDEX_OBJ_FOLDER_STATUS = """
CREATE INDEX IF NOT EXISTS idx_obj_folder_status
ON objects(folder_path, status);
"""

CREATE_INDEX_BATCH_MEMBERS_BLOB = """
CREATE INDEX IF NOT EXISTS idx_batch_members_blob
ON batch_members(blob_key);
"""

CREATE_INDEX_BATCH_MEMBERS_FOLDER = """
CREATE INDEX IF NOT EXISTS idx_batch_members_folder
ON batch_members(folder_path);
"""

CREATE_INDEX_BATCH_MEMBERS_DELETED = """
CREATE INDEX IF NOT EXISTS idx_batch_members_deleted
ON batch_members(deleted_ts);
"""

CREATE_INDEX_BATCH_BLOBS_FOLDER_DELETED = """
CREATE INDEX IF NOT EXISTS idx_batch_blobs_folder_deleted
ON batch_blobs(folder_path, is_deleted);
"""

# Optimization: index for fast object lookups by file_key (UI lookup)
CREATE_INDEX_OBJ_FILEKEY = """
CREATE INDEX IF NOT EXISTS idx_obj_filekey
ON objects(file_key);
"""

# Optimization: index for filtering jobs by status (queue, history)
CREATE_INDEX_JOBS_STATUS = """
CREATE INDEX IF NOT EXISTS idx_jobs_status
ON jobs(status, created_ts DESC);
"""

# Optimization: a covering index for looking up objects without a JOIN
CREATE_INDEX_OBJ_FOLDER_KEY_STATUS = """
CREATE INDEX IF NOT EXISTS idx_obj_folder_key_status
ON objects(folder_path, file_key, status, last_seen_ts DESC);
"""

# Table of multiple accounts used for uploading
CREATE_ACCOUNTS_TABLE = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    session_path TEXT NOT NULL,
    tg_api_id INTEGER NOT NULL,
    tg_api_hash TEXT NOT NULL,
    chat_target TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    is_primary INTEGER NOT NULL DEFAULT 0,
    proxy TEXT NOT NULL DEFAULT '',
    proxy_backup TEXT NOT NULL DEFAULT '',
    phone_masked TEXT NOT NULL DEFAULT '',
    user_id INTEGER NOT NULL DEFAULT 0,
    username TEXT NOT NULL DEFAULT '',
    is_premium INTEGER NOT NULL DEFAULT 0,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL
);
"""

CREATE_INDEX_ACCOUNTS_ACTIVE = """
CREATE INDEX IF NOT EXISTS idx_accounts_active
ON accounts(is_active, is_primary DESC);
"""

CREATE_OBJECT_NOTES_TABLE = """
CREATE TABLE IF NOT EXISTS object_notes (
    folder_path TEXT NOT NULL,
    file_key TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    updated_ts INTEGER NOT NULL,
    PRIMARY KEY (folder_path, file_key)
);
"""

CREATE_FOLDER_SYNC_TABLE = """
CREATE TABLE IF NOT EXISTS folder_sync (
    folder_path TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_ts INTEGER NOT NULL DEFAULT 0
);
"""

# Trash (soft-delete): the object is hidden from normal listings but NOT
# deleted from the channel. Independent of reconcile (which never touches
# this table) — so trash survives a reconciliation pass. Restoring = delete
# the row; "delete forever" = an actual remote delete + deleting the row.
CREATE_TRASH_TABLE = """
CREATE TABLE IF NOT EXISTS trash (
    folder_path TEXT NOT NULL,
    file_key TEXT NOT NULL,
    orig_name TEXT NOT NULL,
    storage_kind TEXT NOT NULL DEFAULT 'regular',
    total_size INTEGER,
    trashed_ts INTEGER NOT NULL,
    PRIMARY KEY (folder_path, file_key)
);
"""

# Share links: a public link to a file (by token) with an optional password
# and expiry. Independent of reconcile. Served by the local HTTP server (REST
# API): GET /share/<token> assembles the file from its chunks and serves it
# with Range support.
CREATE_SHARES_TABLE = """
CREATE TABLE IF NOT EXISTS shares (
    token TEXT PRIMARY KEY,
    folder_path TEXT NOT NULL,
    file_key TEXT NOT NULL,
    orig_name TEXT NOT NULL,
    total_size INTEGER,
    password_hash TEXT NOT NULL DEFAULT '',
    expires_ts INTEGER NOT NULL DEFAULT 0,
    revoked INTEGER NOT NULL DEFAULT 0,
    download_count INTEGER NOT NULL DEFAULT 0,
    created_ts INTEGER NOT NULL
);
"""

ALL_SCHEMA_SQL = [
    CREATE_STATE_TABLE,
    CREATE_FOLDERS_TABLE,
    CREATE_MSG_INDEX_TABLE,
    CREATE_OBJECTS_TABLE,
    CREATE_JOBS_TABLE,
    CREATE_BATCH_BLOBS_TABLE,
    CREATE_BATCH_MEMBERS_TABLE,
    CREATE_INDEX_MSG_CHAT_MSGID,
    CREATE_INDEX_MSG_MSGID,
    CREATE_INDEX_MSG_OBJECT,
    CREATE_INDEX_MSG_SCAN,
    CREATE_INDEX_OBJ_FOLDER_STATUS,
    CREATE_INDEX_BATCH_MEMBERS_BLOB,
    CREATE_INDEX_BATCH_MEMBERS_FOLDER,
    CREATE_INDEX_BATCH_MEMBERS_DELETED,
    CREATE_INDEX_BATCH_BLOBS_FOLDER_DELETED,
    CREATE_INDEX_OBJ_FILEKEY,
    CREATE_INDEX_JOBS_STATUS,
    CREATE_INDEX_OBJ_FOLDER_KEY_STATUS,
    CREATE_ACCOUNTS_TABLE,
    CREATE_INDEX_ACCOUNTS_ACTIVE,
    CREATE_OBJECT_NOTES_TABLE,
    CREATE_FOLDER_SYNC_TABLE,
    CREATE_TRASH_TABLE,
    CREATE_SHARES_TABLE,
]
