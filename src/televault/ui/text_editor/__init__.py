"""Multi-tab IDE-style editor for text/code files, straight from the cloud.

Supports:
- Comfortable work with many files in one window (tabs, Ctrl+W, Ctrl+Tab/Ctrl+Shift+Tab).
- Persisting font zoom across sessions (QSettings).
- Syntax highlighting (Python, JS/TS, HTML/XML, CSS, JSON, SQL, Markdown, YAML/TOML, Shell/Bash, C/C++).
- Advanced find & replace (Ctrl+F / Ctrl+H) with case-sensitivity, whole-word matching and Regex
  (replacement supports $1/\\1 groups; "Replace all" always moves strictly forward — never loops).
- IDE keyboard shortcuts: toggle comment (Ctrl+/), duplicate line (Ctrl+D), move
  lines (Alt+Up/Down), delete line (Ctrl+Shift+K), indent/dedent (Tab/Shift+Tab),
  auto-indent on Enter, go to line (Ctrl+G).
- Code auto-formatting (JSON / trim whitespace).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QRect, QRegularExpression, QSettings, QSize, Qt, QUrl
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QShortcut,
    QTextCursor,
    QTextDocument,
    QTextFormat,
)
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from televault.ui.text_editor._highlighter import CodeHighlighter

# The single main editor window, for multi-tab support
_MAIN_EDITOR_WINDOW: TextEditorWindow | None = None

# Guard against trying to open a huge file as text
_MAX_EDIT_BYTES = 8 * 1024 * 1024

# Font size bounds and default (pt)
_MIN_FONT_PT = 6
_MAX_FONT_PT = 48
_DEFAULT_FONT_PT = 11

# Dark theme palette (VS Code Dark+)
_BG = "#1e1e1e"
_BG_BAR = "#252526"
_BG_TAB = "#2d2d2d"
_BG_TAB_ACTIVE = "#1e1e1e"
_FG = "#d4d4d4"
_FG_MUTED = "#9da5b4"
_LINE_HL = "#2a2d2e"
_LINENO = "#6e7681"
_LINENO_CUR = "#c6c6c6"
_SEL_BG = "#264f78"
_ACCENT = "#0e639c"
_ACCENT_HOVER = "#1177bb"


def _get_saved_font_size() -> int:
    settings = QSettings("TeleVault", "TextEditor")
    val = settings.value("fontSize", _DEFAULT_FONT_PT)
    try:
        if val is None:
            return _DEFAULT_FONT_PT
        return max(_MIN_FONT_PT, min(_MAX_FONT_PT, int(str(val))))
    except (ValueError, TypeError):
        return _DEFAULT_FONT_PT


def _save_font_size(size: int) -> None:
    settings = QSettings("TeleVault", "TextEditor")
    settings.setValue("fontSize", int(size))


def _expand_regex_groups(template: str, match) -> str:
    """Substitutes groups into the regex-mode replacement: ``$1``/``\\1`` (and
    ``$0`` — the whole match). Non-existent groups are left as-is."""
    out: list[str] = []
    i = 0
    while i < len(template):
        ch = template[i]
        if ch in ("$", "\\") and i + 1 < len(template) and template[i + 1].isdigit():
            j = i + 1
            while j < len(template) and template[j].isdigit():
                j += 1
            idx = int(template[i + 1 : j])
            if idx <= match.lastCapturedIndex():
                out.append(match.captured(idx))
                i = j
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _human_size(n: int) -> str:
    size = float(max(0, int(n)))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(n)} B"


# ── Line-numbering component ──────────────────────────────────────────────────


class _LineNumberArea(QWidget):
    def __init__(self, editor: CodeEditor) -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802
        self._editor.paint_line_numbers(event)


# ── Advanced code editor (CodeEditor) ───────────────────────────────────


class CodeEditor(QPlainTextEdit):
    _INDENT = "    "

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._gutter = _LineNumberArea(self)
        self.blockCountChanged.connect(lambda _=0: self._update_gutter_width())
        self.updateRequest.connect(self._update_gutter_area)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_gutter_width()
        self._highlight_current_line()
        self._comment_prefix = "# "

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            if key == Qt.Key.Key_Backtab or (
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            ):
                self.dedent_selection()
            elif self.textCursor().hasSelection():
                self.indent_selection()
            else:
                self.textCursor().insertText(self._INDENT)
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Auto-indent: the new line inherits the current line's leading
            # whitespace/tabs (up to the cursor position — pressing Enter in
            # the middle of the indent doesn't duplicate it).
            cursor = self.textCursor()
            before = cursor.block().text()[: cursor.positionInBlock()]
            indent = before[: len(before) - len(before.lstrip())]
            super().keyPressEvent(event)
            if indent:
                self.textCursor().insertText(indent)
            return
        super().keyPressEvent(event)

    def set_comment_prefix(self, prefix: str) -> None:
        self._comment_prefix = prefix

    # ---- line numbering ---- #
    def line_number_area_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 18 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter_width(self) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_gutter_area(self, rect: QRect, dy: int) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._gutter.setGeometry(
            QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height())
        )

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self._gutter)
        painter.fillRect(event.rect(), QColor(_BG))
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        offset = self.contentOffset()
        top = round(self.blockBoundingGeometry(block).translated(offset).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        current = self.textCursor().blockNumber()
        line_h = self.fontMetrics().height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                is_cur = block_number == current
                painter.setPen(QColor(_LINENO_CUR if is_cur else _LINENO))
                font = painter.font()
                font.setBold(is_cur)
                painter.setFont(font)
                painter.drawText(
                    0,
                    top,
                    self._gutter.width() - 8,
                    line_h,
                    Qt.AlignmentFlag.AlignRight,
                    str(block_number + 1),
                )
            block = block.next()
            if not block.isValid():
                break
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            block_number += 1

    # ---- current line highlight ---- #
    def _highlight_current_line(self) -> None:
        selections: list[QTextEdit.ExtraSelection] = []
        if not self.isReadOnly():
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor(_LINE_HL))
            sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            sel.cursor = self.textCursor()
            sel.cursor.clearSelection()
            selections.append(sel)
        self.setExtraSelections(selections)

    # ---- font zoom ---- #
    def apply_font_size(self, size: int) -> None:
        size = max(_MIN_FONT_PT, min(_MAX_FONT_PT, int(size)))
        font = self.font()
        font.setPointSize(size)
        self.setFont(font)
        self.document().setDefaultFont(font)
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
        self._update_gutter_width()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta and _MAIN_EDITOR_WINDOW is not None:
                _MAIN_EDITOR_WINDOW.change_global_font_size(1 if delta > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    # ---- IDE keyboard shortcuts ---- #
    def toggle_comment(self) -> None:
        cursor = self.textCursor()
        cursor.beginEditBlock()
        start = cursor.selectionStart()
        end = cursor.selectionEnd()

        doc = self.document()
        start_block = doc.findBlock(start)
        end_block = doc.findBlock(max(start, end - 1 if end > start else end))

        prefix = self._comment_prefix
        all_commented = True
        block = start_block
        while block.isValid():
            text = block.text().strip()
            if text and not text.startswith(prefix.strip()):
                all_commented = False
                break
            if block == end_block:
                break
            block = block.next()

        block = start_block
        while block.isValid():
            c = QTextCursor(block)
            if all_commented:
                idx = block.text().find(prefix.strip())
                if idx != -1:
                    c.setPosition(block.position() + idx)
                    c.movePosition(
                        QTextCursor.MoveOperation.Right,
                        QTextCursor.MoveMode.KeepAnchor,
                        len(prefix.strip()),
                    )
                    if c.selectedText().endswith(" "):
                        c.movePosition(
                            QTextCursor.MoveOperation.Right,
                            QTextCursor.MoveMode.KeepAnchor,
                            1,
                        )
                    c.removeSelectedText()
            else:
                c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                c.insertText(prefix)
            if block == end_block:
                break
            block = block.next()

        cursor.endEditBlock()

    def _selected_block_range(self) -> tuple[int, int]:
        """Numbers of the first and last block touched by the selection/cursor."""
        cursor = self.textCursor()
        doc = self.document()
        start = cursor.selectionStart()
        end = cursor.selectionEnd()
        first = doc.findBlock(start).blockNumber()
        last = doc.findBlock(end - 1 if end > start else end).blockNumber()
        return first, last

    def indent_selection(self) -> None:
        first, last = self._selected_block_range()
        doc = self.document()
        cursor = self.textCursor()
        cursor.beginEditBlock()
        for n in range(first, last + 1):
            block = doc.findBlockByNumber(n)
            c = QTextCursor(block)
            c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            c.insertText(self._INDENT)
        cursor.endEditBlock()

    def dedent_selection(self) -> None:
        first, last = self._selected_block_range()
        doc = self.document()
        cursor = self.textCursor()
        cursor.beginEditBlock()
        for n in range(first, last + 1):
            block = doc.findBlockByNumber(n)
            text = block.text()
            remove = 0
            if text.startswith("\t"):
                remove = 1
            else:
                while remove < len(self._INDENT) and text[remove : remove + 1] == " ":
                    remove += 1
            if remove:
                c = QTextCursor(block)
                c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                c.movePosition(
                    QTextCursor.MoveOperation.Right,
                    QTextCursor.MoveMode.KeepAnchor,
                    remove,
                )
                c.removeSelectedText()
        cursor.endEditBlock()

    def duplicate_line(self) -> None:
        cursor = self.textCursor()
        cursor.beginEditBlock()
        if cursor.hasSelection():
            text = cursor.selectedText()
            cursor.insertText(text + text)
        else:
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.movePosition(
                QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor
            )
            line = cursor.selectedText()
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            cursor.insertText("\n" + line)
        cursor.endEditBlock()

    def move_line(self, up: bool) -> None:
        # We swap the TEXT of two adjacent lines without touching the
        # newlines — the previous implementation (cut+paste with '\n') added
        # an extra blank line when moving the last line of the file.
        cursor = self.textCursor()
        block = cursor.block()
        other = block.previous() if up else block.next()
        if not other.isValid():
            return
        column = cursor.positionInBlock()
        text_cur = block.text()
        text_other = other.text()
        n_cur = block.blockNumber()
        n_other = other.blockNumber()
        doc = self.document()

        cursor.beginEditBlock()
        for number, new_text in ((n_other, text_cur), (n_cur, text_other)):
            b = doc.findBlockByNumber(number)
            c = QTextCursor(b)
            c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            c.movePosition(
                QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor
            )
            c.insertText(new_text)
        cursor.endEditBlock()

        # The cursor follows the moved line (same column).
        target = doc.findBlockByNumber(n_other)
        follow = self.textCursor()
        follow.setPosition(target.position() + min(column, len(text_cur)))
        self.setTextCursor(follow)

    def delete_line(self) -> None:
        cursor = self.textCursor()
        cursor.beginEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor
        )
        cursor.removeSelectedText()
        cursor.deleteChar()
        cursor.endEditBlock()


# ── Editor tab (TextEditorTab) ──────────────────────────────────────────


class TextEditorTab(QWidget):
    """A single open file (tab)."""

    def __init__(
        self,
        url: str,
        title: str,
        on_save: Callable[[bytes], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.url = url
        self.base_title = title
        self.on_save = on_save
        self.loaded = False
        self.dirty = False
        self.encoding = "utf-8"
        self.size_bytes = 0

        ext = Path(title).suffix.strip().lower().lstrip(".")
        self.lang = ext or "text"

        self.editor = CodeEditor(self)
        self.editor.setReadOnly(True)
        self.editor.setPlaceholderText(self.tr("⏳ Loading file…"))
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setFrameShape(QFrame.Shape.NoFrame)
        # The font will be applied globally when the tab is added
        font = QFont("Consolas", _DEFAULT_FONT_PT)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.editor.setFont(font)
        self.editor.document().setDefaultFont(font)

        self.highlighter = CodeHighlighter(self.editor.document(), self.lang)
        self._setup_comment_prefix()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.editor)

        self.nam = QNetworkAccessManager(self)
        self._too_big = False
        self.reply: QNetworkReply | None = self.nam.get(QNetworkRequest(QUrl(url)))
        if self.reply is not None:
            self.reply.finished.connect(self._on_loaded)
            self.reply.downloadProgress.connect(self._on_download_progress)

    def _on_download_progress(self, received: int, _total: int) -> None:
        # Abort downloading a huge file IMMEDIATELY, not after it's fully
        # loaded into RAM: previously a 2GB file would arrive in memory in
        # full and only then get rejected.
        if received > _MAX_EDIT_BYTES and self.reply is not None and not self._too_big:
            self._too_big = True
            self.reply.abort()

    def _show_load_error(self, message: str) -> None:
        """The error is shown in the tab ITSELF (not just the status bar — it's
        shared across the whole window and gets overwritten by other tabs'
        activity)."""
        self.editor.setPlaceholderText(f"⚠ {message}")
        if _MAIN_EDITOR_WINDOW:
            _MAIN_EDITOR_WINDOW.update_active_status(message)

    def _setup_comment_prefix(self) -> None:
        if self.lang in (
            "python",
            "py",
            "yaml",
            "yml",
            "toml",
            "ini",
            "shell",
            "bash",
            "sh",
        ):
            self.editor.set_comment_prefix("# ")
        elif self.lang in ("html", "xml"):
            self.editor.set_comment_prefix("<!-- ")
        else:
            self.editor.set_comment_prefix("// ")

    def _on_loaded(self) -> None:
        reply = self.reply
        self.reply = None
        if reply is None:
            return
        try:
            if self._too_big:
                self._show_load_error(
                    self.tr("File too large for the editor (limit {0} MB).").format(
                        _MAX_EDIT_BYTES // (1024 * 1024)
                    )
                )
                return
            if reply.error() != QNetworkReply.NetworkError.NoError:
                self._show_load_error(
                    self.tr("Loading error: {0}").format(reply.errorString())
                )
                return
            raw = bytes(reply.readAll().data())
        finally:
            reply.deleteLater()

        if len(raw) > _MAX_EDIT_BYTES:
            self._show_load_error(
                self.tr("File too large: {0} MB (limit {1} MB).").format(
                    len(raw) // (1024 * 1024), _MAX_EDIT_BYTES // (1024 * 1024)
                )
            )
            return

        text = None
        for enc in ("utf-8", "cp1251", "latin-1"):
            try:
                text = raw.decode(enc)
                self.encoding = enc
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            self._show_load_error(self.tr("Could not read as text (binary file?)."))
            return

        self.size_bytes = len(raw)
        self.editor.setReadOnly(False)
        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)
        self.editor.document().setModified(False)
        self.editor.setPlaceholderText("")
        # Force a geometry recalculation and viewport repaint — the same
        # operation zooming performs, which "woke up" the rendering.
        # Without this, after an async load the text is sometimes not
        # visible until you manually change the font size.
        font_size = (
            _MAIN_EDITOR_WINDOW._current_font_size
            if _MAIN_EDITOR_WINDOW
            else self.editor.font().pointSize()
        )
        self.editor.apply_font_size(font_size)
        self.editor.viewport().update()
        self.loaded = True
        self.dirty = False
        self.editor.setFocus()
        if _MAIN_EDITOR_WINDOW:
            _MAIN_EDITOR_WINDOW.on_tab_state_changed(self)


# ── Main multi-tab editor window (TextEditorWindow) ─────────────────


class TextEditorWindow(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Cloud File Editor"))
        self.resize(1080, 780)
        self._current_font_size = _get_saved_font_size()

        # ---- Tabs ---- #
        self._tabs = QTabWidget(self)
        self._tabs.setDocumentMode(True)
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.tabCloseRequested.connect(self.close_tab)
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # ---- toolbar ---- #
        self._save_btn = QPushButton(self.tr("Save to cloud ☁"), self)
        self._save_btn.setObjectName("saveBtn")
        self._save_btn.setEnabled(False)
        self._save_btn.setToolTip(self.tr("Save the active file to the cloud (Ctrl+S)"))

        self._undo_btn = self._tool_button("↶", self.tr("Undo (Ctrl+Z)"))
        self._redo_btn = self._tool_button("↷", self.tr("Redo (Ctrl+Y)"))
        self._undo_btn.setEnabled(False)
        self._redo_btn.setEnabled(False)

        self._zoom_out_btn = self._tool_button(
            "A−", self.tr("Decrease font size (Ctrl+-)")
        )
        self._zoom_label = QLabel(f"{self._current_font_size} pt", self)
        self._zoom_label.setObjectName("statusLbl")
        self._zoom_reset_btn = self._tool_button("100%", self.tr("Reset zoom (Ctrl+0)"))
        self._zoom_in_btn = self._tool_button(
            "A+", self.tr("Increase font size (Ctrl++)")
        )

        self._find_btn = self._tool_button(self.tr("Find"), self.tr("Find (Ctrl+F)"))
        self._find_btn.setCheckable(True)
        self._replace_btn = self._tool_button(
            self.tr("Replace"), self.tr("Find and replace (Ctrl+H)")
        )
        self._replace_btn.setCheckable(True)

        self._format_btn = self._tool_button(
            self.tr("Format"), self.tr("Auto-format / Trim whitespace")
        )
        self._wrap_chk = QCheckBox(self.tr("Line wrap"), self)
        self._wrap_chk.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._status = QLabel(self.tr("Ready"), self)
        self._status.setObjectName("statusLbl")

        top = QHBoxLayout()
        top.setContentsMargins(10, 6, 10, 6)
        top.setSpacing(6)
        top.addWidget(self._save_btn)
        top.addWidget(self._separator())
        top.addWidget(self._undo_btn)
        top.addWidget(self._redo_btn)
        top.addWidget(self._separator())
        top.addWidget(self._zoom_out_btn)
        top.addWidget(self._zoom_label)
        top.addWidget(self._zoom_reset_btn)
        top.addWidget(self._zoom_in_btn)
        top.addWidget(self._separator())
        top.addWidget(self._find_btn)
        top.addWidget(self._replace_btn)
        top.addWidget(self._format_btn)
        top.addWidget(self._wrap_chk)
        top.addStretch(1)
        top.addWidget(self._status)
        toolbar = QWidget(self)
        toolbar.setObjectName("toolbar")
        toolbar.setLayout(top)

        # ---- find & replace bar ---- #
        self._find_edit = QLineEdit(self)
        self._find_edit.setPlaceholderText(self.tr("Find…"))
        self._find_edit.setClearButtonEnabled(True)

        self._replace_edit = QLineEdit(self)
        self._replace_edit.setPlaceholderText(self.tr("Replace with…"))
        self._replace_edit.setClearButtonEnabled(True)

        self._case_chk = QCheckBox("Aa", self)
        self._case_chk.setToolTip(self.tr("Case sensitive"))
        self._words_chk = QCheckBox(r"\b", self)
        self._words_chk.setToolTip(self.tr("Whole word"))
        self._regex_chk = QCheckBox(".*", self)
        self._regex_chk.setToolTip(self.tr("Regular expression (Regex)"))

        self._find_prev_btn = self._tool_button("↑", self.tr("Previous (Shift+Enter)"))
        self._find_next_btn = self._tool_button("↓", self.tr("Next (Enter)"))
        self._do_replace_btn = self._tool_button(
            self.tr("Replace"), self.tr("Replace the current match")
        )
        self._replace_all_btn = self._tool_button(
            self.tr("Replace all"), self.tr("Replace all matches")
        )
        self._find_close_btn = self._tool_button("✕", self.tr("Close (Esc)"))
        self._find_info = QLabel("", self)
        self._find_info.setObjectName("statusLbl")

        find_top = QHBoxLayout()
        find_top.setContentsMargins(10, 6, 10, 2)
        find_top.setSpacing(6)
        find_top.addWidget(self._find_edit, 1)
        find_top.addWidget(self._case_chk)
        find_top.addWidget(self._words_chk)
        find_top.addWidget(self._regex_chk)
        find_top.addWidget(self._find_prev_btn)
        find_top.addWidget(self._find_next_btn)
        find_top.addWidget(self._find_info)
        find_top.addWidget(self._find_close_btn)

        self._replace_layout_widget = QWidget(self)
        replace_top = QHBoxLayout(self._replace_layout_widget)
        replace_top.setContentsMargins(10, 2, 10, 6)
        replace_top.setSpacing(6)
        replace_top.addWidget(self._replace_edit, 1)
        replace_top.addWidget(self._do_replace_btn)
        replace_top.addWidget(self._replace_all_btn)
        replace_top.addStretch(1)

        find_box = QVBoxLayout()
        find_box.setContentsMargins(0, 0, 0, 0)
        find_box.setSpacing(0)
        find_box.addLayout(find_top)
        find_box.addWidget(self._replace_layout_widget)

        self._find_bar = QWidget(self)
        self._find_bar.setObjectName("findbar")
        self._find_bar.setLayout(find_box)
        self._find_bar.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(toolbar)
        layout.addWidget(self._find_bar)
        layout.addWidget(self._tabs, 1)

        self._apply_dark_theme()

        # ---- button signals ---- #
        self._save_btn.clicked.connect(self._save_active)
        self._undo_btn.clicked.connect(self._undo_active)
        self._redo_btn.clicked.connect(self._redo_active)
        self._zoom_in_btn.clicked.connect(lambda: self.change_global_font_size(1))
        self._zoom_out_btn.clicked.connect(lambda: self.change_global_font_size(-1))
        self._zoom_reset_btn.clicked.connect(lambda: self.reset_global_font_size())
        self._find_btn.toggled.connect(self._on_find_toggled)
        self._replace_btn.toggled.connect(self._on_replace_toggled)
        self._format_btn.clicked.connect(self._format_code)
        self._wrap_chk.toggled.connect(self._on_wrap_toggled)

        self._find_edit.returnPressed.connect(lambda: self._find(forward=True))
        self._find_edit.textChanged.connect(
            lambda _=None: self._find(forward=True, incremental=True)
        )
        self._case_chk.toggled.connect(
            lambda _=None: self._find(forward=True, incremental=True)
        )
        self._words_chk.toggled.connect(
            lambda _=None: self._find(forward=True, incremental=True)
        )
        self._regex_chk.toggled.connect(
            lambda _=None: self._find(forward=True, incremental=True)
        )
        self._find_next_btn.clicked.connect(lambda: self._find(forward=True))
        self._find_prev_btn.clicked.connect(lambda: self._find(forward=False))
        self._do_replace_btn.clicked.connect(self._replace_one)
        self._replace_all_btn.clicked.connect(self._replace_all)
        self._find_close_btn.clicked.connect(self._close_find)

        # ---- keyboard shortcuts ---- #
        self._add_shortcut(QKeySequence.StandardKey.Save, self._save_active)
        self._add_shortcut(QKeySequence("Ctrl+Shift+S"), self._save_all)
        self._add_shortcut(QKeySequence.StandardKey.Close, self.close_active_tab)
        self._add_shortcut(QKeySequence("Ctrl+W"), self.close_active_tab)
        self._add_shortcut(
            QKeySequence.StandardKey.ZoomIn, lambda: self.change_global_font_size(1)
        )
        self._add_shortcut(
            QKeySequence.StandardKey.ZoomOut, lambda: self.change_global_font_size(-1)
        )
        self._add_shortcut(
            QKeySequence("Ctrl++"), lambda: self.change_global_font_size(1)
        )
        self._add_shortcut(
            QKeySequence("Ctrl+="), lambda: self.change_global_font_size(1)
        )
        self._add_shortcut(
            QKeySequence("Ctrl+0"), lambda: self.reset_global_font_size()
        )
        self._add_shortcut(
            QKeySequence.StandardKey.Find, lambda: self._find_btn.setChecked(True)
        )
        self._add_shortcut(
            QKeySequence("Ctrl+H"), lambda: self._replace_btn.setChecked(True)
        )
        # returnPressed on _find_edit only ever searches forward, so Shift+Enter
        # (advertised by the "Previous" button's tooltip) needs its own shortcut.
        self._add_shortcut(
            QKeySequence("Shift+Return"), lambda: self._find(forward=False)
        )
        self._add_shortcut(
            QKeySequence("Shift+Enter"), lambda: self._find(forward=False)
        )
        self._add_shortcut(QKeySequence("Ctrl+/"), self._active_toggle_comment)
        self._add_shortcut(QKeySequence("Ctrl+D"), self._active_duplicate_line)
        self._add_shortcut(
            QKeySequence("Alt+Up"), lambda: self._active_move_line(up=True)
        )
        self._add_shortcut(
            QKeySequence("Alt+Down"), lambda: self._active_move_line(up=False)
        )
        self._add_shortcut(QKeySequence("Ctrl+Shift+K"), self._active_delete_line)
        self._add_shortcut(QKeySequence("Ctrl+Y"), self._redo_active)
        self._add_shortcut(QKeySequence("Ctrl+G"), self._goto_line)
        self._add_shortcut(QKeySequence("Ctrl+Tab"), lambda: self._cycle_tab(1))
        self._add_shortcut(QKeySequence("Ctrl+Shift+Tab"), lambda: self._cycle_tab(-1))
        self._add_shortcut(QKeySequence(Qt.Key.Key_Escape), self._on_escape)

    def active_tab(self) -> TextEditorTab | None:
        widget = self._tabs.currentWidget()
        return widget if isinstance(widget, TextEditorTab) else None

    def open_file_tab(
        self, url: str, title: str, on_save: Callable[[bytes], None]
    ) -> None:
        # If this file is already open, switch to it
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if isinstance(tab, TextEditorTab) and tab.url == url:
                self._tabs.setCurrentIndex(i)
                return

        tab = TextEditorTab(url, title, on_save, self)
        tab.editor.apply_font_size(self._current_font_size)
        tab.editor.textChanged.connect(lambda t=tab: self._on_tab_text_changed(t))
        tab.editor.cursorPositionChanged.connect(
            lambda t=tab: self._on_tab_cursor_changed(t)
        )

        idx = self._tabs.addTab(tab, title)
        self._tabs.setCurrentIndex(idx)

    def close_tab(self, index: int) -> None:
        tab = self._tabs.widget(index)
        if isinstance(tab, TextEditorTab) and tab.loaded and tab.dirty:
            resp = QMessageBox.question(
                self,
                self.tr("Unsaved changes"),
                self.tr(
                    "The file '{0}' has unsaved changes. Save before closing?"
                ).format(tab.base_title),
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if resp == QMessageBox.StandardButton.Cancel:
                return
            if resp == QMessageBox.StandardButton.Save:
                self._save_tab(tab)

        self._tabs.removeTab(index)
        if self._tabs.count() == 0:
            self.hide()

    def close_active_tab(self) -> None:
        idx = self._tabs.currentIndex()
        if idx >= 0:
            self.close_tab(idx)

    def _cycle_tab(self, step: int) -> None:
        count = self._tabs.count()
        if count > 1:
            self._tabs.setCurrentIndex((self._tabs.currentIndex() + step) % count)

    def _goto_line(self) -> None:
        tab = self.active_tab()
        if not tab or not tab.loaded:
            return
        from PySide6.QtWidgets import QInputDialog

        total = tab.editor.blockCount()
        line, ok = QInputDialog.getInt(
            self,
            self.tr("Go to line"),
            self.tr("Line (1–{0}):").format(total),
            tab.editor.textCursor().blockNumber() + 1,
            1,
            total,
        )
        if not ok:
            return
        block = tab.editor.document().findBlockByNumber(line - 1)
        cursor = tab.editor.textCursor()
        cursor.setPosition(block.position())
        tab.editor.setTextCursor(cursor)
        tab.editor.centerCursor()
        tab.editor.setFocus()

    def change_global_font_size(self, step: int) -> None:
        self._current_font_size = max(
            _MIN_FONT_PT, min(_MAX_FONT_PT, self._current_font_size + step)
        )
        _save_font_size(self._current_font_size)
        self._zoom_label.setText(f"{self._current_font_size} pt")
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if isinstance(tab, TextEditorTab):
                tab.editor.apply_font_size(self._current_font_size)

    def reset_global_font_size(self) -> None:
        self._current_font_size = _DEFAULT_FONT_PT
        _save_font_size(self._current_font_size)
        self._zoom_label.setText(f"{self._current_font_size} pt")
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if isinstance(tab, TextEditorTab):
                tab.editor.apply_font_size(self._current_font_size)

    # ---- state handling ---- #
    def _on_tab_changed(self, index: int) -> None:
        tab = self.active_tab()
        if tab:
            self.on_tab_state_changed(tab)
        else:
            self._save_btn.setEnabled(False)
            self._undo_btn.setEnabled(False)
            self._redo_btn.setEnabled(False)
            self._status.setText(self.tr("No files open"))

    def _on_tab_text_changed(self, tab: TextEditorTab) -> None:
        if not tab.loaded:
            return
        if not tab.dirty:
            tab.dirty = True
            idx = self._tabs.indexOf(tab)
            if idx >= 0:
                self._tabs.setTabText(idx, f"*{tab.base_title}")
        if tab == self.active_tab():
            self._save_btn.setEnabled(True)
            self._undo_btn.setEnabled(tab.editor.document().isUndoAvailable())
            self._redo_btn.setEnabled(tab.editor.document().isRedoAvailable())
            self._update_tab_status(tab)

    def _on_tab_cursor_changed(self, tab: TextEditorTab) -> None:
        if tab == self.active_tab():
            self._update_tab_status(tab)

    def on_tab_state_changed(self, tab: TextEditorTab) -> None:
        self._save_btn.setEnabled(tab.dirty)
        self._undo_btn.setEnabled(tab.editor.document().isUndoAvailable())
        self._redo_btn.setEnabled(tab.editor.document().isRedoAvailable())
        self._wrap_chk.setChecked(
            tab.editor.lineWrapMode() == QPlainTextEdit.LineWrapMode.WidgetWidth
        )
        self._update_tab_status(tab)

    def update_active_status(self, msg: str) -> None:
        self._status.setText(msg)

    def _update_tab_status(self, tab: TextEditorTab) -> None:
        if not tab.loaded:
            self._status.setText(self.tr("Loading…"))
            return
        cursor = tab.editor.textCursor()
        line = cursor.blockNumber() + 1
        col = cursor.positionInBlock() + 1
        lines = tab.editor.blockCount()
        # NOT toPlainText(): that's a full copy of the text on EVERY cursor
        # move — on a large file the status bar turned navigation into a
        # slog. O(1):
        chars = max(0, tab.editor.document().characterCount() - 1)
        dot = "● " if tab.dirty else ""

        sel_info = ""
        if cursor.hasSelection():
            sel_chars = len(cursor.selectedText())
            sel_info = self.tr("   •   Selected: {0} chars").format(sel_chars)

        lang_badge = tab.lang.upper()
        self._status.setText(
            self.tr(
                "{0}Ln {1}, Col {2}{3}   •   {4} lines, {5} chars"
                "   •   {6}   •   {7}   •   {8}"
            ).format(
                dot,
                line,
                col,
                sel_info,
                lines,
                chars,
                lang_badge,
                tab.encoding,
                _human_size(tab.size_bytes),
            )
        )

    # ---- editor actions on the active tab ---- #
    def _save_tab(self, tab: TextEditorTab) -> None:
        if not tab.loaded or not tab.dirty:
            return
        text = tab.editor.toPlainText()
        try:
            data = text.encode(tab.encoding)
        except Exception:
            data = text.encode("utf-8")
        try:
            tab.on_save(data)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self,
                self.tr("Editor"),
                self.tr("Failed to save '{0}':\n{1}").format(tab.base_title, exc),
            )
            return
        tab.dirty = False
        tab.size_bytes = len(data)
        tab.editor.document().setModified(False)
        idx = self._tabs.indexOf(tab)
        if idx >= 0:
            self._tabs.setTabText(idx, tab.base_title)
        if tab == self.active_tab():
            self._save_btn.setEnabled(False)
            # To be precise: on_save only QUEUES the upload job, it doesn't
            # wait for it to finish.
            self._status.setText(self.tr("Queued for upload to the cloud ⬆"))

    def _save_active(self) -> None:
        tab = self.active_tab()
        if tab:
            self._save_tab(tab)

    def _save_all(self) -> None:
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if isinstance(tab, TextEditorTab) and tab.dirty:
                self._save_tab(tab)

    def _undo_active(self) -> None:
        tab = self.active_tab()
        if tab:
            tab.editor.undo()

    def _redo_active(self) -> None:
        tab = self.active_tab()
        if tab:
            tab.editor.redo()

    def _active_toggle_comment(self) -> None:
        tab = self.active_tab()
        if tab:
            tab.editor.toggle_comment()

    def _active_duplicate_line(self) -> None:
        tab = self.active_tab()
        if tab:
            tab.editor.duplicate_line()

    def _active_move_line(self, up: bool) -> None:
        tab = self.active_tab()
        if tab:
            tab.editor.move_line(up)

    def _active_delete_line(self) -> None:
        tab = self.active_tab()
        if tab:
            tab.editor.delete_line()

    def _on_wrap_toggled(self, checked: bool) -> None:
        tab = self.active_tab()
        if tab:
            tab.editor.setLineWrapMode(
                QPlainTextEdit.LineWrapMode.WidgetWidth
                if checked
                else QPlainTextEdit.LineWrapMode.NoWrap
            )

    def _format_code(self) -> None:
        tab = self.active_tab()
        if not tab or not tab.loaded:
            return
        text = tab.editor.toPlainText()
        formatted = None

        if tab.lang in ("json",):
            try:
                obj = json.loads(text)
                formatted = json.dumps(obj, ensure_ascii=False, indent=2)
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    self.tr("Format JSON"),
                    self.tr("JSON syntax error:\n{0}").format(exc),
                )
                return
        else:
            lines = [line.rstrip() for line in text.splitlines()]
            formatted = "\n".join(lines) + "\n"

        if formatted is not None and formatted != text:
            cursor = tab.editor.textCursor()
            cursor.beginEditBlock()
            cursor.select(QTextCursor.SelectionType.Document)
            cursor.insertText(formatted)
            cursor.endEditBlock()
            tab.dirty = True
            self._on_tab_text_changed(tab)

    # ---- find & replace ---- #
    def _on_find_toggled(self, checked: bool) -> None:
        if checked:
            self._replace_btn.setChecked(False)
            self._replace_layout_widget.hide()
            self._show_find_bar()
        else:
            if not self._replace_btn.isChecked():
                self._find_bar.hide()
                tab = self.active_tab()
                if tab:
                    tab.editor.setFocus()

    def _on_replace_toggled(self, checked: bool) -> None:
        if checked:
            self._find_btn.setChecked(False)
            self._replace_layout_widget.show()
            self._show_find_bar()
        else:
            if not self._find_btn.isChecked():
                self._find_bar.hide()
                tab = self.active_tab()
                if tab:
                    tab.editor.setFocus()

    def _show_find_bar(self) -> None:
        self._find_bar.show()
        tab = self.active_tab()
        if tab:
            cursor = tab.editor.textCursor()
            if cursor.hasSelection():
                self._find_edit.setText(cursor.selectedText())
        self._find_edit.setFocus()
        self._find_edit.selectAll()

    def _close_find(self) -> None:
        self._find_btn.setChecked(False)
        self._replace_btn.setChecked(False)
        self._find_bar.hide()
        tab = self.active_tab()
        if tab:
            tab.editor.setFocus()

    def _find(self, *, forward: bool, incremental: bool = False) -> bool:
        tab = self.active_tab()
        if not tab or not tab.loaded:
            return False
        needle = self._find_edit.text()
        if not needle:
            self._find_info.setText("")
            return False

        flags = QTextDocument.FindFlag(0)
        if not forward:
            flags |= QTextDocument.FindFlag.FindBackward
        if self._case_chk.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if self._words_chk.isChecked():
            flags |= QTextDocument.FindFlag.FindWholeWords

        if incremental:
            cursor = tab.editor.textCursor()
            cursor.setPosition(min(cursor.selectionStart(), cursor.position()))
            tab.editor.setTextCursor(cursor)

        rx: QRegularExpression | None = None
        if self._regex_chk.isChecked():
            rx = self._build_regex()
            if rx is None:
                self._find_info.setText(self.tr("Invalid Regex"))
                return False

        found = (
            tab.editor.find(rx, flags)
            if rx is not None
            else tab.editor.find(needle, flags)
        )

        if not found:
            # Wrap the search around to the other end of the document (only for
            # interactive search; _replace_all does NOT use this — it does a
            # strict forward pass instead).
            tc = tab.editor.textCursor()
            tc.movePosition(
                QTextCursor.MoveOperation.Start
                if forward
                else QTextCursor.MoveOperation.End
            )
            tab.editor.setTextCursor(tc)
            found = (
                tab.editor.find(rx, flags)
                if rx is not None
                else tab.editor.find(needle, flags)
            )

        self._find_info.setText("" if found else self.tr("no matches"))
        return found

    def _build_regex(self) -> QRegularExpression | None:
        """Build a QRegularExpression from the current search settings (None = invalid)."""
        rx_opts = QRegularExpression.PatternOption.NoPatternOption
        if not self._case_chk.isChecked():
            rx_opts |= QRegularExpression.PatternOption.CaseInsensitiveOption
        rx = QRegularExpression(self._find_edit.text(), rx_opts)
        return rx if rx.isValid() else None

    def _expand_replacement(self, selected_text: str) -> str:
        """Expand regex group refs ($1/\\1) in the replacement, if regex mode is on."""
        replacement = self._replace_edit.text()
        if not self._regex_chk.isChecked():
            return replacement
        rx = self._build_regex()
        if rx is None:
            return replacement
        match = rx.match(selected_text)
        if not match.hasMatch():
            return replacement
        return _expand_regex_groups(replacement, match)

    def _replace_one(self) -> None:
        tab = self.active_tab()
        if not tab or not tab.loaded:
            return
        cursor = tab.editor.textCursor()
        if cursor.hasSelection():
            cursor.insertText(self._expand_replacement(cursor.selectedText()))
            self._on_tab_text_changed(tab)
        self._find(forward=True)

    def _replace_all(self) -> None:
        # NOT via self._find: that wraps the search back to the start of the
        # document, and a replacement containing the search term ("a" -> "aa")
        # would loop forever — past the end of the document it kept finding
        # the text it had just inserted.
        tab = self.active_tab()
        if not tab or not tab.loaded:
            return
        needle = self._find_edit.text()
        if not needle:
            return

        doc = tab.editor.document()
        use_regex = self._regex_chk.isChecked()
        rx = self._build_regex() if use_regex else None
        if use_regex and rx is None:
            self._find_info.setText(self.tr("Invalid Regex"))
            return
        flags = QTextDocument.FindFlag(0)
        if self._case_chk.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if self._words_chk.isChecked():
            flags |= QTextDocument.FindFlag.FindWholeWords

        edit_cursor = QTextCursor(doc)
        edit_cursor.beginEditBlock()
        position = 0
        count = 0
        while True:
            found = (
                doc.find(rx, position, flags)
                if use_regex
                else doc.find(needle, position, flags)
            )
            if found.isNull():
                break
            if found.selectionStart() == found.selectionEnd():
                # Empty match (regex like "x*") — step forward, else infinite loop.
                position = found.selectionEnd() + 1
                continue
            found.insertText(self._expand_replacement(found.selectedText()))
            position = found.position()  # strictly forward — no re-scanning
            count += 1
        edit_cursor.endEditBlock()

        self._find_info.setText(self.tr("Replaced: {0}").format(count))
        if count > 0:
            self._on_tab_text_changed(tab)

    def _on_escape(self) -> None:
        if self._find_bar.isVisible():
            self._close_find()
            return
        self.close()

    # ---- UI-building helpers ---- #
    def _tool_button(self, text: str, tooltip: str) -> QPushButton:
        btn = QPushButton(text, self)
        btn.setObjectName("toolBtn")
        btn.setToolTip(tooltip)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        return btn

    def _separator(self) -> QFrame:
        line = QFrame(self)
        line.setObjectName("vsep")
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFixedWidth(8)
        return line

    def _add_shortcut(
        self, seq: QKeySequence | str | QKeySequence.StandardKey, slot
    ) -> None:
        sc = QShortcut(
            QKeySequence(seq) if isinstance(seq, str) else QKeySequence(seq), self
        )
        sc.activated.connect(slot)

    def _apply_dark_theme(self) -> None:
        self.setStyleSheet(
            f"""
            QWidget {{ background: {_BG}; color: {_FG};
                font-family: "Segoe UI", "Inter", sans-serif; }}
            QWidget#toolbar, QWidget#findbar {{ background: {_BG_BAR};
                border-bottom: 1px solid #141414; }}
            QTabWidget::pane {{ border: none; background: {_BG}; }}
            QTabBar::tab {{ background: {_BG_TAB}; color: {_FG_MUTED}; padding: 7px 14px;
                border-top-left-radius: 4px; border-top-right-radius: 4px; margin-right: 2px; font-size: 12px; }}
            QTabBar::tab:selected {{ background: {_BG_TAB_ACTIVE}; color: #ffffff; font-weight: 600; }}
            QTabBar::tab:hover {{ background: #383838; }}
            QTabBar::close-button {{ image: none; subcontrol-position: right; }}
            QPlainTextEdit {{ background: {_BG}; color: {_FG}; border: none;
                selection-background-color: {_SEL_BG}; selection-color: #ffffff; }}
            QLabel#statusLbl {{ color: {_FG_MUTED}; padding-right: 6px; font-size: 11px; }}
            QLineEdit {{ background: #1e1e1e; color: {_FG};
                border: 1px solid #3c3c3c; border-radius: 4px; padding: 4px 8px; font-size: 12px; }}
            QLineEdit:focus {{ border: 1px solid {_ACCENT_HOVER}; }}
            QPushButton#saveBtn {{ background: {_ACCENT}; color: #ffffff; border: none;
                padding: 6px 14px; border-radius: 4px; font-weight: 600; }}
            QPushButton#saveBtn:hover {{ background: {_ACCENT_HOVER}; }}
            QPushButton#saveBtn:disabled {{ background: #33373b; color: #7a7a7a; }}
            QPushButton#toolBtn {{ background: #3a3d41; color: {_FG}; border: none;
                padding: 5px 10px; border-radius: 4px; font-size: 12px; }}
            QPushButton#toolBtn:hover {{ background: #4a4e54; }}
            QPushButton#toolBtn:checked {{ background: {_ACCENT}; color: #ffffff; }}
            QPushButton#toolBtn:disabled {{ color: #6a6a6a; background: #2c2f33; }}
            QCheckBox {{ color: {_FG_MUTED}; padding-left: 4px; font-size: 12px; }}
            QFrame#vsep {{ color: #3c3c3c; }}
            QScrollBar:vertical {{ background: {_BG}; width: 12px; margin: 0; }}
            QScrollBar::handle:vertical {{ background: #3c3c3c; border-radius: 6px;
                min-height: 24px; }}
            QScrollBar::handle:vertical:hover {{ background: #505050; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar:horizontal {{ background: {_BG}; height: 12px; margin: 0; }}
            QScrollBar::handle:horizontal {{ background: #3c3c3c; border-radius: 6px;
                min-width: 24px; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
            """
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        dirty_tabs = []
        for i in range(self._tabs.count()):
            tab = self._tabs.widget(i)
            if isinstance(tab, TextEditorTab) and tab.loaded and tab.dirty:
                dirty_tabs.append(tab)

        if dirty_tabs:
            resp = QMessageBox.question(
                self,
                self.tr("Unsaved Changes"),
                self.tr(
                    "You have {0} unsaved file(s). Save all before closing?"
                ).format(len(dirty_tabs)),
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if resp == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if resp == QMessageBox.StandardButton.Save:
                self._save_all()

        global _MAIN_EDITOR_WINDOW
        _MAIN_EDITOR_WINDOW = None
        super().closeEvent(event)


def open_text_editor(
    parent: QWidget | None,
    *,
    url: str,
    title: str,
    on_save: Callable[[bytes], None],
) -> QWidget:
    global _MAIN_EDITOR_WINDOW
    if _MAIN_EDITOR_WINDOW is None or not _MAIN_EDITOR_WINDOW.isVisible():
        _MAIN_EDITOR_WINDOW = TextEditorWindow()
        _MAIN_EDITOR_WINDOW.show()

    _MAIN_EDITOR_WINDOW.open_file_tab(url, title, on_save)
    _MAIN_EDITOR_WINDOW.raise_()
    _MAIN_EDITOR_WINDOW.activateWindow()
    return _MAIN_EDITOR_WINDOW
