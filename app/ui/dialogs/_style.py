from __future__ import annotations


_DIALOG_STYLESHEET = """
    QDialog {
        background: #09090b;
        color: #e4e4e7;
    }
    QLabel {
        color: #e4e4e7;
    }
    QLineEdit, QSpinBox {
        background: #18181b;
        color: #e4e4e7;
        border: 1px solid #3f3f46;
        border-radius: 6px;
        padding: 5px 9px;
        selection-background-color: #6d28d9;
    }
    QLineEdit:focus, QSpinBox:focus {
        border: 1px solid #7c3aed;
        background: #1f1f23;
    }
    QLineEdit:disabled, QSpinBox:disabled {
        color: #71717a;
        background: #141416;
    }
    QSpinBox::up-button, QSpinBox::down-button {
        background: #27272a;
        border: none;
        border-radius: 4px;
        width: 18px;
    }
    QSpinBox::up-button:hover, QSpinBox::down-button:hover {
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
