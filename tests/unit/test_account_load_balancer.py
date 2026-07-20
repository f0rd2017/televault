from __future__ import annotations

from televault.core.types import AppConfig
from televault.tg.upload.uploader import TgUploader

MB = 1024 * 1024


def _uploader(n_clients: int) -> TgUploader:
    cfg = AppConfig(tg_api_id=1, tg_api_hash="x", tg_session_path="s", cache_dir="/tmp")
    main = object()
    extras = [object() for _ in range(n_clients - 1)]
    return TgUploader(
        cfg, object(), main, chat=object(), chat_id="1", extra_clients=extras
    )


def test_single_client_pool_always_offset_zero() -> None:
    up = _uploader(1)
    assert up._reserve_pool_account() == 0
    assert up._reserve_pool_account() == 0


def test_reserve_samples_all_accounts_before_repeating() -> None:
    up = _uploader(3)
    # Nothing released yet → 3 distinct accounts get sampled (reservation avoids
    # piling onto an already-busy account while a free one exists).
    offsets = [up._reserve_pool_account() for _ in range(3)]
    assert sorted(offsets) == [0, 1, 2]


def test_reserve_prefers_fastest_free_account_by_measured_speed() -> None:
    up = _uploader(3)
    # Sample all three, then release with very different throughput.
    for _ in range(3):
        up._reserve_pool_account()
    up._release_pool_account(0, bytes_uploaded=100 * MB, elapsed=1.0)  # ~100 MB/s
    up._release_pool_account(1, bytes_uploaded=1 * MB, elapsed=1.0)  # ~1 MB/s
    up._release_pool_account(2, bytes_uploaded=1 * MB, elapsed=1.0)  # ~1 MB/s

    # All free now → fastest (account 0) is chosen.
    first = up._reserve_pool_account()
    assert first == 0
    # Account 0 is now busy → next goes to a free slower one, not back to 0.
    second = up._reserve_pool_account()
    assert second in (1, 2)
    assert second != 0


def test_release_is_safe_for_out_of_range_offset() -> None:
    up = _uploader(2)
    # Should not raise.
    up._release_pool_account(99, bytes_uploaded=10 * MB, elapsed=1.0)
    up._release_pool_account(-1, bytes_uploaded=10 * MB, elapsed=1.0)
