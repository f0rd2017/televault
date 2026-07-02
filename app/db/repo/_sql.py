"""SQL-константы DbRepo (вынесены из repo.py при дроблении god-модуля)."""

_UPSERT_MSG_PART_SQL = """
INSERT INTO msg_index(
  msg_id, chat_id, folder_path, file_key, part_index, parts_total,
  orig_name, file_size, caption_raw, date_ts, is_deleted, name_pinned
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
ON CONFLICT(chat_id, msg_id) DO UPDATE SET
  folder_path = excluded.folder_path,
  file_key = excluded.file_key,
  part_index = excluded.part_index,
  parts_total = excluded.parts_total,
  orig_name = CASE WHEN msg_index.name_pinned = 1 THEN msg_index.orig_name ELSE excluded.orig_name END,
  file_size = excluded.file_size,
  caption_raw = excluded.caption_raw,
  date_ts = excluded.date_ts,
  is_deleted = 0,
  lost_ts = NULL
"""

_UPSERT_BATCH_BLOB_SQL = """
INSERT INTO batch_blobs(
  blob_key, folder_path, chat_id, msg_id, blob_name, blob_size, blob_sha256, manifest_json,
  is_deleted, created_ts, last_seen_ts
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(blob_key) DO UPDATE SET
  folder_path = excluded.folder_path,
  chat_id = excluded.chat_id,
  msg_id = excluded.msg_id,
  blob_name = excluded.blob_name,
  blob_size = excluded.blob_size,
  blob_sha256 = excluded.blob_sha256,
  manifest_json = excluded.manifest_json,
  is_deleted = excluded.is_deleted,
  last_seen_ts = excluded.last_seen_ts
"""

_UPSERT_BATCH_MEMBER_SQL = """
INSERT INTO batch_members(
  folder_path, file_key, blob_key, orig_name, member_index, member_size, member_sha256,
  deleted_ts, name_pinned, created_ts, updated_ts
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(folder_path, file_key) DO UPDATE SET
  blob_key = excluded.blob_key,
  orig_name = CASE
    WHEN batch_members.name_pinned = 1 THEN batch_members.orig_name
    ELSE excluded.orig_name
  END,
  member_index = excluded.member_index,
  member_size = excluded.member_size,
  member_sha256 = excluded.member_sha256,
  deleted_ts = excluded.deleted_ts,
  name_pinned = CASE
    WHEN excluded.name_pinned = 1 THEN 1
    ELSE batch_members.name_pinned
  END,
  updated_ts = excluded.updated_ts
"""

_REBUILD_SINGLE_OBJECT_SQL = """
WITH live AS (
    SELECT *
    FROM msg_index
    WHERE is_deleted = 0
      AND folder_path = ?
      AND file_key = ?
      AND (caption_raw IS NULL OR caption_raw NOT LIKE '%"kind":"tgccm_batch_blob"%')
      AND (caption_raw IS NULL OR caption_raw NOT LIKE '%"t":"tgccm_batch_blob"%')
),
dedup AS (
    SELECT *
    FROM (
        SELECT
            msg_id,
            chat_id,
            folder_path,
            file_key,
            part_index,
            parts_total,
            orig_name,
            name_pinned,
            file_size,
            date_ts,
            ROW_NUMBER() OVER (
                PARTITION BY folder_path, file_key, part_index
                ORDER BY date_ts DESC, msg_id DESC, chat_id DESC
            ) AS rn
        FROM live
    )
    WHERE rn = 1
),
name_pick AS (
    SELECT folder_path, file_key, orig_name
    FROM (
        SELECT
            folder_path,
            file_key,
            orig_name,
            name_pinned,
            date_ts,
            msg_id,
            chat_id,
            ROW_NUMBER() OVER (
                PARTITION BY folder_path, file_key
                ORDER BY name_pinned DESC, date_ts DESC, msg_id DESC, chat_id DESC
            ) AS rn
        FROM dedup
    )
    WHERE rn = 1
),
agg AS (
    SELECT
        folder_path,
        file_key,
        MAX(parts_total) AS parts_total,
        COUNT(DISTINCT part_index) AS have_parts,
        CASE
            WHEN SUM(CASE WHEN file_size IS NULL THEN 1 ELSE 0 END) > 0 THEN NULL
            ELSE SUM(file_size)
        END AS total_size,
        MAX(date_ts) AS last_seen_ts
    FROM dedup
    GROUP BY folder_path, file_key
)
INSERT INTO objects(
    file_key, folder_path, orig_name,
    parts_total, have_parts, status, total_size, last_seen_ts
)
SELECT
    agg.file_key,
    agg.folder_path,
    name_pick.orig_name,
    agg.parts_total,
    agg.have_parts,
    CASE WHEN agg.have_parts = agg.parts_total THEN 'complete' ELSE 'incomplete' END AS status,
    agg.total_size,
    agg.last_seen_ts
FROM agg
JOIN name_pick
  ON name_pick.folder_path = agg.folder_path
 AND name_pick.file_key = agg.file_key;
"""

_REBUILD_ALL_OBJECTS_SQL = """
WITH live AS (
    SELECT *
    FROM msg_index
    WHERE is_deleted = 0
      AND (caption_raw IS NULL OR caption_raw NOT LIKE '%"kind":"tgccm_batch_blob"%')
      AND (caption_raw IS NULL OR caption_raw NOT LIKE '%"t":"tgccm_batch_blob"%')
),
dedup AS (
    SELECT *
    FROM (
        SELECT
            msg_id,
            chat_id,
            folder_path,
            file_key,
            part_index,
            parts_total,
            orig_name,
            name_pinned,
            file_size,
            date_ts,
            ROW_NUMBER() OVER (
                PARTITION BY folder_path, file_key, part_index
                ORDER BY date_ts DESC, msg_id DESC, chat_id DESC
            ) AS rn
        FROM live
    )
    WHERE rn = 1
),
name_pick AS (
    SELECT folder_path, file_key, orig_name
    FROM (
        SELECT
            folder_path,
            file_key,
            orig_name,
            name_pinned,
            date_ts,
            msg_id,
            chat_id,
            ROW_NUMBER() OVER (
                PARTITION BY folder_path, file_key
                ORDER BY name_pinned DESC, date_ts DESC, msg_id DESC, chat_id DESC
            ) AS rn
        FROM dedup
    )
    WHERE rn = 1
),
agg AS (
    SELECT
        folder_path,
        file_key,
        MAX(parts_total) AS parts_total,
        COUNT(DISTINCT part_index) AS have_parts,
        CASE
            WHEN SUM(CASE WHEN file_size IS NULL THEN 1 ELSE 0 END) > 0 THEN NULL
            ELSE SUM(file_size)
        END AS total_size,
        MAX(date_ts) AS last_seen_ts
    FROM dedup
    GROUP BY folder_path, file_key
)
INSERT INTO objects(
    file_key, folder_path, orig_name,
    parts_total, have_parts, status, total_size, last_seen_ts
)
SELECT
    agg.file_key,
    agg.folder_path,
    name_pick.orig_name,
    agg.parts_total,
    agg.have_parts,
    CASE WHEN agg.have_parts = agg.parts_total THEN 'complete' ELSE 'incomplete' END AS status,
    agg.total_size,
    agg.last_seen_ts
FROM agg
JOIN name_pick
  ON name_pick.folder_path = agg.folder_path
 AND name_pick.file_key = agg.file_key;
"""
