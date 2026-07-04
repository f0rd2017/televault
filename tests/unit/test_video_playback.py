from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import functools
import http.server
import socketserver
import subprocess
import threading
import time
from pathlib import Path

import pytest

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication

from app.core.utils import ffmpeg_available
from app.ui.media_viewer import VideoViewerWindow, _format_time


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _write_test_video(path, *, duration: int = 2, size: str = "160x120") -> str:
    subprocess.run(  # noqa: S603
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size={size}:rate=10",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    return str(path)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


def _serve_dir(directory: Path):
    handler = functools.partial(_QuietHandler, directory=str(directory))
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _pump_until(predicate, timeout_ms: int = 15000) -> bool:
    _app()
    loop = QEventLoop()
    deadline = time.monotonic() + timeout_ms / 1000.0
    timer = QTimer()
    timer.setInterval(50)

    def _tick() -> None:
        if predicate() or time.monotonic() > deadline:
            loop.quit()

    timer.timeout.connect(_tick)
    timer.start()
    loop.exec()
    timer.stop()
    return predicate()


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")
def test_video_player_launches_and_plays(tmp_path):
    _app()
    _write_test_video(tmp_path / "clip.mp4")
    httpd = _serve_dir(tmp_path)
    win = None
    try:
        port = httpd.server_address[1]
        url = f"http://127.0.0.1:{port}/clip.mp4"
        win = VideoViewerWindow(url, "clip.mp4")
        errors: list[tuple] = []
        win._player.errorOccurred.connect(lambda e, s: errors.append((e, s)))
        win.show()

        playing = _pump_until(
            lambda: (
                win._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            )
        )
        assert playing, f"player did not start; errors={errors}"
        assert not errors, f"player reported error: {errors}"
        assert win._player.mediaStatus() in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferingMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        )
    finally:
        if win is not None:
            win.close()
        httpd.shutdown()


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")
def test_video_player_scrub_previews_without_seeking_until_release(tmp_path):
    _app()
    _write_test_video(tmp_path / "clip.mp4")
    httpd = _serve_dir(tmp_path)
    win = None
    try:
        port = httpd.server_address[1]
        url = f"http://127.0.0.1:{port}/clip.mp4"
        win = VideoViewerWindow(url, "clip.mp4")
        win.show()
        _pump_until(
            lambda: (
                win._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            )
        )

        seek_calls: list[int] = []
        win._player.setPosition = lambda pos: seek_calls.append(int(pos))

        # Клип двухсекундный (см. _write_test_video), диапазон слайдера ~0..2000мс.
        win._on_slider_pressed()
        for value in (300, 900, 1800):
            win._position.sliderMoved.emit(value)
        assert seek_calls == [], "dragging the slider must not seek on every move"
        assert _format_time(1800) in win._time_label.text()

        win._position.setValue(1800)
        win._position.sliderReleased.emit()
        assert seek_calls == [1800], "release should seek exactly once"
    finally:
        if win is not None:
            win.close()
        httpd.shutdown()


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")
def test_video_player_shows_buffering_indicator_on_stall(tmp_path):
    _app()
    _write_test_video(tmp_path / "clip.mp4")
    httpd = _serve_dir(tmp_path)
    win = None
    try:
        port = httpd.server_address[1]
        url = f"http://127.0.0.1:{port}/clip.mp4"
        win = VideoViewerWindow(url, "clip.mp4")
        win.show()
        _pump_until(
            lambda: (
                win._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            )
        )

        win._on_media_status(QMediaPlayer.MediaStatus.StalledMedia)
        assert win._buffering is True
        assert "buffering" in win._time_label.text()

        win._on_media_status(QMediaPlayer.MediaStatus.BufferedMedia)
        assert win._buffering is False
        assert "buffering" not in win._time_label.text()
    finally:
        if win is not None:
            win.close()
        httpd.shutdown()


def test_video_player_invalid_source_shows_fallback(tmp_path):
    _app()
    (tmp_path / "broken.mp4").write_bytes(b"not a real video, no moov atom here")
    httpd = _serve_dir(tmp_path)
    win = None
    try:
        port = httpd.server_address[1]
        url = f"http://127.0.0.1:{port}/broken.mp4"
        win = VideoViewerWindow(url, "broken.mp4")
        win.show()

        shown = _pump_until(lambda: win._fallback_btn.isVisible(), timeout_ms=10000)
        assert shown, "fallback button should appear on a broken source"
    finally:
        if win is not None:
            win.close()
        httpd.shutdown()


def test_video_player_logs_error_on_invalid_source(tmp_path, caplog):
    _app()
    (tmp_path / "broken.mp4").write_bytes(b"not a real video, no moov atom here")
    httpd = _serve_dir(tmp_path)
    win = None
    try:
        port = httpd.server_address[1]
        url = f"http://127.0.0.1:{port}/broken.mp4"
        with caplog.at_level("WARNING", logger="app.ui.media_viewer"):
            win = VideoViewerWindow(url, "broken.mp4")
            win.show()
            _pump_until(lambda: win._fallback_btn.isVisible(), timeout_ms=10000)
        assert any("broken.mp4" in rec.getMessage() for rec in caplog.records), (
            f"expected a WARNING log entry; got: {[r.getMessage() for r in caplog.records]}"
        )
    finally:
        if win is not None:
            win.close()
        httpd.shutdown()
