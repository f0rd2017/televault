from __future__ import annotations

from app.tg.upload import _AdaptiveUploadController


def test_adaptive_upload_controller_ignores_short_low_speed_spikes_when_ema_is_high() -> None:
    controller = _AdaptiveUploadController(
        initial_concurrency=8,
        max_concurrency=8,
        is_premium=True,
        min_concurrency=1,
    )
    controller._last_downscale_ts = -10_000.0

    for _ in range(6):
        controller.record_sample(
            sent_bytes=4 * 1024 * 1024,
            elapsed_seconds=0.20,
        )

    before = int(controller.summary()["final_concurrency"])
    assert before == 8

    for _ in range(3):
        controller.record_sample(
            sent_bytes=512 * 1024,
            elapsed_seconds=1.10,
        )

    after = int(controller.summary()["final_concurrency"])
    assert after == before


def test_adaptive_upload_controller_downscales_when_sustained_speed_is_low() -> None:
    controller = _AdaptiveUploadController(
        initial_concurrency=6,
        max_concurrency=6,
        is_premium=True,
        min_concurrency=1,
    )
    controller._last_downscale_ts = -10_000.0

    for _ in range(3):
        controller.record_sample(
            sent_bytes=256 * 1024,
            elapsed_seconds=1.20,
        )

    assert int(controller.summary()["final_concurrency"]) == 5

