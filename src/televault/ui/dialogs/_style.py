from __future__ import annotations

from pathlib import Path

_ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "assets"
_CHEVRON_ICON_PATH = (_ASSETS_DIR / "chevron_down.png").as_posix()
_CHEVRON_ICON_HOVER_PATH = (_ASSETS_DIR / "chevron_down_hover.png").as_posix()
_CHECKMARK_ICON_PATH = (_ASSETS_DIR / "checkmark.png").as_posix()


_DIALOG_STYLESHEET_TEMPLATE = """
    QDialog {
        background: #09090b;
        color: #e4e4e7;
    }
    QLabel {
        color: #e4e4e7;
    }
    QScrollArea {
        background: transparent;
        border: none;
    }
    QScrollArea > QWidget > QWidget {
        background: transparent;
    }
    QComboBox {
        background: #18181b;
        color: #e4e4e7;
        border: 1px solid #3f3f46;
        border-radius: 6px;
        padding: 5px 28px 5px 9px;
        selection-background-color: #6d28d9;
    }
    QComboBox:hover { border: 1px solid #52525b; }
    QComboBox:focus { border: 1px solid #7c3aed; }
    QComboBox::drop-down {
        subcontrol-origin: padding;
        subcontrol-position: top right;
        width: 24px;
        background: transparent;
        border: none;
        border-left: 1px solid #3f3f46;
    }
    QComboBox::down-arrow {
        image: url("__CHEVRON_ICON__");
        width: 10px;
        height: 10px;
        margin-right: 8px;
    }
    QComboBox:hover::down-arrow {
        image: url("__CHEVRON_ICON_HOVER__");
    }
    QComboBox QAbstractItemView {
        background: #18181b;
        color: #e4e4e7;
        border: 1px solid #3f3f46;
        selection-background-color: #6d28d9;
        selection-color: #ffffff;
        outline: none;
    }
    QCheckBox {
        color: #e4e4e7;
        spacing: 8px;
    }
    QCheckBox::indicator {
        width: 15px;
        height: 15px;
        border: 1px solid #52525b;
        border-radius: 4px;
        background: #18181b;
    }
    QCheckBox::indicator:hover { border: 1px solid #71717a; }
    QCheckBox::indicator:checked {
        background: #6d28d9;
        border: 1px solid #7c3aed;
        image: url("__CHECKMARK_ICON__");
    }
    QSlider::groove:horizontal {
        background: #27272a;
        height: 4px;
        border-radius: 2px;
    }
    QSlider::sub-page:horizontal {
        background: #6d28d9;
        height: 4px;
        border-radius: 2px;
    }
    QSlider::handle:horizontal {
        background: #e4e4e7;
        width: 14px;
        height: 14px;
        margin: -5px 0;
        border-radius: 7px;
    }
    QSlider::handle:horizontal:hover { background: #ffffff; }
    QLineEdit, QSpinBox, QDoubleSpinBox {
        background: #18181b;
        color: #e4e4e7;
        border: 1px solid #3f3f46;
        border-radius: 6px;
        padding: 5px 9px;
        selection-background-color: #6d28d9;
    }
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
        border: 1px solid #7c3aed;
        background: #1f1f23;
    }
    QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
        color: #71717a;
        background: #141416;
    }
    QSpinBox::up-button, QSpinBox::down-button,
    QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
        background: #27272a;
        border: none;
        border-radius: 4px;
        width: 18px;
    }
    QSpinBox::up-button:hover, QSpinBox::down-button:hover,
    QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
        background: #3f3f46;
    }
    QPushButton {
        background: #27272a;
        color: #e4e4e7;
        border: 1px solid #3f3f46;
        border-radius: 6px;
        padding: 6px 14px;
        font-weight: 500;
    }
    QPushButton:hover { background: #3f3f46; color: #ffffff; }
    QPushButton:pressed { background: #52525b; }
    QDialogButtonBox QPushButton { min-width: 80px; }
    QTabWidget::pane {
        background: #09090b;
        border: 1px solid #27272a;
        border-radius: 8px;
        top: -1px;
    }
    QTabBar::tab {
        background: #18181b;
        color: #a1a1aa;
        border: 1px solid #27272a;
        border-bottom: none;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        padding: 6px 16px;
        margin-right: 2px;
    }
    QTabBar::tab:selected {
        background: #27272a;
        color: #ffffff;
    }
    QTabBar::tab:hover:!selected {
        background: #1f1f23;
        color: #e4e4e7;
    }
    QScrollBar:vertical {
        background: #18181b;
        width: 10px;
        margin: 3px 2px 3px 2px;
        border: none;
        border-radius: 5px;
    }
    QScrollBar::handle:vertical {
        background: #52525b;
        min-height: 20px;
        border-radius: 5px;
    }
    QScrollBar::handle:vertical:hover { background: #71717a; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
        background: transparent; border: none; height: 0;
    }
    QToolTip {
        background: #18181b;
        color: #e4e4e7;
        border: 1px solid #3f3f46;
        padding: 4px 7px;
    }
"""

_DIALOG_STYLESHEET = (
    _DIALOG_STYLESHEET_TEMPLATE.replace("__CHEVRON_ICON__", _CHEVRON_ICON_PATH)
    .replace("__CHEVRON_ICON_HOVER__", _CHEVRON_ICON_HOVER_PATH)
    .replace("__CHECKMARK_ICON__", _CHECKMARK_ICON_PATH)
)
