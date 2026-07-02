"""Нативные окна просмотра видео и фото (замена открытия превью в браузере).

Видео/фото открываются в отдельном top-level окне на Qt и не зависят от
браузера:
  * видео — QMediaPlayer + QAudioOutput + QVideoWidget (play/pause/seek/volume,
    горячие клавиши);
  * фото — QGraphicsView + QPixmap (зум колесом, панорама перетаскиванием,
    двойной клик — вписать).

Источник — тот же локальный стрим-сервер (HTTP Range), поэтому файл не
скачивается целиком. При невозможности создать нативное окно (нет
мультимедиа-бэкенда и т.п.) выполняется фолбэк на открытие в браузере.
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

# Держим ссылки на открытые окна, иначе их соберёт сборщик мусора и окно
# мгновенно закроется сразу после show().
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
# Фото
# --------------------------------------------------------------------------- #
class _ImageView(QGraphicsView):
    """QGraphicsView с зумом колеса и панорамой перетаскиванием."""

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

        self._status = QLabel("Загрузка…", self)
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
                    self._status.setText("Буферизация…")
                    from PySide6.QtCore import QTimer

                    QTimer.singleShot(1500, self._reload)
                    return
                try:
                    self._status.setText(f"Ошибка загрузки: {reply.errorString()}")
                except RuntimeError:
                    pass
                return
            raw = bytes(reply.readAll().data())
        finally:
            reply.deleteLater()

        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import QImageReader

        if not raw:
            self._show_open_external("Файл пуст.")
            return

        # Декодируем через QImageReader: задействуются все доступные Qt
        # форматные плагины (png/webp/gif/bmp/ico/svg/tga/...) и учитывается
        # EXIF-ориентация, чтобы фото с телефона не отображалось повёрнутым.
        buffer = QBuffer(QByteArray(raw), self)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        image = reader.read()

        pixmap = QPixmap.fromImage(image) if not image.isNull() else QPixmap()
        if pixmap.isNull():
            # Последняя попытка — прямой разбор байтов средствами Qt.
            pixmap = QPixmap()
            pixmap.loadFromData(raw)

        if pixmap.isNull():
            # Формат, который Qt декодировать не умеет (heic/avif/tiff/raw/psd
            # и т.п.) — пробуем сконвертировать в PNG через ffmpeg в фоне.
            self._decode_via_ffmpeg_async(raw)
            return

        self._view.set_pixmap(pixmap)
        self._status.hide()

    def _reload(self) -> None:
        self._reply = self._nam.get(QNetworkRequest(QUrl(self._url)))
        self._reply.finished.connect(self._on_finished)

    def _decode_via_ffmpeg_async(self, raw: bytes) -> None:
        """Фолбэк для форматов, которые Qt не декодирует сам (heic/avif/tiff/
        raw/psd…): конвертируем в PNG через ffmpeg в фоне."""
        import threading

        self._status.setText("Конвертация формата...")
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
                        "Не удалось загрузить сконвертированное изображение."
                    )
            else:
                self._show_open_external(
                    "Этот формат изображения не поддерживается встроенным просмотром."
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
        btn = QPushButton("Открыть во внешнем приложении", self)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)

        def _open_external() -> None:
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl(self._url))
            self.close()

        btn.clicked.connect(_open_external)
        self.layout().addWidget(btn)


# --------------------------------------------------------------------------- #
# Видео
# --------------------------------------------------------------------------- #
class ClickableSlider(QSlider):
    """Слайдер, позволяющий кликнуть в любую точку для моментального перехода."""

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

        self._fallback_btn = QPushButton("Внешний плеер", self)
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

        from PySide6.QtCore import QTimer

        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(250)
        self._click_timer.timeout.connect(self._toggle)

        self._play_btn.clicked.connect(self._toggle)
        # sliderMoved стреляет на КАЖДОЕ движение мыши при перетаскивании — если
        # вешать на него player.setPosition напрямую, перемотка превращается в
        # шквал сетевых Range-запросов к стрим-серверу (по одному на пиксель
        # драга), отсюда лаги/подвисания при скрабе. Пока тянут ползунок — только
        # превью времени в лейбле; реальный seek — один раз, на отпускании кнопки.
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
        self._time_label.setText("Загрузка…")
        self._player.setSource(QUrl(self._url))
        self._player.play()

    def eventFilter(self, obj, event) -> bool:
        from PySide6.QtCore import QEvent

        if obj == self._video:
            if event.type() == QEvent.Type.MouseButtonDblClick:
                self._click_timer.stop()
                self._toggle_fullscreen()
                return True
            elif event.type() == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.LeftButton:
                    if not self._click_timer.isActive():
                        self._click_timer.start()
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
        # Только визуальное превью времени во время протяжки — сам seek не
        # шлём на каждое движение мыши (см. коммент у sliderMoved.connect выше).
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
        suffix = " · буферизация…" if self._buffering else ""
        self._time_label.setText(
            f"{_format_time(self._player.position())} / "
            f"{_format_time(self._duration)}{suffix}"
        )

    def _on_media_status(self, status) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        # Плеер не показывает ничего своё, пока сеть отстаёт (следующая часть
        # стрим-окна ещё качается) — кадр просто замирает, и это выглядит как
        # зависание/баг, а не буферизация. Явный индикатор снимает эту путаницу.
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
        self._time_label.setText(f"Ошибка: {error_string}")
        self._fallback_btn.show()

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

        self._status = QLabel("Загрузка PDF…", self)
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
                    self._status.setText(f"Ошибка загрузки: {reply.errorString()}")
                except RuntimeError:
                    pass
                return
            raw = bytes(reply.readAll().data())
        finally:
            reply.deleteLater()

        if not raw:
            self._status.setText("Файл пуст.")
            return

        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtPdf import QPdfDocument

        # Ссылку на QByteArray ДЕРЖИМ в self: QBuffer не владеет данными, и с
        # временным массивом документ молча грузился в Status.Error (пустое окно).
        self._data = QByteArray(raw)
        self._buffer = QBuffer(self._data, self)
        self._buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self._doc.load(self._buffer)

        if self._doc.status() == QPdfDocument.Status.Error:
            self._status.setText("Не удалось открыть PDF (файл повреждён?)")
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
# Точка входа
# --------------------------------------------------------------------------- #
def open_media_viewer(
    parent: QWidget | None,
    *,
    url: str,
    title: str,
    viewer_type: str = "image",
) -> QWidget | None:
    """Открыть фото/видео/pdf в отдельном нативном окне.

    При невозможности создать окно (например, нет мультимедиа-бэкенда) —
    фолбэк на открытие во внешнем браузере.
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
