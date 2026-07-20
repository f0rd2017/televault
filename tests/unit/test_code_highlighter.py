import os
import pytest
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from televault.ui.text_editor._highlighter import CodeHighlighter


@pytest.fixture
def app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def document():
    return QTextDocument()


def test_code_highlighter_init(app, document):
    highlighter = CodeHighlighter(document)
    assert len(highlighter._rules) == 0


def test_code_highlighter_python(app, document):
    highlighter = CodeHighlighter(document, "python")
    assert len(highlighter._rules) > 0

    document.setPlainText("def hello():\n    pass")
    # Force highlight
    highlighter.rehighlight()

    block = document.findBlockByNumber(0)
    formats = block.layout().formats()
    # def and hello should be highlighted
    assert len(formats) > 0


def test_code_highlighter_javascript_multiline(app, document):
    highlighter = CodeHighlighter(document, "javascript")
    document.setPlainText("/* \n comment \n */ \n function x() {}")
    highlighter.rehighlight()

    block1 = document.findBlockByNumber(0)
    assert block1.userState() == 1  # In comment

    block3 = document.findBlockByNumber(2)
    assert block3.userState() in (-1, 0)  # Comment ended


def test_code_highlighter_languages(app, document):
    langs = ["json", "javascript", "html", "css", "sql", "yaml", "markdown", "bash"]
    for lang in langs:
        highlighter = CodeHighlighter(document, lang)
        document.setPlainText("some code")
        highlighter.rehighlight()
        assert highlighter is not None


def test_code_highlighter_rehighlight(app, document):
    highlighter = CodeHighlighter(document, "python")
    document.setPlainText("import os\n# comment")
    highlighter.rehighlight()
    highlighter.set_language("markdown")
    document.setPlainText("# Header\n**bold**")
    highlighter.rehighlight()
    block = document.findBlockByNumber(0)
    formats = block.layout().formats()
    assert len(formats) > 0
