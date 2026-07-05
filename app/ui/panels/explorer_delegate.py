"""Icon-grid item delegate for the file explorer.

Qt's stock item delegate only wraps label text at word boundaries. File names
rarely contain spaces (``photo_2026-06-21.jpg``, ``batch_2026-07-02.zip`` …), so
they couldn't wrap and were elided onto a single line — even though the grid cell
had room for two or three more. This delegate lays the label out with
``WrapAtWordBoundaryOrAnywhere`` so a long, space-less name uses the full height
of the cell before anything is clipped.

Only the label rendering is custom; the cell background/selection highlight is
still drawn by the active style (so the app stylesheet keeps applying), and the
grid cell size comes from the model's ``SizeHintRole`` (unchanged).
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QBrush,
    QColor,
    QIcon,
    QPalette,
    QTextDocument,
    QTextOption,
)
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)


class ExplorerIconDelegate(QStyledItemDelegate):
    _ICON_TOP_MARGIN = 3
    _ICON_TEXT_GAP = 3
    _H_MARGIN = 2

    def paint(self, painter, option, index) -> None:  # noqa: N802 (Qt override)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()

        painter.save()
        # Background + selection/hover: let the style (incl. the app stylesheet)
        # paint the item panel, but without the text/icon — we draw those so the
        # label can wrap at any character.
        opt.text = ""
        opt.icon = QIcon()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        rect = opt.rect
        icon_size = option.decorationSize
        if not icon_size.isValid() or icon_size.width() <= 0:
            icon_size = QSize(56, 56)
        # Reserve a fixed-height icon slot so the label baseline is identical for
        # every item — otherwise a short (landscape) thumbnail would pull its text
        # up and the grid would look ragged.
        slot_h = icon_size.height()

        deco = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(deco, QIcon) and not deco.isNull():
            pm = deco.pixmap(icon_size)
            if not pm.isNull():
                dpr = pm.devicePixelRatio() or 1.0
                pw = pm.width() / dpr
                ph = pm.height() / dpr
                x = rect.left() + (rect.width() - pw) / 2.0
                y = rect.top() + self._ICON_TOP_MARGIN + max(0.0, (slot_h - ph) / 2.0)
                painter.drawPixmap(QRectF(x, y, pw, ph), pm, QRectF(pm.rect()))

        text = index.data(Qt.ItemDataRole.DisplayRole)
        if text:
            top = rect.top() + self._ICON_TOP_MARGIN + slot_h + self._ICON_TEXT_GAP
            text_rect = QRectF(
                rect.left() + self._H_MARGIN,
                top,
                rect.width() - 2 * self._H_MARGIN,
                rect.bottom() - top,
            )
            if text_rect.width() > 1 and text_rect.height() > 1:
                self._paint_label(painter, opt, index, text_rect, str(text))
        painter.restore()

    def _paint_label(
        self,
        painter,
        opt: QStyleOptionViewItem,
        index,
        rect: QRectF,
        text: str,
    ) -> None:
        if opt.state & QStyle.StateFlag.State_Selected:
            color = opt.palette.color(QPalette.ColorRole.HighlightedText)
        else:
            fg = index.data(Qt.ItemDataRole.ForegroundRole)
            if isinstance(fg, QBrush):
                color = fg.color()
            elif isinstance(fg, QColor):
                color = fg
            else:
                color = opt.palette.color(QPalette.ColorRole.Text)

        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        text_option = QTextOption(Qt.AlignmentFlag.AlignHCenter)
        text_option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(text_option)
        doc.setPlainText(text)
        doc.setTextWidth(rect.width())

        painter.save()
        painter.translate(rect.topLeft())
        # Clip to whole lines so an overflowing label is cut between rows, never
        # through the middle of a line of glyphs.
        line_h = self._line_height(doc)
        usable_h = rect.height()
        if line_h > 0:
            usable_h = max(line_h, (int(rect.height() // line_h)) * line_h)
        painter.setClipRect(QRectF(0.0, 0.0, rect.width(), usable_h))
        ctx = QAbstractTextDocumentLayout.PaintContext()
        ctx.palette.setColor(QPalette.ColorRole.Text, color)
        doc.documentLayout().draw(painter, ctx)
        painter.restore()

    @staticmethod
    def _line_height(doc: QTextDocument) -> float:
        block = doc.firstBlock()
        layout = block.layout() if block.isValid() else None
        if layout is not None and layout.lineCount() > 0:
            return float(layout.lineAt(0).height())
        from PySide6.QtGui import QFontMetricsF

        return float(QFontMetricsF(doc.defaultFont()).lineSpacing())

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 (Qt override)
        hint = index.data(Qt.ItemDataRole.SizeHintRole)
        if isinstance(hint, QSize):
            return hint
        return super().sizeHint(option, index)
