"""Математика разбиения файла на логические части для загрузки.

Чистые функции без состояния: сколько частей планировать, как ровно разложить
байты, как ребалансировать между несколькими аккаунтами, сколько внутренних
воркеров давать части. Вынесено из ``TgUploader`` для читаемости и точечного
тестирования.
"""

from __future__ import annotations

import math

UPLOAD_TARGET_PART_MAX_BYTES = 1024 * 1024 * 1024
# Stripe a SINGLE file across accounts only when it's big enough to win from it.
# Below this, the file goes whole to ONE (rotating) account, so many files upload
# in parallel one-per-account (file-level parallelism) — far faster than splitting
# every small file across all accounts and gating it on the slowest (proxied) one.
MULTI_CLIENT_SHARD_MIN_BYTES = 100 * 1024 * 1024
MULTI_CLIENT_BALANCE_TARGET_PART_BYTES = 64 * 1024 * 1024
MULTI_CLIENT_BALANCE_MIN_PART_BYTES = 8 * 1024 * 1024
DIRECT_PARALLEL_BIGFILE_MIN_BYTES = 8 * 1024 * 1024
MEDIUM_PART_INNER_UPLOAD_WORKERS = 2
LARGE_PART_INNER_UPLOAD_MIN_BYTES = 32 * 1024 * 1024
LARGE_PART_INNER_UPLOAD_WORKERS = 4


def logical_part_limit_bytes(safe_limit_bytes: int) -> int:
    return max(1, min(int(safe_limit_bytes), int(UPLOAD_TARGET_PART_MAX_BYTES)))


def plan_logical_parts(
    *,
    total_size: int,
    base_parts: int,
    part_limit_bytes: int,
) -> int:
    total = max(0, int(total_size))
    if total <= 0:
        return 1
    parts = max(1, int(base_parts))
    parts = min(parts, total)
    limit = max(1, int(part_limit_bytes))
    while math.ceil(float(total) / float(parts)) > limit:
        next_parts = min(total, parts * 2)
        if next_parts == parts:
            break
        parts = next_parts
    return max(1, int(parts))


def build_even_part_sizes(total_size: int, parts_total: int) -> dict[int, int]:
    total = max(0, int(total_size))
    if total <= 0:
        return {0: 0}
    parts = max(1, min(int(parts_total), total))
    base = total // parts
    remainder = total % parts
    return {idx: int(base + (1 if idx < remainder else 0)) for idx in range(parts)}


def base_logical_parts(
    *, total_size: int, pool_size: int, shard_min_bytes: int | None = None
) -> int:
    if int(pool_size) <= 1:
        return 1
    threshold = (
        int(shard_min_bytes)
        if shard_min_bytes is not None
        else int(MULTI_CLIENT_SHARD_MIN_BYTES)
    )
    if int(total_size) <= threshold:
        return 1
    return max(1, int(pool_size))


def default_inner_upload_workers(payload_size: int) -> int:
    size = max(0, int(payload_size))
    if size >= int(DIRECT_PARALLEL_BIGFILE_MIN_BYTES):
        workers = int(MEDIUM_PART_INNER_UPLOAD_WORKERS)
    else:
        workers = 1
    if size < int(LARGE_PART_INNER_UPLOAD_MIN_BYTES):
        return workers
    return int(max(workers, LARGE_PART_INNER_UPLOAD_WORKERS))


def rebalance_multi_client_parts(
    *,
    total_size: int,
    planned_parts: int,
    pool_size: int,
    part_limit_bytes: int,
) -> int:
    total = max(0, int(total_size))
    parts = max(1, int(planned_parts))
    pool = max(1, int(pool_size))
    if total <= 0 or pool <= 1 or parts < pool:
        return parts

    target = max(
        int(MULTI_CLIENT_BALANCE_MIN_PART_BYTES),
        min(int(part_limit_bytes), int(MULTI_CLIENT_BALANCE_TARGET_PART_BYTES)),
    )
    while math.ceil(float(total) / float(parts)) > target:
        next_parts = parts + pool
        if next_parts <= parts:
            break
        next_avg = math.ceil(float(total) / float(next_parts))
        if next_avg < int(MULTI_CLIENT_BALANCE_MIN_PART_BYTES):
            break
        parts = next_parts
    return int(parts)
