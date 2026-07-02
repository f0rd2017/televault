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
    monkeypatch.setattr(
        text_editor_module.QMessageBox, "critical", lambda *a, **k: None
    )
    monkeypatch.setattr(
        text_editor_module.QMessageBox, "information", lambda *a, **k: None
    )
    monkeypatch.setattr(
        text_editor_module.QMessageBox,
        "question",
        lambda *a, **k: text_editor_module.QMessageBox.StandardButton.Yes,
    )
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


def _prepared_tab(editor_window, text: str, name: str = "f.txt"):
    editor_window.open_file_tab(name, name, lambda d: None)
    tab = editor_window.active_tab()
    tab.loaded = True
    tab.editor.setReadOnly(False)
    tab.editor.setPlainText(text)
    return tab


def test_replace_all_does_not_loop_when_replacement_contains_needle(editor_window):
    """Регрессия: «a» → «aa» зацикливалось навсегда (поиск wrap'ался на начало
    и находил только что вставленное)."""
    tab = _prepared_tab(editor_window, "a b a")
    editor_window._find_edit.setText("a")
    editor_window._replace_edit.setText("aa")
    editor_window._replace_all()
    assert tab.editor.toPlainText() == "aa b aa"


def test_replace_all_regex_groups(editor_window):
    tab = _prepared_tab(editor_window, "name=1\nsize=2")
    editor_window._regex_chk.setChecked(True)
    editor_window._find_edit.setText(r"(\w+)=(\d+)")
    editor_window._replace_edit.setText(r"$2:$1")
    editor_window._replace_all()
    assert tab.editor.toPlainText() == "1:name\n2:size"
    editor_window._regex_chk.setChecked(False)


def test_replace_all_empty_regex_match_does_not_hang(editor_window):
    tab = _prepared_tab(editor_window, "abc")
    editor_window._regex_chk.setChecked(True)
    editor_window._find_edit.setText("x*")  # может совпадать с пустотой
    editor_window._replace_edit.setText("y")
    editor_window._replace_all()  # главное — завершилась, не зависла
    editor_window._regex_chk.setChecked(False)
    assert "abc" in tab.editor.toPlainText().replace("y", "")


def test_move_last_line_up_keeps_line_count(app):
    """Регрессия: перемещение последней строки вверх добавляло пустую строку."""
    editor = CodeEditor()
    editor.setPlainText("a\nb")
    cursor = editor.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    editor.setTextCursor(cursor)
    editor.move_line(up=True)
    assert editor.toPlainText() == "b\na"
    editor.move_line(up=False)  # обратно вниз
    assert editor.toPlainText() == "a\nb"


def test_indent_and_dedent_selection(app):
    editor = CodeEditor()
    editor.setPlainText("one\ntwo")
    cursor = editor.textCursor()
    cursor.select(cursor.SelectionType.Document)
    editor.setTextCursor(cursor)
    editor.indent_selection()
    assert editor.toPlainText() == "    one\n    two"
    cursor = editor.textCursor()
    cursor.select(cursor.SelectionType.Document)
    editor.setTextCursor(cursor)
    editor.dedent_selection()
    assert editor.toPlainText() == "one\ntwo"


def test_cycle_tab_wraps(editor_window):
    editor_window.open_file_tab("f1", "F1", lambda d: None)
    editor_window.open_file_tab("f2", "F2", lambda d: None)
    editor_window.open_file_tab("f3", "F3", lambda d: None)
    editor_window._tabs.setCurrentIndex(2)
    editor_window._cycle_tab(1)
    assert editor_window._tabs.currentIndex() == 0
    editor_window._cycle_tab(-1)
    assert editor_window._tabs.currentIndex() == 2


def test_expand_regex_groups_helper():
    from PySide6.QtCore import QRegularExpression

    from app.ui.text_editor import _expand_regex_groups

    match = QRegularExpression(r"(\d+)-(\d+)").match("10-20")
    assert _expand_regex_groups(r"$2..$1", match) == "20..10"
    assert _expand_regex_groups(r"\1+\2", match) == "10+20"
    assert _expand_regex_groups("$0", match) == "10-20"
    assert _expand_regex_groups("$9 нет", match) == "$9 нет"  # несуществующая группа
