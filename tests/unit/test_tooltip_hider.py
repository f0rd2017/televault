"""The app-wide tooltip hider must dismiss a visible tooltip on mouse move so
hints don't linger over the same icon/button after the pointer starts moving."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QToolTip

from app.ui.window_main import _TooltipMouseMoveHider


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _move_event() -> QMouseEvent:
    return QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(5, 5),
        QPointF(5, 5),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )


def test_hides_visible_tooltip_on_mouse_move(monkeypatch):
    _app()
    hidden = {"count": 0}
    monkeypatch.setattr(QToolTip, "isVisible", staticmethod(lambda: True))
    monkeypatch.setattr(
        QToolTip, "hideText", staticmethod(lambda: hidden.__setitem__("count", 1))
    )
    f = _TooltipMouseMoveHider()
    assert f.eventFilter(None, _move_event()) is False
    assert hidden["count"] == 1


def test_no_hide_when_no_tooltip_visible(monkeypatch):
    _app()
    hidden = {"count": 0}
    monkeypatch.setattr(QToolTip, "isVisible", staticmethod(lambda: False))
    monkeypatch.setattr(
        QToolTip, "hideText", staticmethod(lambda: hidden.__setitem__("count", 1))
    )
    f = _TooltipMouseMoveHider()
    f.eventFilter(None, _move_event())
    assert hidden["count"] == 0


def test_ignores_non_move_events(monkeypatch):
    _app()
    hidden = {"count": 0}
    # Even if a tooltip is visible, a non-move event must not hide it.
    monkeypatch.setattr(QToolTip, "isVisible", staticmethod(lambda: True))
    monkeypatch.setattr(
        QToolTip, "hideText", staticmethod(lambda: hidden.__setitem__("count", 1))
    )
    f = _TooltipMouseMoveHider()
    paint = QEvent(QEvent.Type.Paint)
    assert f.eventFilter(None, paint) is False
    assert hidden["count"] == 0


def test_filter_never_swallows_the_event(monkeypatch):
    # The filter must always return False so the event keeps propagating
    # (hiding the tooltip must not block clicks/moves reaching widgets).
    _app()
    monkeypatch.setattr(QToolTip, "isVisible", staticmethod(lambda: True))
    monkeypatch.setattr(QToolTip, "hideText", staticmethod(lambda: None))
    f = _TooltipMouseMoveHider()
    assert f.eventFilter(None, _move_event()) is False
