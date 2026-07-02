from __future__ import annotations

import asyncio

import pytest

from app.core.transfer_progress import TransferProgressAggregator


@pytest.mark.asyncio
async def test_progress_aggregator_monotonic_and_final_emit() -> None:
    events: list[tuple[float, str]] = []

    async def dispatch(percent: float, message: str) -> None:
        events.append((percent, message))

    agg = TransferProgressAggregator(
        total_parts=2,
        total_bytes_hint=1000,
        emit_interval_ms=40,
        percent_step=2.0,
        activity="Downloading",
    )
    await agg.start(dispatch)

    agg.on_part_progress(0, 120, 500)
    agg.on_part_progress(0, 200, 500)
    agg.on_part_progress(1, 160, 500)
    await asyncio.sleep(0.09)

    agg.on_part_progress(0, 360, 500)
    agg.on_part_progress(1, 400, 500)
    await asyncio.sleep(0.09)

    await agg.stop("Download complete")

    assert events
    percents = [value for value, _ in events]
    assert percents == sorted(percents)
    assert percents[-1] == pytest.approx(100.0)
    assert events[-1][1] == "Download complete"


@pytest.mark.asyncio
async def test_progress_aggregator_coalesces_small_updates() -> None:
    events: list[tuple[float, str]] = []

    async def dispatch(percent: float, message: str) -> None:
        events.append((percent, message))

    agg = TransferProgressAggregator(
        total_parts=1,
        total_bytes_hint=1000,
        emit_interval_ms=50,
        percent_step=5.0,
        activity="Uploading",
    )
    await agg.start(dispatch)

    for value in range(10, 120, 10):
        agg.on_part_progress(0, value, 1000)
    await asyncio.sleep(0.07)
    await agg.stop("Upload complete")

    assert events
    assert events[-1][0] == pytest.approx(100.0)
    # Updates were tiny and frequent; emitter should keep event count bounded.
    assert len(events) <= 5


@pytest.mark.asyncio
async def test_progress_aggregator_reports_speed_in_human_units() -> None:
    events: list[tuple[float, str]] = []

    async def dispatch(percent: float, message: str) -> None:
        events.append((percent, message))

    agg = TransferProgressAggregator(
        total_parts=1,
        total_bytes_hint=10 * 1024 * 1024,
        emit_interval_ms=40,
        percent_step=1.0,
        activity="Uploading",
    )
    await agg.start(dispatch)

    agg.on_part_progress(0, 3 * 1024 * 1024, 10 * 1024 * 1024)
    await asyncio.sleep(0.08)
    agg.on_part_progress(0, 8 * 1024 * 1024, 10 * 1024 * 1024)
    await asyncio.sleep(0.08)
    await agg.stop("Upload complete")

    speed_messages = [
        msg for _pct, msg in events if "|" in msg and msg != "Upload complete"
    ]
    assert speed_messages
    assert any(message.endswith("/s") for message in speed_messages)


@pytest.mark.asyncio
async def test_progress_reports_wire_and_effective_when_compressed() -> None:
    events: list[tuple[float, str]] = []

    async def dispatch(percent: float, message: str) -> None:
        events.append((percent, message))

    # Payload (wire) = 10 MiB, source (original) = 20 MiB → 2:1 compression.
    agg = TransferProgressAggregator(
        total_parts=1,
        total_bytes_hint=10 * 1024 * 1024,
        emit_interval_ms=40,
        percent_step=1.0,
        activity="Uploading",
        source_bytes_hint=20 * 1024 * 1024,
    )
    await agg.start(dispatch)

    agg.on_part_progress(0, 3 * 1024 * 1024, 10 * 1024 * 1024)
    await asyncio.sleep(0.08)
    agg.on_part_progress(0, 8 * 1024 * 1024, 10 * 1024 * 1024)
    await asyncio.sleep(0.08)
    await agg.stop("Upload complete")

    dual = [msg for _pct, msg in events if "wire" in msg and "eff" in msg]
    assert dual, f"expected wire|eff metrics, got: {[m for _p, m in events]}"

    # Effective MB/s must be ~2x the wire MB/s (compression ratio).
    sample = dual[-1]
    wire_val = float(sample.split("wire")[0].rsplit("|", 1)[-1].strip().split()[0])
    eff_val = float(sample.split("|")[-1].strip().split()[0])
    assert eff_val == pytest.approx(wire_val * 2.0, rel=0.05)


@pytest.mark.asyncio
async def test_progress_shows_single_metric_without_compression() -> None:
    events: list[tuple[float, str]] = []

    async def dispatch(percent: float, message: str) -> None:
        events.append((percent, message))

    # No source hint → effective == wire, so only one speed metric is shown.
    agg = TransferProgressAggregator(
        total_parts=1,
        total_bytes_hint=10 * 1024 * 1024,
        emit_interval_ms=40,
        percent_step=1.0,
        activity="Downloading",
    )
    await agg.start(dispatch)

    agg.on_part_progress(0, 5 * 1024 * 1024, 10 * 1024 * 1024)
    await asyncio.sleep(0.08)
    await agg.stop("Download complete")

    speed_messages = [
        msg for _pct, msg in events if "|" in msg and msg != "Download complete"
    ]
    assert speed_messages
    assert all("wire" not in msg and "eff" not in msg for msg in speed_messages)
