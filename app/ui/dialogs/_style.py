from __future__ import annotations


_DIALOG_STYLESHEET = """
    QDialog {
        background: #0e1118;
        color: #f3f1ff;
    }
    QLabel {
        color: #d8d0f5;
    }
    QLineEdit, QSpinBox {
        background: #0f1420;
        color: #f4f2ff;
        border: 1px solid #2f3850;
        border-radius: 8px;
        padding: 5px 9px;
        selection-background-color: #6f4ec0;
    }
    QLineEdit:focus, QSpinBox:focus {
        border: 1px solid #8b67dd;
        background: #121a2a;
    }
    QSpinBox::up-button, QSpinBox::down-button {
        background: #1a2235;
        border: none;
        border-radius: 4px;
        width: 18px;
    }
    QSpinBox::up-button:hover, QSpinBox::down-button:hover {
        background: #253348;
    }
    QPushButton {
        background: #1c2638;
        color: #d8d0f5;
        border: none;
        border-radius: 8px;
        padding: 6px 14px;
        font-weight: 600;
    }
    QPushButton:hover { background: #253348; }
    QPushButton:pressed { background: #2e3e5a; }
    QDialogButtonBox QPushButton { min-width: 80px; }
    QScrollBar:vertical {
        background: #12192a;
        width: 10px;
        margin: 3px 2px 3px 2px;
        border: none;
        border-radius: 5px;
    }
    QScrollBar::handle:vertical {
        background: #4f5f80;
        min-height: 20px;
        border-radius: 5px;
    }
    QScrollBar::handle:vertical:hover { background: #6577a3; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
        background: transparent; border: none; height: 0;
    }
    QToolTip {
        background: #1f2639;
        color: #f2ebff;
        border: 1px solid #546287;
        padding: 4px 7px;
    }
"""
