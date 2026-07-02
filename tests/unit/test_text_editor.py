import os
import pytest
from PySide6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app.ui.text_editor as text_editor_module
from app.ui.text_editor import TextEditorWindow, CodeEditor, open_text_editor

@pytest.fixture
def app():
    app = QApplication.instance() or QApplication([])
    yield app

@pytest.fixture
def editor_window(app, monkeypatch):
    monkeypatch.setattr(text_editor_module.QMessageBox, "critical", lambda *a, **k: None)
    monkeypatch.setattr(text_editor_module.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(text_editor_module.QMessageBox, "question", lambda *a, **k: text_editor_module.QMessageBox.StandardButton.Yes)
    # Ensure global window is reset for each test
    text_editor_module._MAIN_EDITOR_WINDOW = None
    window = TextEditorWindow()
    yield window
    window.close()
    text_editor_module._MAIN_EDITOR_WINDOW = None

def test_code_editor_basics(app):
    editor = CodeEditor()
    editor.setPlainText("Hello")
    assert editor.toPlainText() == "Hello"
    editor.duplicate_line()
    # It duplicates current line
    assert editor.toPlainText() == "Hello\nHello"

def test_code_editor_comment(app):
    editor = CodeEditor()
    editor.set_comment_prefix("# ")
    editor.setPlainText("test")
    # Set cursor to start
    cursor = editor.textCursor()
    cursor.setPosition(0)
    editor.setTextCursor(cursor)
    
    editor.toggle_comment()
    assert editor.toPlainText() == "# test"
    
    editor.toggle_comment()
    # The toggle comment logic strips prefix.strip() which leaves the trailing space
    assert editor.toPlainText() == " test"

def test_text_editor_window_open_tab(editor_window):
    saved_data = None
    def on_save(data: bytes):
        nonlocal saved_data
        saved_data = data

    editor_window.open_file_tab("file1.txt", "Test File", on_save)
    assert editor_window._tabs.count() == 1
    tab = editor_window.active_tab()
    assert tab is not None
    
    # Simulate finished load
    tab.loaded = True
    tab.dirty = True
    tab.editor.setPlainText("Sample text")
    tab.editor.document().setModified(True)
    assert tab.editor.document().isModified() is True
    
    # Save active tab
    editor_window._save_active()
    assert saved_data == b"Sample text"
    assert tab.editor.document().isModified() is False

def test_open_text_editor(app):
    text_editor_module._MAIN_EDITOR_WINDOW = None
    
    saved = False
    def on_save(data: bytes):
        nonlocal saved
        saved = True

    # pass kwargs
    open_text_editor(None, url="file.py", title="Test Python", on_save=on_save)
    
    window = text_editor_module._MAIN_EDITOR_WINDOW
    assert window is not None
    assert window._tabs.count() == 1
    
    tab = window.active_tab()
    tab.loaded = True
    tab.dirty = True
    tab.editor.setPlainText("print('hello')")
    window._save_active()
    assert saved is True
    
    window.close()
    text_editor_module._MAIN_EDITOR_WINDOW = None

def test_text_editor_close_tab(editor_window):
    editor_window.open_file_tab("file1.txt", "File 1", lambda d: None)
    editor_window.open_file_tab("file2.txt", "File 2", lambda d: None)
    assert editor_window._tabs.count() == 2
    
    editor_window.close_active_tab()
    assert editor_window._tabs.count() == 1
