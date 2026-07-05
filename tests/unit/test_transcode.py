"""Transcode/remux of non-native formats during streaming.

Pure planning (ffprobe JSON → plan → ffmpeg args) — no ffmpeg;
an end-to-end test (AVI → fragmented MP4 via a real socket) — skipped
if ffmpeg/ffprobe are unavailable.
"""

from __future__ import annotations

import os
import subprocess
import urllib.request
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from app.api.common import TranscodeResponse
from app.api.server import ApiContext, ApiServer, dispatch
from app.core.transcode import (
    build_ffmpeg_args,
    plan_from_probe,
    transcode_available,
)
from app.core.types import ApiConfig, PartMeta, PartRecord
from app.db.database import connect_db
from app.db.repo import DbRepo
from app.tg.parser import build_caption

# ── Planning (pure) ──────────────────────────────────────────────────────────


def _probe(video: str | None, audio: str | None) -> dict:
    streams = []
    if video:
        streams.append({"codec_type": "video", "codec_name": video})
    if audio:
        streams.append({"codec_type": "audio", "codec_name": audio})
    return {"streams": streams}


def test_plan_h264_aac_is_remux_only() -> None:
    plan = plan_from_probe(_probe("h264", "aac"))
    assert plan.copy_video and plan.copy_audio
    assert plan.is_remux_only


def test_plan_foreign_codecs_transcode_both() -> None:
    plan = plan_from_probe(_probe("wmv3", "wmav2"))
    assert not plan.copy_video and not plan.copy_audio
    assert not plan.is_remux_only


def test_plan_mixed_copies_only_compatible() -> None:
    plan = plan_from_probe(_probe("mpeg4", "mp3"))
    assert not plan.copy_video
    assert plan.copy_audio


def test_plan_handles_missing_streams() -> None:
    audio_only = plan_from_probe(_probe(None, "aac"))
    assert audio_only.video_codec is None
    assert audio_only.copy_audio
    empty = plan_from_probe({})
    assert empty.video_codec is None and empty.audio_codec is None


def test_build_args_copy_vs_transcode() -> None:
    url = "http://127.0.0.1:1/share/x"
    copy_args = build_ffmpeg_args(url, plan_from_probe(_probe("h264", "aac")))
    assert url in copy_args
    assert ["-c:v", "copy"] == copy_args[
        copy_args.index("-c:v") : copy_args.index("-c:v") + 2
    ]
    assert copy_args[-1] == "-"  # stdout
    assert "frag_keyframe+empty_moov+default_base_moof" in " ".join(copy_args)

    tc_args = build_ffmpeg_args(url, plan_from_probe(_probe("wmv3", "wmav2")))
    assert "libx264" in tc_args
    assert "aac" in tc_args

    audio_only = build_ffmpeg_args(url, plan_from_probe(_probe(None, "aac")))
    assert "-c:v" not in audio_only


# ── Routing (dispatch, no sockets) ───────────────────────────────────────────


def _caption(folder: str, key: str, idx: int, total: int, name: str) -> str:
    meta = PartMeta(
        folder_path=folder,
        file_key=key,
        part_index=idx,
        parts_total=total,
        orig_name=name,
    )
    return build_caption(meta, extra={"enc": False})


def _part(
    folder: str, key: str, idx: int, total: int, name: str, file_size: int
) -> PartRecord:
    return PartRecord(
        msg_id=1000 + idx,
        chat_id="chat",
        folder_path=folder,
        file_key=key,
        part_index=idx,
        parts_total=total,
        orig_name=name,
        file_size=file_size,
        caption_raw=_caption(folder, key, idx, total, name),
        date_ts=100 + idx,
    )


def _make_ctx(tmp_path, content_size: int, *, token: str = "") -> ApiContext:
    repo = DbRepo(connect_db(tmp_path / "idx.sqlite3"))
    repo.upsert_msg_parts_bulk([_part("Vids", "k1", 0, 1, "movie.avi", content_size)])
    repo.create_share("vid", "Vids", "k1", "movie.avi", total_size=content_size)
    config = SimpleNamespace(
        cache_dir=str(tmp_path / "cache"),
        download_root=str(tmp_path / "dl"),
        caption_prefix="FC1|",
        api=ApiConfig(enabled=True, host="127.0.0.1", port=0, token=token),
    )
    return ApiContext(
        repo=repo,
        worker=None,
        token=token,
        config=config,
        share_dir=str(tmp_path / "share"),
    )


def test_share_transcode_param_returns_transcode_response(tmp_path) -> None:
    ctx = _make_ctx(tmp_path, 100)
    result = dispatch(ctx, "GET", "/share/vid", {"transcode": ["1"]}, {}, b"")
    assert isinstance(result, TranscodeResponse)
    assert result.input_path == "/share/vid"
    assert result.input_query == {}  # without a password the query is empty
    assert result.filename == "movie.avi"


def test_media_transcode_param_carries_token(tmp_path) -> None:
    ctx = _make_ctx(tmp_path, 100, token="sekret")
    result = dispatch(
        ctx,
        "GET",
        "/api/media",
        {
            "folder": ["Vids"],
            "file_key": ["k1"],
            "transcode": ["1"],
            "token": ["sekret"],
        },
        {},
        b"",
    )
    assert isinstance(result, TranscodeResponse)
    assert result.input_path == "/api/media"
    assert result.input_query["token"] == "sekret"
    assert result.input_query["folder"] == "Vids"


# ── End-to-end: AVI → fMP4 via a real socket ─────────────────────────────────


class _SliceWorker:
    """Serves the requested parts, slicing prepared byte content (like in
    test_stream.FakeStreamWorker, but without tracking stats)."""

    def __init__(self, content: bytes, part_size: int) -> None:
        self.content = content
        self.part_size = part_size

    def fetch_stream_parts_blocking(
        self,
        folder,
        file_key,
        part_indices,
        cache_dir,
        *,
        timeout=600.0,
        prefix_bytes=None,
    ) -> dict[int, str]:
        cache = Path(cache_dir)
        cache.mkdir(parents=True, exist_ok=True)
        out: dict[int, str] = {}
        for idx in part_indices:
            p = cache / f"part_{int(idx):08d}.bin"
            if not p.exists():
                lo = int(idx) * self.part_size
                p.write_bytes(self.content[lo : lo + self.part_size])
            out[int(idx)] = str(p)
        return out


def _make_test_avi(path: Path) -> bytes:
    subprocess.run(  # noqa: S603
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=160x120:rate=10",
            "-c:v",
            "mpeg4",
            "-y",
            str(path),
        ],
        check=True,
        timeout=60,
    )
    return path.read_bytes()


@pytest.mark.skipif(not transcode_available(), reason="ffmpeg/ffprobe not in PATH")
def test_real_http_transcode_avi_to_fmp4(tmp_path) -> None:
    avi = _make_test_avi(tmp_path / "src.avi")
    ctx = _make_ctx(tmp_path, len(avi))
    worker = _SliceWorker(avi, len(avi))
    ctx.worker = worker

    server = ApiServer(config=ctx.config, repo=ctx.repo, worker=worker)
    server._ctx = ctx  # noqa: SLF001
    assert server.start()
    try:
        host, port = server.address

        # Native serving stays as before: AVI bytes over Range.
        with urllib.request.urlopen(
            f"http://{host}:{port}/share/vid", timeout=15
        ) as resp:
            assert resp.read() == avi

        # Transcode: the output is fragmented MP4 (ftyp at the start), not AVI.
        with urllib.request.urlopen(
            f"http://{host}:{port}/share/vid?transcode=1", timeout=60
        ) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "video/mp4"
            body = resp.read()
        assert body[4:8] == b"ftyp"
        assert b"moof" in body  # fragmented: has fragments, plays from a pipe
        assert not body.startswith(b"RIFF")
    finally:
        server.stop()
