from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle

TOOLTIP_WAKEUP_DELAY_MS = 2000

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_CHEVRON_ICON_PATH = (_ASSETS_DIR / "chevron_down.png").as_posix()
_CHEVRON_ICON_HOVER_PATH = (_ASSETS_DIR / "chevron_down_hover.png").as_posix()


class _DelayedTooltipStyle(QProxyStyle):
    """Makes tooltips wait for the cursor to actually stop moving.

    Qt's default ~700ms wake-up delay also lets a "fall asleep" grace
    period chain tooltips together when hopping between adjacent
    widgets, which feels twitchy. We want every tooltip to wait the
    same fixed delay, so the grace period is disabled (set to 0).
    """

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_ToolTip_WakeUpDelay:
            return TOOLTIP_WAKEUP_DELAY_MS
        if hint == QStyle.StyleHint.SH_ToolTip_FallAsleepDelay:
            return 0
        return super().styleHint(hint, option, widget, returnData)


_MAIN_WINDOW_STYLESHEET = """
            QMainWindow, QWidget#mainCentral {
                background: #09090b;
                color: #fafafa;
                font-family: 'Segoe UI Variable Display', 'Segoe UI', 'Inter', 'Roboto', sans-serif;
            }
            
            QStatusBar {
                background: #09090b;
                color: #a1a1aa;
                border-top: 1px solid #27272a;
                padding: 6px 12px;
                font-size: 12px;
            }
            QStatusBar::item { border: none; }

            QFrame#topBar {
                background: #18181b;
                border: 1px solid #27272a;
                border-radius: 12px;
            }
            
            /* Typography */
            QLabel { color: #fafafa; }
            QLabel#panelTitle {
                color: #ffffff;
                font-size: 14px;
                font-weight: 700;
                letter-spacing: 0.5px;
                text-transform: uppercase;
                margin-bottom: 2px;
            }
            QLabel#panelHint {
                color: #71717a;
                font-size: 12px;
            }
            QLabel#emptyStateLabel {
                color: #a1a1aa;
                font-size: 15px;
                font-weight: 500;
                padding: 40px 30px;
                border-radius: 16px;
                border: 2px dashed #27272a;
                background: #09090b;
            }

            /* Buttons */
            QPushButton {
                background: #18181b;
                color: #e4e4e7;
                border: 1px solid #27272a;
                border-radius: 10px;
                padding: 8px 18px;
                font-weight: 600;
                font-size: 13px;
            }
            QPushButton:hover {
                background: #27272a;
                color: #ffffff;
                border: 1px solid #3f3f46;
            }
            QPushButton:pressed {
                background: #09090b;
                border: 1px solid #27272a;
            }
            QPushButton:disabled {
                color: #52525b;
                background: #09090b;
                border: 1px solid #18181b;
            }
            
            QPushButton#navButton, QPushButton#topActionButton {
                background: transparent;
                color: #a1a1aa;
                border: 1px solid transparent;
                border-radius: 10px;
                padding: 0;
                font-size: 15px;
                font-weight: 700;
            }
            QPushButton#navButton:hover, QPushButton#topActionButton:hover {
                background: #27272a;
                color: #ffffff;
                border: 1px solid #3f3f46;
            }
            QPushButton#topActionButton:checked {
                background: #3b0764;
                color: #ffffff;
                border: 1px solid #6d28d9;
            }
            QPushButton#everywhereButton {
                background: transparent;
                color: #a1a1aa;
                border: 1px solid transparent;
                border-radius: 10px;
                padding: 0 14px;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton#everywhereButton:hover {
                background: #27272a;
                color: #ffffff;
                border: 1px solid #3f3f46;
            }
            QPushButton#everywhereButton:checked {
                background: #3b0764;
                color: #ffffff;
                border: 1px solid #6d28d9;
            }
            
            QPushButton#applyFilterButton {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 #6d28d9,
                    stop: 1 #4f46e5
                );
                color: #ffffff;
                border: none;
                border-radius: 10px;
                padding: 8px 18px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#applyFilterButton:hover {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 #7c3aed,
                    stop: 1 #6366f1
                );
                border: none;
            }
            QPushButton#applyFilterButton:pressed {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 #5b21b6,
                    stop: 1 #4338ca
                );
            }

            /* Inputs */
            QLineEdit, QComboBox, QTextEdit {
                background-color: #09090b;
                color: #fafafa;
                border: 1px solid #27272a;
                border-radius: 10px;
                padding: 8px 14px;
                selection-background-color: #4f46e5;
                font-size: 13px;
            }
            QLineEdit:hover, QComboBox:hover, QTextEdit:hover {
                border: 1px solid #3f3f46;
            }
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus {
                border: 1px solid #6d28d9;
                background-color: #09090b;
            }
            QLineEdit#pathBar {
                background-color: #18181b;
                color: #fafafa;
                font-weight: 500;
                border: 1px solid #27272a;
            }
            QLineEdit#searchEdit {
                min-width: 240px;
            }
            QComboBox {
                padding-right: 28px;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 26px;
                background: transparent;
                border: none;
                border-left: 1px solid #27272a;
            }
            QComboBox::down-arrow {
                image: url("__CHEVRON_ICON__");
                width: 10px;
                height: 10px;
                margin-right: 10px;
            }
            QComboBox:hover::down-arrow {
                image: url("__CHEVRON_ICON_HOVER__");
            }
            
            /* Panels */
            QFrame#leftPanel, QFrame#rightPanel, QFrame#explorerDropFrame {
                background: #18181b;
                border: 1px solid #27272a;
                border-radius: 16px;
            }
            QFrame#explorerDropFrame[dropActive="true"] {
                background: #27272a;
                border: 2px dashed #6d28d9;
            }
            
            /* Views */
            QListView#explorerView, QTreeView#folderTree {
                background: transparent;
                border: none;
                outline: none;
                color: #fafafa;
                font-size: 13px;
            }
            QTreeView#folderTree {
                padding-top: 6px;
                show-decoration-selected: 0;
            }
            QTreeView::branch {
                background: transparent;
                border: none;
                image: none;
            }
            QTreeView::branch:selected {
                background: transparent;
            }
            QTreeView::item {
                padding: 6px 10px;
                border-radius: 6px;
                margin: 2px 4px;
            }
            QTreeView::item:hover {
                background: #27272a;
                color: #ffffff;
            }
            QTreeView::item:selected {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 #31135e,
                    stop: 1 #2a247a
                );
                color: #ffffff;
                font-weight: 700;
                border: 1px solid #4c1d95;
            }
            QListView::item {
                border-radius: 10px;
                padding: 8px;
                margin: 4px;
            }
            QListView::item:hover {
                background: #27272a;
            }
            QListView::item:selected {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 #31135e,
                    stop: 1 #2a247a
                );
                border: 1px solid #4c1d95;
            }

            /* Progress & Logs */
            QWidget#progressWidget, QWidget#progressWidgetOverlay {
                background: #18181b;
                border: 1px solid #3f3f46;
                border-radius: 16px;
                padding: 12px;
            }
            
            QPushButton#logToggleButton, QPushButton#processToggleButton {
                background: transparent;
                color: #a1a1aa;
                border: none;
                font-weight: bold;
                font-size: 12px;
                padding: 4px 8px;
            }
            QPushButton#logToggleButton:hover, QPushButton#processToggleButton:hover {
                color: #ffffff;
            }
            QPushButton#logToggleButton:checked, QPushButton#processToggleButton:checked {
                color: #8b5cf6;
            }
            QProgressBar#globalProgressBar {
                background: #09090b;
                border: 1px solid #27272a;
                border-radius: 6px;
                text-align: center;
                color: #fafafa;
                font-weight: 700;
                height: 12px;
                font-size: 11px;
            }
            QProgressBar#globalProgressBar::chunk {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #6d28d9,
                    stop: 1 #4f46e5
                );
                border-radius: 5px;
            }
            QLabel#activitySpinner {
                color: #8b5cf6;
                font-weight: 700;
                font-family: monospace;
            }
            QTextEdit#eventsLog {
                background: #09090b;
                border: 1px solid #27272a;
                border-radius: 10px;
                font-family: 'JetBrains Mono', 'Consolas', 'Monaco', monospace;
                font-size: 12px;
                color: #a1a1aa;
                padding: 10px;
            }

            /* ScrollBars */
            QScrollBar:vertical {
                background: transparent;
                width: 12px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #3f3f46;
                min-height: 40px;
                border-radius: 6px;
                margin: 2px;
            }
            QScrollBar::handle:vertical:hover {
                background: #52525b;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            
            QScrollBar:horizontal {
                background: transparent;
                height: 12px;
                margin: 0;
            }
            QScrollBar::handle:horizontal {
                background: #3f3f46;
                min-width: 40px;
                border-radius: 6px;
                margin: 2px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #52525b;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0px;
            }
            
            /* Menus */
            QMenu {
                background: #18181b;
                color: #e4e4e7;
                border: 1px solid #27272a;
                border-radius: 10px;
                padding: 6px;
            }
            QMenu::item {
                padding: 8px 32px 8px 14px;
                border-radius: 6px;
                font-size: 13px;
            }
            QMenu::item:selected {
                background: #27272a;
                color: #ffffff;
            }
            QMenu::separator {
                height: 1px;
                background: #27272a;
                margin: 4px 8px;
            }

            /* Splitter */
            QSplitter::handle {
                background: transparent;
            }
        """


def main_window_stylesheet() -> str:
    """The main window QSS with asset placeholders resolved to real paths.

    Any code that applies ``_MAIN_WINDOW_STYLESHEET`` directly to a widget
    (rather than through :func:`apply_theme`) must use this instead, or the
    unresolved ``__CHEVRON_ICON__`` placeholder ends up as a literal
    (nonexistent) file name and the combo-box arrow silently fails to load.
    """
    return _MAIN_WINDOW_STYLESHEET.replace(
        "__CHEVRON_ICON__", _CHEVRON_ICON_PATH
    ).replace("__CHEVRON_ICON_HOVER__", _CHEVRON_ICON_HOVER_PATH)


def apply_theme(app: QApplication) -> None:
    app.setStyle(_DelayedTooltipStyle(app.style()))
    app.setStyleSheet(main_window_stylesheet())
