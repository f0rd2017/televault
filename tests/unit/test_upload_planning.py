from __future__ import annotations

from app.tg.partition import (
    base_logical_parts,
    build_even_part_sizes,
    plan_logical_parts,
    rebalance_multi_client_parts,
)


def test_upload_planning_rebalances_medium_multi_account_file_into_second_wave() -> (
    None
):
    gib = 1024 * 1024 * 1024
    total_size = 195 * 1024 * 1024

    parts = rebalance_multi_client_parts(
        total_size=total_size,
        planned_parts=plan_logical_parts(
            total_size=total_size,
            base_parts=3,
            part_limit_bytes=gib,
        ),
        pool_size=3,
        part_limit_bytes=gib,
    )

    assert parts == 6
    sizes = build_even_part_sizes(total_size, parts)
    assert sum(sizes.values()) == total_size
    assert max(sizes.values()) <= gib


def test_upload_planning_doubles_parts_when_per_client_chunk_exceeds_1gib() -> None:
    gib = 1024 * 1024 * 1024
    total_size = (gib * 4) + (256 * 1024 * 1024)

    parts = plan_logical_parts(
        total_size=total_size,
        base_parts=3,
        part_limit_bytes=gib,
    )

    assert parts == 6
    sizes = build_even_part_sizes(total_size, parts)
    assert sum(sizes.values()) == total_size
    assert max(sizes.values()) <= gib


def test_upload_planning_can_double_twice_for_very_large_file() -> None:
    gib = 1024 * 1024 * 1024
    total_size = (gib * 10) + (256 * 1024 * 1024)

    parts = plan_logical_parts(
        total_size=total_size,
        base_parts=3,
        part_limit_bytes=gib,
    )

    assert parts == 12
    sizes = build_even_part_sizes(total_size, parts)
    assert sum(sizes.values()) == total_size
    assert max(sizes.values()) <= gib


def test_upload_planning_single_account_keeps_small_file_single_part() -> None:
    gib = 1024 * 1024 * 1024

    parts = plan_logical_parts(
        total_size=700 * 1024 * 1024,
        base_parts=1,
        part_limit_bytes=gib,
    )

    assert parts == 1


def test_upload_planning_multi_account_keeps_small_file_single_part() -> None:
    base_parts = base_logical_parts(
        total_size=2 * 1024 * 1024,
        pool_size=3,
    )
    parts = plan_logical_parts(
        total_size=2 * 1024 * 1024,
        base_parts=base_parts,
        part_limit_bytes=1024 * 1024 * 1024,
    )

    assert base_parts == 1
    assert parts == 1


def test_upload_planning_keeps_sub_100mb_file_whole_for_file_level_parallelism() -> (
    None
):
    # A 50MB file must NOT be striped across accounts — it goes whole to one
    # (rotating) account so many files upload in parallel one-per-account.
    assert base_logical_parts(total_size=50 * 1024 * 1024, pool_size=3) == 1
    assert base_logical_parts(total_size=99 * 1024 * 1024, pool_size=3) == 1


def test_upload_planning_stripes_files_over_100mb_across_accounts() -> None:
    # Above the 100MB rule the file is split across the whole account pool.
    assert base_logical_parts(total_size=150 * 1024 * 1024, pool_size=3) == 3
    assert base_logical_parts(total_size=150 * 1024 * 1024, pool_size=2) == 2


def test_upload_planning_large_multi_account_file_can_add_third_wave() -> None:
    gib = 1024 * 1024 * 1024
    total_size = 428 * 1024 * 1024

    parts = rebalance_multi_client_parts(
        total_size=total_size,
        planned_parts=plan_logical_parts(
            total_size=total_size,
            base_parts=3,
            part_limit_bytes=gib,
        ),
        pool_size=3,
        part_limit_bytes=gib,
    )

    assert parts == 9


def test_upload_planning_single_account_splits_large_file_by_two() -> None:
    gib = 1024 * 1024 * 1024

    parts = plan_logical_parts(
        total_size=(gib + (512 * 1024 * 1024)),
        base_parts=1,
        part_limit_bytes=gib,
    )

    assert parts == 2
