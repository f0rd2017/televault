"""Native video/photo viewer windows (replaces opening previews in a browser).

Video/photo open in a separate top-level Qt window and don't depend on a
browser:
  * video — QMediaPlayer + QAudioOutput + QVideoWidget (play/pause/seek/volume,
    keyboard shortcuts);
  * photo — QGraphicsView + QPixmap (wheel zoom, drag pan, double-click to
    fit).

The source is the same local streaming server (HTTP Range), so the file
isn't downloaded in full. If a native window can't be created (no
multimedia backend, etc.) it falls back to opening in the browser.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)
from PySide6.QtWidgets import (
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

# Keep references to open windows, otherwise the garbage collector would
# collect them and the window would close instantly right after show().
_OPEN_VIEWERS: set[QWidget] = set()


def _register(window: QWidget) -> None:
    _OPEN_VIEWERS.add(window)
    window.destroyed.connect(lambda *_: _OPEN_VIEWERS.discard(window))


def _format_time(ms: int) -> str:
    if ms <= 0:
        return "0:00"
    total = int(ms) // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# --------------------------------------------------------------------------- #
# Photo
# --------------------------------------------------------------------------- #
class _ImageView(QGraphicsView):
    """QGraphicsView with wheel zoom and drag panning."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item = None
        self._zoom = 0
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setBackgroundBrush(Qt.GlobalColor.black)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._scene.clear()
        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(self._pixmap_item.boundingRect())
        self._zoom = 0
        self.fit()

    def fit(self) -> None:
        if self._pixmap_item is None:
            return
        self.resetTransform()
        self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        self._zoom = 0

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._pixmap_item is None:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        if delta > 0:
            self._zoom += 1
            self.scale(1.25, 1.25)
        else:
            self._zoom -= 1
            if self._zoom <= 0:
                self.fit()
            else:
                self.scale(0.8, 0.8)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        if self._zoom == 0:
            self.fit()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.fit()


class ImageViewerWindow(QWidget):
    from PySide6.QtCore import Signal

    _ffmpeg_done = Signal(str, str, bool)

    def __init__(self, url: str, title: str) -> None:
        super().__init__()
        self._ffmpeg_done.connect(self._on_ffmpeg_done)
        self.setWindowTitle(title)
        self.resize(1000, 720)
        self._url = url
        self._title = title
        self._retries = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._view = _ImageView(self)
        layout.addWidget(self._view, 1)

        self._status = QLabel(self.tr("Loading…"), self)
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._status)

        self._nam = QNetworkAccessManager(self)
        self._reply = self._nam.get(QNetworkRequest(QUrl(url)))
        self._reply.finished.connect(self._on_finished)

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.close)

    def _on_finished(self) -> None:
        reply = self._reply
        self._reply = None
        if reply is None:
            return
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                if self._retries < 5:
                    self._retries += 1
                    self._status.setText(self.tr("Buffering…"))
                    from PySide6.QtCore import QTimer

                    QTimer.singleShot(1500, self._reload)
                    return
                try:
                    self._status.setText(
                        self.tr("Loading error: {0}").format(reply.errorString())
                    )
                except RuntimeError:
                    pass
                return
            raw = bytes(reply.readAll().data())
        finally:
            reply.deleteLater()

        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import QImageReader

        if not raw:
            self._show_open_external(self.tr("The file is empty."))
            return

        # Decode via QImageReader: this uses all available Qt format plugins
        # (png/webp/gif/bmp/ico/svg/tga/...) and honors EXIF orientation, so a
        # phone photo doesn't come out rotated.
        buffer = QBuffer(QByteArray(raw), self)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        image = reader.read()

        pixmap = QPixmap.fromImage(image) if not image.isNull() else QPixmap()
        if pixmap.isNull():
            # Last resort — parse the raw bytes directly via Qt.
            pixmap = QPixmap()
            pixmap.loadFromData(raw)

        if pixmap.isNull():
            # A format Qt can't decode itself (heic/avif/tiff/raw/psd, etc.) —
            # try converting to PNG via ffmpeg in the background.
            self._decode_via_ffmpeg_async(raw)
            return

        self._view.set_pixmap(pixmap)
        self._status.hide()

    def _reload(self) -> None:
        self._reply = self._nam.get(QNetworkRequest(QUrl(self._url)))
        self._reply.finished.connect(self._on_finished)

    def _decode_via_ffmpeg_async(self, raw: bytes) -> None:
        """Fallback for formats Qt can't decode itself (heic/avif/tiff/
        raw/psd, etc.): convert to PNG via ffmpeg in the background."""
        import threading

        self._status.setText(self.tr("Converting format..."))
        self._status.show()

        def _worker() -> None:
            import os
            import tempfile

            from app.core.utils import convert_image_to_png

            suffix = os.path.splitext(self._title or "")[1]
            src = ""
            out_png = ""
            success = False
            try:
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                    tf.write(raw)
                    src = tf.name
                out_png = src + ".png"
                if convert_image_to_png(src, out_png):
                    success = True
            except Exception:
                pass

            self._ffmpeg_done.emit(src, out_png, success)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_ffmpeg_done(self, src: str, out_png: str, success: bool) -> None:
        import os

        try:
            if success and os.path.exists(out_png):
                pixmap = QPixmap(out_png)
                if not pixmap.isNull():
                    self._view.set_pixmap(pixmap)
                    self._status.hide()
                else:
                    self._show_open_external(
                        self.tr("Failed to load the converted image.")
                    )
            else:
                self._show_open_external(
                    self.tr(
                        "This image format is not supported by the built-in viewer."
                    )
                )
        finally:
            for path in (src, out_png):
                if path and os.path.exists(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if hasattr(self, "_reply") and self._reply is not None:
            reply = self._reply
            self._reply = None
            reply.abort()
            reply.deleteLater()
        super().closeEvent(event)

    def _show_open_external(self, message: str) -> None:
        self._status.setText(message)
        self._status.show()
        btn = QPushButton(self.tr("Open in external application"), self)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)

        def _open_external() -> None:
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl(self._url))
            self.close()

        btn.clicked.connect(_open_external)
        self.layout().addWidget(btn)


# --------------------------------------------------------------------------- #
# Video
# --------------------------------------------------------------------------- #
class ClickableSlider(QSlider):
    """A slider that lets you click anywhere on it to jump there instantly."""

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            from PySide6.QtWidgets import QStyleOptionSlider

            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            val = self.style().sliderValueFromPosition(
                self.minimum(),
                self.maximum(),
                int(event.position().x())
                if self.orientation() == Qt.Orientation.Horizontal
                else int(event.position().y()),
                self.width()
                if self.orientation() == Qt.Orientation.Horizontal
                else self.height(),
                opt.upsideDown,
            )
            self.setValue(val)
            self.sliderMoved.emit(val)
        super().mousePressEvent(event)


class VideoViewerWindow(QWidget):
    def __init__(self, url: str, title: str) -> None:
        super().__init__()
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget

        self.setWindowTitle(title)
        self.resize(1080, 720)
        self.setStyleSheet("""
            QWidget { background-color: #0f0f0f; color: #ffffff; }
            QPushButton { 
                background: transparent; border: none; font-size: 18px; color: #f0f0f0; padding: 4px;
            }
            QPushButton:hover { color: #ff0050; }
            QSlider::groove:horizontal {
                border: none; height: 6px; background: #333333; border-radius: 3px;
            }
            QSlider::sub-page:horizontal { background: #ff0050; border-radius: 3px; }
            QSlider::handle:horizontal {
                background: #ffffff; width: 14px; margin-top: -4px; margin-bottom: -4px; border-radius: 7px;
            }
            QSlider::handle:horizontal:hover { background: #ff0050; }
            QLabel { font-size: 13px; color: #cccccc; }
            QComboBox {
                background: #222222; border: 1px solid #444; border-radius: 4px;
                padding: 2px 6px; color: #eeeeee; font-size: 13px;
            }
            QComboBox::drop-down { border: none; }
        """)

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._video = QVideoWidget(self)
        self._player.setVideoOutput(self._video)

        self._duration = 0
        self._seeking = False

        self._play_btn = QPushButton("⏸", self)
        self._play_btn.setFixedWidth(44)
        self._position = ClickableSlider(Qt.Orientation.Horizontal, self)
        self._position.setCursor(Qt.CursorShape.PointingHandCursor)
        self._time_label = QLabel("0:00 / 0:00", self)

        self._fallback_btn = QPushButton(self.tr("External player"), self)
        self._fallback_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._fallback_btn.setStyleSheet("color: #ffaa00; font-weight: bold;")
        self._fallback_btn.hide()

        def _open_external():
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl(self._url))
            self.close()

        self._fallback_btn.clicked.connect(_open_external)

        from PySide6.QtWidgets import QComboBox

        self._speed_box = QComboBox(self)
        self._speed_box.addItems(["0.5x", "1.0x", "1.25x", "1.5x", "2.0x"])
        self._speed_box.setCurrentText("1.0x")
        self._speed_box.setFixedWidth(65)
        self._speed_box.setCursor(Qt.CursorShape.PointingHandCursor)

        self._volume = ClickableSlider(Qt.Orientation.Horizontal, self)
        self._volume.setRange(0, 100)
        self._volume.setValue(80)
        self._volume.setFixedWidth(110)
        self._volume.setCursor(Qt.CursorShape.PointingHandCursor)
        self._audio.setVolume(0.8)

        controls = QHBoxLayout()
        controls.setContentsMargins(12, 10, 12, 10)
        controls.setSpacing(12)
        controls.addWidget(self._play_btn)
        controls.addWidget(self._position, 1)
        controls.addWidget(self._time_label)
        controls.addWidget(self._speed_box)
        controls.addWidget(QLabel("🔊", self))
        controls.addWidget(self._volume)
        controls.addWidget(self._fallback_btn)

        bar = QWidget(self)
        bar.setStyleSheet("background-color: #181818;")
        bar.setLayout(controls)
        self._controls_layout = controls

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._video, 1)
        layout.addWidget(bar)

        self._video.installEventFilter(self)

        self._play_btn.clicked.connect(self._toggle)
        # sliderMoved fires on EVERY mouse move while dragging — if we hooked
        # player.setPosition directly to it, scrubbing would turn into a flood
        # of network Range requests to the streaming server (one per pixel of
        # drag), causing lag/hangs while scrubbing. While the handle is being
        # dragged we only preview the time in the label; the real seek happens
        # once, on button release.
        self._position.sliderMoved.connect(self._on_slider_preview)
        self._position.sliderPressed.connect(self._on_slider_pressed)
        self._position.sliderReleased.connect(self._on_slider_released)
        self._speed_box.currentTextChanged.connect(self._on_speed_changed)
        self._volume.valueChanged.connect(lambda v: self._audio.setVolume(v / 100.0))
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_error)

        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self._toggle)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.close)
        QShortcut(
            QKeySequence(Qt.Key.Key_Right),
            self,
            activated=lambda: self._seek_relative(5000),
        )
        QShortcut(
            QKeySequence(Qt.Key.Key_Left),
            self,
            activated=lambda: self._seek_relative(-5000),
        )
        QShortcut(
            QKeySequence(Qt.Key.Key_F),
            self,
            activated=self._toggle_fullscreen,
        )

        self._url = url
        self._title = title
        self._buffering = False
        self._transcode_attempted = False
        self._time_label.setText(self.tr("Loading…"))
        self._player.setSource(QUrl(self._url))
        self._player.play()

    def eventFilter(self, obj, event) -> bool:
        from PySide6.QtCore import QEvent

        # Single click toggles play/pause instantly; double click toggles
        # fullscreen. We deliberately DON'T debounce the single click behind a
        # timer: a double click then delivers two single toggles (pause then
        # unpause — net no change) followed by the fullscreen switch, so
        # playback state is preserved and there's no lag. The old timer-based
        # approach mistook a quick pause→resume double-tap for a double click
        # and flipped fullscreen, making the window "jump".
        if obj == self._video:
            if event.type() == QEvent.Type.MouseButtonDblClick:
                if event.button() == Qt.MouseButton.LeftButton:
                    self._toggle_fullscreen()
                    return True
            elif event.type() == QEvent.Type.MouseButtonRelease:
                if event.button() == Qt.MouseButton.LeftButton:
                    self._toggle()
                    return True
        return super().eventFilter(obj, event)

    def _on_speed_changed(self, text: str) -> None:
        try:
            rate = float(text.replace("x", ""))
            self._player.setPlaybackRate(rate)
        except ValueError:
            pass

    def _toggle(self) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _on_state(self, state) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._play_btn.setText("⏸" if playing else "▶")

    def _on_duration(self, dur: int) -> None:
        self._duration = int(dur)
        self._position.setRange(0, self._duration)
        self._update_time()

    def _on_position(self, pos: int) -> None:
        if not self._seeking:
            self._position.setValue(int(pos))
        self._update_time()

    def _on_slider_pressed(self) -> None:
        self._seeking = True

    def _on_slider_preview(self, value: int) -> None:
        # Only a visual time preview while dragging — we don't send the seek
        # itself on every mouse move (see the comment on sliderMoved.connect
        # above).
        self._time_label.setText(
            f"{_format_time(value)} / {_format_time(self._duration)}"
        )

    def _on_slider_released(self) -> None:
        self._player.setPosition(self._position.value())
        self._seeking = False

    def _seek_relative(self, delta: int) -> None:
        limit = self._duration or 0
        new_pos = max(0, min(limit, self._player.position() + delta))
        self._player.setPosition(new_pos)

    def _update_time(self) -> None:
        suffix = self.tr(" · buffering…") if self._buffering else ""
        self._time_label.setText(
            f"{_format_time(self._player.position())} / "
            f"{_format_time(self._duration)}{suffix}"
        )

    def _on_media_status(self, status) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        # The player shows nothing of its own while the network lags behind
        # (the next chunk of the stream window is still downloading) — the
        # frame just freezes, which looks like a hang/bug rather than
        # buffering. An explicit indicator removes that confusion.
        self._buffering = status in (
            QMediaPlayer.MediaStatus.BufferingMedia,
            QMediaPlayer.MediaStatus.StalledMedia,
        )
        self._update_time()

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _on_error(self, error, error_string: str) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if error == QMediaPlayer.Error.NoError:
            return
        logger.warning("Video playback error for %s: %s", self._title, error_string)
        if self._try_transcode_fallback():
            return
        self._time_label.setText(self.tr("Error: {0}").format(error_string))
        self._fallback_btn.show()

    def _try_transcode_fallback(self) -> bool:
        """Non-native format/codec: restart playback once via server-side
        transcode (``?transcode=1`` — ffmpeg repackages into fragmented MP4
        on the fly, see app.core.transcode). True = playback was restarted."""
        if self._transcode_attempted or "transcode=" in self._url:
            return False
        if "/api/media" not in self._url:
            return False  # not our local streaming server — this fallback doesn't apply
        from app.core.transcode import transcode_available

        if not transcode_available():
            return False
        self._transcode_attempted = True
        self._url = f"{self._url}&transcode=1"
        self._time_label.setText(self.tr("Format not supported — transcoding…"))
        logger.info("Retrying playback via server-side transcode: %s", self._title)
        self._player.setSource(QUrl(self._url))
        self._player.play()
        return True

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        try:
            self._player.stop()
            self._player.setSource(QUrl())
        except Exception:
            pass
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
class PdfViewerWindow(QWidget):
    def __init__(self, url: str, title: str) -> None:
        super().__init__()
        from PySide6.QtPdf import QPdfDocument
        from PySide6.QtPdfWidgets import QPdfView

        self.setWindowTitle(title)
        self.resize(1000, 800)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._view = QPdfView(self)
        self._view.setPageMode(QPdfView.PageMode.MultiPage)
        layout.addWidget(self._view, 1)

        self._status = QLabel(self.tr("Loading PDF…"), self)
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._status)

        self._doc = QPdfDocument(self)
        self._buffer = None
        self._data = None

        self._nam = QNetworkAccessManager(self)
        self._reply = self._nam.get(QNetworkRequest(QUrl(url)))
        self._reply.finished.connect(self._on_finished)

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.close)

    def _on_finished(self) -> None:
        reply = self._reply
        self._reply = None
        if reply is None:
            return
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                try:
                    self._status.setText(
                        self.tr("Loading error: {0}").format(reply.errorString())
                    )
                except RuntimeError:
                    pass
                return
            raw = bytes(reply.readAll().data())
        finally:
            reply.deleteLater()

        if not raw:
            self._status.setText(self.tr("The file is empty."))
            return

        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtPdf import QPdfDocument

        # We KEEP a reference to the QByteArray on self: QBuffer doesn't own the
        # data, and with a temporary array the document silently loaded into
        # Status.Error (blank window).
        self._data = QByteArray(raw)
        self._buffer = QBuffer(self._data, self)
        self._buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self._doc.load(self._buffer)

        if self._doc.status() == QPdfDocument.Status.Error:
            self._status.setText(self.tr("Failed to open the PDF (file corrupted?)"))
            return

        self._view.setDocument(self._doc)
        self._status.hide()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if hasattr(self, "_reply") and self._reply is not None:
            reply = self._reply
            self._reply = None
            reply.abort()
            reply.deleteLater()
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def open_media_viewer(
    parent: QWidget | None,
    *,
    url: str,
    title: str,
    viewer_type: str = "image",
) -> QWidget | None:
    """Open a photo/video/pdf in a separate native window.

    If the window can't be created (e.g. no multimedia backend) — falls back
    to opening in the external browser.
    """
    try:
        if viewer_type == "video":
            window: QWidget = VideoViewerWindow(url, title)
        elif viewer_type == "pdf":
            window: QWidget = PdfViewerWindow(url, title)
        else:
            window: QWidget = ImageViewerWindow(url, title)
    except Exception:
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl(url))
        return None

    window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    _register(window)
    window.show()
    window.raise_()
    window.activateWindow()
    return window
