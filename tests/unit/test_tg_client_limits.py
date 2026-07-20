from __future__ import annotations

from types import SimpleNamespace

from televault.tg.client import _build_transfer_limits


def test_build_transfer_limits_regular_account() -> None:
    cfg = SimpleNamespace(
        upload_max_fileparts_default=4000, upload_max_fileparts_premium=8000
    )
    limits = _build_transfer_limits(cfg, is_premium=False)

    assert limits.is_premium is False
    assert limits.request_size_bytes == 524288
    assert limits.max_fileparts == 4000
    assert limits.max_file_size_bytes == 4000 * 524288


def test_build_transfer_limits_premium_account() -> None:
    cfg = SimpleNamespace(
        upload_max_fileparts_default=4000, upload_max_fileparts_premium=8000
    )
    limits = _build_transfer_limits(cfg, is_premium=True)

    assert limits.is_premium is True
    assert limits.max_fileparts == 8000
    assert limits.max_file_size_bytes == 8000 * 524288


def test_build_transfer_limits_fallback_values() -> None:
    limits = _build_transfer_limits(None, is_premium=False)

    assert limits.max_fileparts == 4000
    assert limits.max_file_size_bytes == 4000 * 524288
