"""Transcode/remux non-native formats during streaming (last roadmap item).

A non-native container/codec (AVI/XviD, WMV, FLV, MPEG-PS…) won't play at all
in the share-link recipient's browser, and not on every QMediaPlayer backend
either. Instead of downloading the whole file: ffmpeg repacks the stream on
the fly into **fragmented MP4** (playable by both the browser and
QMediaPlayer), reading the source over HTTP from our own stream server
(Range already works — ffmpeg seeks on its own using the container's index).

The strategy is decided by ffprobe based on the actual codecs, not the file
extension:
- h264/hevc/av1 video and aac/mp3 audio → ``-c copy`` (a cheap remux, no CPU cost);
- everything else → libx264 (veryfast) / aac.

v1 deliberately has no seeking: the output is a chunked stream of unknown
length (``Connection: close``), Range isn't supported. That's enough for
"watch this AVI via the link"; seeking goes through the native path.

Pure planning (``plan_from_probe``/``build_ffmpeg_args``) is kept separate
from the subprocess plumbing (``probe_media``) — so it can be tested without ffmpeg.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

# Codecs that are valid inside MP4 and play in browsers — we don't re-encode these.
MP4_COPY_VIDEO_CODECS = {"h264", "hevc", "av1"}
MP4_COPY_AUDIO_CODECS = {"aac", "mp3"}

_PROBE_TIMEOUT_SEC = 25.0


@dataclass(frozen=True)
class TranscodePlan:
    """What to do with the source streams when packaging into fragmented MP4."""

    video_codec: str | None  # None — there's no video stream
    audio_codec: str | None  # None — there's no audio stream
    copy_video: bool
    copy_audio: bool

    @property
    def is_remux_only(self) -> bool:
        """Just repackaging the container, with no re-encoding (no CPU load)."""
        return (self.video_codec is None or self.copy_video) and (
            self.audio_codec is None or self.copy_audio
        )


def transcode_available() -> bool:
    """Whether ffmpeg+ffprobe are both on PATH (the pipeline needs both)."""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def plan_from_probe(probe: dict) -> TranscodePlan:
    """Build a plan from ffprobe's JSON output (``-show_streams``).

    We take the first video stream and the first audio stream (the typical
    media file case); an unrecognized codec means re-encode (the safe default).
    """
    streams = probe.get("streams") or []
    video = next((s for s in streams if str(s.get("codec_type")) == "video"), None)
    audio = next((s for s in streams if str(s.get("codec_type")) == "audio"), None)
    video_codec = str(video.get("codec_name") or "") if video else None
    audio_codec = str(audio.get("codec_name") or "") if audio else None
    return TranscodePlan(
        video_codec=video_codec or None,
        audio_codec=audio_codec or None,
        copy_video=bool(video_codec and video_codec in MP4_COPY_VIDEO_CODECS),
        copy_audio=bool(audio_codec and audio_codec in MP4_COPY_AUDIO_CODECS),
    )


def build_ffmpeg_args(input_url: str, plan: TranscodePlan) -> list[str]:
    """Build the ffmpeg args: input over a URL, output — fragmented MP4 to stdout.

    ``frag_keyframe+empty_moov`` — writes moov right away, then fragments: the
    stream can be played from the first byte without waiting for the end (a
    regular MP4 writes moov at the end and doesn't work for piping).
    """
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        input_url,
    ]
    if plan.video_codec is not None:
        if plan.copy_video:
            args += ["-c:v", "copy"]
        else:
            args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    if plan.audio_codec is not None:
        if plan.copy_audio:
            args += ["-c:a", "copy"]
        else:
            args += ["-c:a", "aac", "-b:a", "192k"]
    args += [
        "-f",
        "mp4",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-",
    ]
    return args


def probe_media(input_url: str) -> dict | None:
    """Run ffprobe on a URL → parsed JSON, or None (file unreadable/no ffprobe).

    This blocks the calling thread (the HTTP handler) — that's intentional,
    since the server model is threaded (ThreadingHTTPServer)."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 — fixed binary, our own URL
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                input_url,
            ],
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffprobe failed for transcode input: %s", exc)
        return None
    if completed.returncode != 0:
        logger.warning(
            "ffprobe returned %d for transcode input: %s",
            completed.returncode,
            (completed.stderr or b"")[:200].decode("utf-8", "replace"),
        )
        return None
    try:
        data = json.loads(completed.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
