from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QImageReader,
    QPainter,
    QPen,
    QPixmap,
    QPolygon,
)


_EXTENSION_PALETTES: dict[str, tuple[str, str, str]] = {
    "text": ("#3d8cff", "#2a5fb0", "#2f6fd1"),
    "document": ("#4f76d9", "#3752a4", "#4466c5"),
    "table": ("#2ea56f", "#1f7a52", "#278d5f"),
    "slides": ("#d18234", "#9f5e21", "#bb742e"),
    "pdf": ("#dd5252", "#a53a3a", "#c94444"),
    "archive": ("#d39f36", "#9a7020", "#b7882f"),
    "binary": ("#d35f4f", "#9b4035", "#b85043"),
    "code": ("#37a8d8", "#26799b", "#2f95bf"),
    "image": ("#c55de3", "#8e42a4", "#ad4fc9"),
    "audio": ("#a06ad9", "#724aa0", "#8d5ec2"),
    "video": ("#5f79df", "#4055a2", "#546cc7"),
    "package": ("#8c5cff", "#6541bf", "#7a50df"),
    "default": ("#6f8096", "#4e5c6d", "#5f6f84"),
}

_EXTENSION_SPECIFIC_PALETTES: dict[str, tuple[str, str, str]] = {
    # text / docs
    "txt": ("#4aa4ff", "#2d6fb8", "#3889df"),
    "md": ("#6b9bff", "#486dbe", "#5a86dc"),
    "log": ("#669fe4", "#4a74ab", "#5b8dcb"),
    "toml": ("#61a7cc", "#457996", "#538fb0"),
    "json": ("#3ca8e8", "#2a7ba8", "#3296cd"),
    "xml": ("#45b0cf", "#2f7f98", "#3a99b7"),
    "yaml": ("#5a9ec7", "#3e7392", "#4c8aaf"),
    "yml": ("#5a9ec7", "#3e7392", "#4c8aaf"),
    "pdf": ("#e35757", "#a93f3f", "#ce4a4a"),
    "doc": ("#4f7cff", "#3a58b8", "#466eda"),
    "docx": ("#5a85ff", "#4161bb", "#4e74de"),
    "epub": ("#4d84d4", "#3a629d", "#4576bc"),
    "rtf": ("#6a8ce3", "#4c67ab", "#5c7ec9"),
    # tables / slides
    "xls": ("#2db277", "#208356", "#279967"),
    "xlsx": ("#30b67a", "#21885a", "#2a9f6b"),
    "csv": ("#37bc7e", "#25905f", "#2da46d"),
    "tsv": ("#42bf82", "#2c9766", "#36a973"),
    "ppt": ("#df8a3f", "#a6652a", "#c57834"),
    "pptx": ("#e09144", "#ad6a2c", "#ca7f38"),
    # archives
    "zip": ("#d7a63b", "#a17824", "#c18f32"),
    "rar": ("#d18b2c", "#975f18", "#b87724"),
    "7z": ("#c67e24", "#8e5616", "#ad6d1d"),
    "tar": ("#be8b3f", "#875f25", "#a97634"),
    "gz": ("#b97834", "#845423", "#a3682d"),
    "zst": ("#b7823a", "#845c26", "#a26f31"),
    # binaries
    "exe": ("#e1645a", "#aa473e", "#cb544b"),
    "msi": ("#d95b56", "#a0443e", "#c04e48"),
    "dll": ("#cc6d62", "#984c45", "#b85e54"),
    "iso": ("#a56be1", "#744aa1", "#915dca"),
    "dmg": ("#9a68d8", "#6f49a0", "#875bc2"),
    # code
    "py": ("#4ea6e2", "#31769f", "#3c8ec2"),
    "html": ("#4da9d8", "#357a9d", "#428fba"),
    "css": ("#53ace0", "#3a80a6", "#4796c4"),
    "js": ("#46b7db", "#2f839e", "#399dc0"),
    "ts": ("#4f9be8", "#356fad", "#4288cf"),
    "sql": ("#45a0d8", "#32749e", "#3b8abf"),
    "ps1": ("#5d9dd4", "#426f95", "#4f89b5"),
    # media
    "jpg": ("#cf5ee5", "#973fa9", "#b551cc"),
    "jpeg": ("#cf5ee5", "#973fa9", "#b551cc"),
    "png": ("#bf56dc", "#8c3da4", "#a84cc4"),
    "gif": ("#b950d2", "#84399a", "#a146bc"),
    "webp": ("#be53d7", "#883b9d", "#a54abc"),
    "svg": ("#b45ed6", "#82449d", "#9d53be"),
    "mp3": ("#a470e0", "#784da5", "#9262c8"),
    "wav": ("#9a6ed2", "#704b9b", "#895dc0"),
    "flac": ("#8f68c8", "#674693", "#7e58b3"),
    "mp4": ("#617de5", "#4459a7", "#556fd0"),
    "mkv": ("#5d73d9", "#42509d", "#5064c1"),
    "avi": ("#586ace", "#3f4a96", "#4a5bb7"),
    # packages / installers
    "apk": ("#8b61f4", "#6546ba", "#7955da"),
    "jar": ("#8d5cf7", "#6742c3", "#7a50df"),
    "whl": ("#9062fa", "#6b48c5", "#7f57e3"),
    "deb": ("#8659ea", "#6241af", "#734ed3"),
}

_EXTENSION_GROUPS: dict[str, str] = {
    "txt": "text",
    "md": "text",
    "log": "text",
    "nfo": "text",
    "srt": "text",
    "ass": "text",
    "ssa": "text",
    "vtt": "text",
    "sub": "text",
    "ini": "text",
    "cfg": "text",
    "conf": "text",
    "properties": "text",
    "env": "text",
    "toml": "text",
    "json": "text",
    "xml": "text",
    "yaml": "text",
    "yml": "text",
    "pdf": "pdf",
    "doc": "document",
    "docx": "document",
    "docm": "document",
    "dotx": "document",
    "rtf": "document",
    "odt": "document",
    "odf": "document",
    "pages": "document",
    "epub": "document",
    "fb2": "document",
    "mobi": "document",
    "djvu": "document",
    "chm": "document",
    "xps": "document",
    "xls": "table",
    "xlsx": "table",
    "csv": "table",
    "tsv": "table",
    "numbers": "table",
    "ods": "table",
    "ppt": "slides",
    "pptx": "slides",
    "pps": "slides",
    "ppsx": "slides",
    "key": "slides",
    "odp": "slides",
    "zip": "archive",
    "rar": "archive",
    "7z": "archive",
    "tar": "archive",
    "tgz": "archive",
    "gz": "archive",
    "bz2": "archive",
    "xz": "archive",
    "zst": "archive",
    "cab": "archive",
    "lz4": "archive",
    "exe": "binary",
    "msi": "binary",
    "bat": "binary",
    "cmd": "binary",
    "com": "binary",
    "dll": "binary",
    "sys": "binary",
    "iso": "binary",
    "dmg": "binary",
    "appimage": "binary",
    "vhd": "binary",
    "vhdx": "binary",
    "qcow2": "binary",
    "vmdk": "binary",
    "img": "binary",
    "bin": "binary",
    "py": "code",
    "ipynb": "code",
    "js": "code",
    "ts": "code",
    "tsx": "code",
    "jsx": "code",
    "html": "code",
    "htm": "code",
    "css": "code",
    "scss": "code",
    "sass": "code",
    "less": "code",
    "sql": "code",
    "java": "code",
    "cpp": "code",
    "cc": "code",
    "c": "code",
    "h": "code",
    "hpp": "code",
    "cs": "code",
    "vb": "code",
    "go": "code",
    "rs": "code",
    "kt": "code",
    "kts": "code",
    "swift": "code",
    "rb": "code",
    "pl": "code",
    "lua": "code",
    "r": "code",
    "dart": "code",
    "vue": "code",
    "svelte": "code",
    "php": "code",
    "sh": "code",
    "ps1": "code",
    "psm1": "code",
    "psd1": "code",
    "jpg": "image",
    "jpeg": "image",
    "png": "image",
    "gif": "image",
    "webp": "image",
    "bmp": "image",
    "ico": "image",
    "tif": "image",
    "tiff": "image",
    "avif": "image",
    "svg": "image",
    "raw": "image",
    "cr2": "image",
    "nef": "image",
    "psd": "image",
    "ai": "image",
    "heic": "image",
    "mp3": "audio",
    "wav": "audio",
    "flac": "audio",
    "ogg": "audio",
    "opus": "audio",
    "wma": "audio",
    "aiff": "audio",
    "amr": "audio",
    "aac": "audio",
    "m4a": "audio",
    "mp4": "video",
    "mkv": "video",
    "avi": "video",
    "m4v": "video",
    "wmv": "video",
    "flv": "video",
    "mpeg": "video",
    "mpg": "video",
    "mts": "video",
    "m2ts": "video",
    "3gp": "video",
    "mov": "video",
    "webm": "video",
    "apk": "package",
    "ipa": "package",
    "deb": "package",
    "rpm": "package",
    "jar": "package",
    "whl": "package",
    "egg": "package",
    "nupkg": "package",
    "pkg": "package",
    "msix": "package",
    "appx": "package",
    "torrent": "package",
}

_GROUP_GLYPHS: dict[str, str] = {
    "text": "lines",
    "document": "document",
    "table": "table",
    "slides": "slides",
    "pdf": "pdf",
    "archive": "archive",
    "binary": "binary",
    "code": "code",
    "image": "image",
    "audio": "audio",
    "video": "video",
    "package": "package",
    "default": "dot",
}

_EXTENSION_LABEL_OVERRIDES: dict[str, str] = {
    "jpeg": "JPG",
    "yaml": "YAML",
    "yml": "YAML",
    "json": "JSON",
    "xml": "XML",
    "toml": "TOML",
    "csv": "CSV",
    "tsv": "TSV",
    "properties": "PROP",
    "numbers": "NUM",
    "txt": "TXT",
    "md": "MD",
    "ps1": "PS1",
    "ipynb": "IPYN",
    "scss": "SCSS",
    "sass": "SASS",
    "less": "LESS",
    "html": "HTML",
    "tsx": "TSX",
    "jsx": "JSX",
    "tiff": "TIFF",
    "mpeg": "MPEG",
    "m2ts": "M2TS",
    "appimage": "APP",
    "torrent": "TOR",
    "7z": "7Z",
}


def _build_folder_icon(size: int = 64) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)

    # Soft pastel pink folder icon scaled from a 64x64 design grid.
    body_color = QColor("#fba8be")
    flap_color = QColor("#fcd5df")
    edge_color = QColor("#c27a8e")
    scale = max(0.2, size / 64.0)

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen_width = max(1.0, 1.5 * scale)

    # Draw flap (back part)
    painter.setPen(QPen(edge_color, pen_width))
    painter.setBrush(flap_color)
    painter.drawRoundedRect(
        12 * scale,
        14 * scale,
        22 * scale,
        10 * scale,
        5 * scale,
        5 * scale,
    )

    # Draw main body
    painter.setPen(QPen(edge_color, pen_width))
    painter.setBrush(body_color)
    painter.drawRoundedRect(
        8 * scale,
        19 * scale,
        48 * scale,
        32 * scale,
        8 * scale,
        8 * scale,
    )

    # Glossy highlight
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(255, 249, 228, 82))
    painter.drawRoundedRect(
        12 * scale,
        23 * scale,
        40 * scale,
        6 * scale,
        3 * scale,
        3 * scale,
    )
    painter.end()

    return QIcon(pix)


def _build_file_icon_with_badge(
    base_icon: QIcon,
    badge_kind: str,
    recently_exported: bool = False,
    loading_phase: int = 0,
    size: int = 58,
) -> QIcon:
    pix = base_icon.pixmap(size, size)
    if pix.isNull():
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    badge_size = max(18, int(size * 0.33))
    margin = 2
    bx = size - badge_size - margin
    by = size - badge_size - margin

    if badge_kind == "downloaded":
        fill = QColor("#2fd785")
        border = QColor("#1f9f61")
    elif badge_kind == "loading":
        fill = QColor("#8a5dff")
        border = QColor("#5f3ec6")
    else:
        # Purple-pink badge for "not downloaded yet".
        fill = QColor("#c957ff")
        border = QColor("#9434cf")

    painter.setPen(QPen(border, 2))
    painter.setBrush(fill)
    painter.drawEllipse(bx, by, badge_size, badge_size)

    if recently_exported:
        painter.setPen(QPen(QColor("#ff7ad8"), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(bx - 2, by - 2, badge_size + 4, badge_size + 4)

    painter.setPen(
        QPen(
            QColor("#f8fbff"),
            2.2,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
    )

    if badge_kind == "downloaded":
        x1 = int(bx + badge_size * 0.30)
        y1 = int(by + badge_size * 0.56)
        x2 = int(bx + badge_size * 0.46)
        y2 = int(by + badge_size * 0.72)
        x3 = int(bx + badge_size * 0.74)
        y3 = int(by + badge_size * 0.36)
        painter.drawLine(x1, y1, x2, y2)
        painter.drawLine(x2, y2, x3, y3)
    elif badge_kind == "loading":
        # 3 minimalist vertical bars with center-first wave.
        phase = int(loading_phase) % 8
        bars = [0.40, 0.58, 0.78, 0.95, 0.78, 0.58, 0.40, 0.58]
        center_scale = bars[phase]
        side_scale = bars[(phase + 2) % len(bars)]

        cx = int(bx + badge_size * 0.50)
        cy = int(by + badge_size * 0.50)
        spacing = max(3, int(badge_size * 0.18))
        line_w = max(1.6, badge_size * 0.09)
        max_half = int(badge_size * 0.34)
        min_half = int(badge_size * 0.12)

        def _half_len(scale: float) -> int:
            raw = int(max_half * scale)
            return max(min_half, min(max_half, raw))

        c_half = _half_len(center_scale)
        s_half = _half_len(side_scale)
        left_x = cx - spacing
        right_x = cx + spacing

        painter.setPen(
            QPen(
                QColor("#f9fbff"),
                line_w,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.drawLine(left_x, cy - s_half, left_x, cy + s_half)
        painter.drawLine(cx, cy - c_half, cx, cy + c_half)
        painter.drawLine(right_x, cy - s_half, right_x, cy + s_half)
    else:
        cx = int(bx + badge_size * 0.5)
        top = int(by + badge_size * 0.28)
        mid = int(by + badge_size * 0.60)
        bottom = int(by + badge_size * 0.74)
        painter.drawLine(cx, top, cx, mid)
        painter.drawLine(
            int(cx - badge_size * 0.18), int(mid - badge_size * 0.05), cx, bottom
        )
        painter.drawLine(
            int(cx + badge_size * 0.18), int(mid - badge_size * 0.05), cx, bottom
        )
        painter.drawLine(
            int(bx + badge_size * 0.30),
            int(by + badge_size * 0.80),
            int(bx + badge_size * 0.70),
            int(by + badge_size * 0.80),
        )

    painter.end()
    return QIcon(pix)


def _file_extension_token(file_name: str) -> str:
    ext = Path(file_name).suffix.strip().lower().lstrip(".")
    return ext or "file"


def is_image_name(file_name: str) -> bool:
    """Картинка ли это (по расширению) — для превью в гриде."""
    return _EXTENSION_GROUPS.get(_file_extension_token(file_name)) == "image"


def is_video_name(file_name: str) -> bool:
    """Видео ли это (по расширению) — для кадра-постера в гриде (инкремент 4)."""
    return _EXTENSION_GROUPS.get(_file_extension_token(file_name)) == "video"


def is_pdf_name(file_name: str) -> bool:
    """PDF-документ (по расширению) — для встроенного просмотра."""
    return _EXTENSION_GROUPS.get(_file_extension_token(file_name)) == "pdf"


def is_text_editable_name(file_name: str) -> bool:
    """Текстовые и кодовые файлы (а также файлы без расширения), которые можно
    открыть в редакторе."""
    token = _file_extension_token(file_name)
    if token == "file":
        # Имя без расширения (напр. '1232113123123') — трактуем как текст,
        # чтобы такой файл открывался по двойному клику.
        return True
    return _EXTENSION_GROUPS.get(token) in ("text", "code")


def make_thumbnail_icon(path: str, size: int = 58) -> QIcon | None:
    """Миниатюра из локального файла-картинки. `QImageReader.setScaledSize`
    декодирует сразу уменьшенным — память не жрёт на больших картинках.
    На ошибке возвращает None (фолбэк на типовую иконку)."""
    try:
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)  # учесть EXIF-ориентацию
        src = reader.size()
        if not src.isValid() or src.width() <= 0 or src.height() <= 0:
            return None
        scaled = src.scaled(
            QSize(int(size), int(size)), Qt.AspectRatioMode.KeepAspectRatio
        )
        if scaled.width() > 0 and scaled.height() > 0:
            reader.setScaledSize(scaled)
        image = reader.read()
        if image.isNull():
            return None
        pix = QPixmap.fromImage(image)
        if pix.isNull():
            return None
        return QIcon(pix)
    except Exception:
        return None


def _extension_visual_style(file_name: str) -> tuple[str, QColor, QColor, QColor, str]:
    ext = _file_extension_token(file_name)
    group = _EXTENSION_GROUPS.get(ext, "default")
    accent_raw, border_raw, chip_raw = _EXTENSION_SPECIFIC_PALETTES.get(
        ext,
        _EXTENSION_PALETTES[group],
    )
    label = _EXTENSION_LABEL_OVERRIDES.get(
        ext,
        ext[:4].upper() if ext != "file" else "FILE",
    )
    glyph = _GROUP_GLYPHS.get(group, "dot")
    return label, QColor(accent_raw), QColor(border_raw), QColor(chip_raw), glyph


def _draw_extension_glyph(
    painter: QPainter,
    glyph_kind: str,
    left: int,
    top: int,
    width: int,
    height: int,
) -> None:
    glyph_left = left + 9
    glyph_right = left + width - 9
    glyph_top = top + 22
    glyph_bottom = top + height - 24
    glyph_w = max(12, glyph_right - glyph_left)
    glyph_h = max(10, glyph_bottom - glyph_top)
    cx = glyph_left + glyph_w // 2
    cy = glyph_top + glyph_h // 2

    stroke = QColor("#f7faff")
    stroke.setAlpha(218)
    soft_fill = QColor("#f7faff")
    soft_fill.setAlpha(38)
    strong_fill = QColor("#f7faff")
    strong_fill.setAlpha(176)

    base_pen = QPen(
        stroke,
        1.7,
        Qt.PenStyle.SolidLine,
        Qt.PenCapStyle.RoundCap,
        Qt.PenJoinStyle.RoundJoin,
    )
    painter.setPen(base_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if glyph_kind == "lines":
        for i in range(3):
            y = glyph_top + int((i + 1) * glyph_h / 4)
            inset = 0 if i < 2 else 8
            painter.drawLine(glyph_left, y, glyph_right - inset, y)
        return

    if glyph_kind == "document":
        bullet_r = 2
        for i in range(3):
            y = glyph_top + int((i + 1) * glyph_h / 4)
            painter.setBrush(strong_fill)
            painter.drawEllipse(glyph_left, y - bullet_r, bullet_r * 2, bullet_r * 2)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(glyph_left + 8, y, glyph_right, y)
        return

    if glyph_kind == "table":
        cell_w = max(5, int(glyph_w * 0.43))
        cell_h = max(4, int(glyph_h * 0.35))
        x1 = glyph_left
        y1 = glyph_top + 2
        gap_x = max(3, glyph_w - (cell_w * 2))
        gap_y = max(3, glyph_h - (cell_h * 2))
        for row in range(2):
            for col in range(2):
                painter.drawRoundedRect(
                    x1 + col * (cell_w + gap_x),
                    y1 + row * (cell_h + gap_y),
                    cell_w,
                    cell_h,
                    2,
                    2,
                )
        return

    if glyph_kind == "slides":
        painter.drawRoundedRect(glyph_left, glyph_top + 1, glyph_w, glyph_h - 2, 3, 3)
        bar_y = glyph_top + max(4, int(glyph_h * 0.28))
        painter.drawLine(glyph_left + 3, bar_y, glyph_right - 3, bar_y)
        tri = QPolygon(
            [
                QPoint(glyph_left + 6, bar_y + 3),
                QPoint(glyph_left + 6, glyph_bottom - 4),
                QPoint(glyph_right - 6, cy),
            ]
        )
        painter.setBrush(soft_fill)
        painter.drawPolygon(tri)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        return

    if glyph_kind == "pdf":
        pdf_font = QFont()
        pdf_font.setBold(True)
        pdf_font.setPointSize(7)
        painter.setFont(pdf_font)
        painter.drawText(
            glyph_left,
            glyph_top,
            glyph_w,
            glyph_h,
            int(Qt.AlignmentFlag.AlignCenter),
            "PDF",
        )
        return

    if glyph_kind == "archive":
        zipper_x = cx
        painter.drawLine(zipper_x, glyph_top + 1, zipper_x, glyph_bottom - 1)
        tooth_h = max(3, int(glyph_h * 0.15))
        tooth_w = max(3, int(glyph_w * 0.11))
        y = glyph_top + 2
        while y < glyph_bottom - 2:
            painter.setBrush(strong_fill)
            painter.drawRect(zipper_x - tooth_w // 2, y, tooth_w, tooth_h)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            y += tooth_h + 2
        return

    if glyph_kind == "binary":
        body_w = max(12, int(glyph_w * 0.62))
        body_h = max(10, int(glyph_h * 0.60))
        bx = cx - body_w // 2
        by = cy - body_h // 2
        painter.drawRoundedRect(bx, by, body_w, body_h, 3, 3)
        pin_len = max(3, int(body_w * 0.10))
        for i in range(3):
            py = by + int((i + 1) * body_h / 4)
            painter.drawLine(bx - pin_len, py, bx, py)
            painter.drawLine(bx + body_w, py, bx + body_w + pin_len, py)
        painter.setBrush(strong_fill)
        painter.drawEllipse(cx - 2, cy - 2, 4, 4)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        return

    if glyph_kind == "code":
        lx = glyph_left + 4
        rx = glyph_right - 4
        painter.drawLine(lx + 4, glyph_top + 3, lx, cy)
        painter.drawLine(lx, cy, lx + 4, glyph_bottom - 3)
        painter.drawLine(rx - 4, glyph_top + 3, rx, cy)
        painter.drawLine(rx, cy, rx - 4, glyph_bottom - 3)
        painter.drawLine(cx + 2, glyph_top + 2, cx - 2, glyph_bottom - 2)
        return

    if glyph_kind == "image":
        sun_r = max(2, int(glyph_h * 0.12))
        painter.setBrush(strong_fill)
        painter.drawEllipse(glyph_left + 2, glyph_top + 1, sun_r * 2, sun_r * 2)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        mountain = QPolygon(
            [
                QPoint(glyph_left + 1, glyph_bottom - 2),
                QPoint(glyph_left + int(glyph_w * 0.33), cy + 2),
                QPoint(glyph_left + int(glyph_w * 0.53), glyph_bottom - 6),
                QPoint(glyph_left + int(glyph_w * 0.72), cy + 4),
                QPoint(glyph_right - 1, glyph_bottom - 2),
            ]
        )
        painter.drawPolyline(mountain)
        return

    if glyph_kind == "audio":
        stem_x = cx + 2
        top_y = glyph_top + 2
        bot_y = glyph_bottom - 4
        painter.drawLine(stem_x, top_y, stem_x, bot_y)
        painter.drawLine(stem_x, top_y, stem_x + 7, top_y + 2)
        painter.setBrush(strong_fill)
        painter.drawEllipse(stem_x - 10, bot_y - 2, 6, 6)
        painter.drawEllipse(stem_x - 2, bot_y - 1, 6, 6)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        return

    if glyph_kind == "video":
        frame_h = max(10, int(glyph_h * 0.72))
        fy = cy - frame_h // 2
        painter.drawRoundedRect(glyph_left + 2, fy, glyph_w - 4, frame_h, 3, 3)
        tri = QPolygon(
            [
                QPoint(cx - 3, cy - 5),
                QPoint(cx - 3, cy + 5),
                QPoint(cx + 6, cy),
            ]
        )
        painter.setBrush(strong_fill)
        painter.drawPolygon(tri)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        return

    if glyph_kind == "package":
        box = QPolygon(
            [
                QPoint(cx, glyph_top + 1),
                QPoint(glyph_right - 2, glyph_top + 6),
                QPoint(glyph_right - 2, glyph_bottom - 2),
                QPoint(cx, glyph_bottom),
                QPoint(glyph_left + 2, glyph_bottom - 2),
                QPoint(glyph_left + 2, glyph_top + 6),
            ]
        )
        painter.drawPolygon(box)
        painter.drawLine(cx, glyph_top + 1, cx, glyph_bottom)
        painter.drawLine(glyph_left + 2, glyph_top + 6, cx, glyph_top + 12)
        painter.drawLine(glyph_right - 2, glyph_top + 6, cx, glyph_top + 12)
        return

    painter.setBrush(strong_fill)
    painter.drawEllipse(cx - 3, cy - 3, 6, 6)
    painter.setBrush(Qt.BrushStyle.NoBrush)


def _build_typed_file_icon(file_name: str, status: str, size: int = 58) -> QIcon:
    label, accent, border, _chip, glyph_kind = _extension_visual_style(file_name)
    # Цвет акцента/угловой метки по состоянию: damaged=красный, offline=синий,
    # прочие незавершённые=оранжевый.
    warn_glyph = "!"
    if status == "damaged":
        accent = QColor("#fc8181")  # red
        border = QColor("#c53030")
        warn_glyph = "✖"
    elif status == "offline":
        accent = QColor("#8fb8ff")  # blue
        border = QColor("#3b62a8")
        warn_glyph = "☁"
    elif status != "complete":
        accent = QColor("#f6ad55")  # Orange-ish
        border = QColor("#c05621")

    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    left = int(size * 0.16)
    top = int(size * 0.08)
    width = int(size * 0.68)
    height = int(size * 0.84)
    fold = max(10, int(width * 0.28))

    # Shadow effect
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(0, 0, 0, 40))
    painter.drawRoundedRect(left + 2, top + 2, width, height, 8, 8)

    # Main card body
    painter.setPen(QPen(border, 1.2))
    painter.setBrush(QColor("#ffffff"))
    painter.drawRoundedRect(left, top, width, height, 8, 8)

    # Corner fold (Dog-ear)
    painter.setPen(QPen(border, 1.2))
    painter.setBrush(QColor("#f7fafc"))
    fold_poly = QPolygon(
        [
            QPoint(left + width - fold, top),
            QPoint(left + width, top + fold),
            QPoint(left + width - fold, top + fold),
        ]
    )
    painter.drawPolygon(fold_poly)
    painter.drawLine(left + width - fold, top, left + width - fold, top + fold)
    painter.drawLine(left + width - fold, top + fold, left + width, top + fold)

    # Top accent bar
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(accent)
    painter.drawRoundedRect(left + 8, top + 14, width - 24, 6, 2, 2)

    # Draw Glyph
    _draw_extension_glyph(
        painter,
        glyph_kind=glyph_kind,
        left=left,
        top=top,
        width=width,
        height=height,
    )

    # Extension label chip
    label_font = QFont("Segoe UI", 7, QFont.Weight.Bold)
    painter.setFont(label_font)
    metrics = QFontMetrics(label_font)
    label_text = label[:4]

    tw = metrics.horizontalAdvance(label_text)
    cw = max(20, tw + 8)
    ch = 12
    cx = left + 6
    cy = top + height - ch - 6

    painter.setBrush(accent)
    painter.drawRoundedRect(cx, cy, cw, ch, 4, 4)

    painter.setPen(QColor("#ffffff"))
    painter.drawText(cx, cy, cw, ch, Qt.AlignmentFlag.AlignCenter, label_text)

    if status != "complete":
        painter.setPen(QPen(border, 1.5))
        painter.setBrush(QColor("#feebc8"))
        painter.drawEllipse(left - 4, top - 4, 12, 12)
        painter.setPen(QColor(border))
        painter.setFont(QFont("Arial", 8, QFont.Weight.Bold))
        painter.drawText(
            left - 4, top - 4, 12, 12, Qt.AlignmentFlag.AlignCenter, warn_glyph
        )

    painter.end()
    return QIcon(pix)
