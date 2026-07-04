from __future__ import annotations

import re

from PySide6.QtGui import (
    QColor,
    QFont,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextDocument,
)


class CodeHighlighter(QSyntaxHighlighter):
    """Universal syntax highlighter for popular programming languages."""

    def __init__(self, document: QTextDocument, lang: str = "text") -> None:
        super().__init__(document)
        self._rules: list[tuple[re.Pattern, QTextCharFormat, int]] = []
        self._multi_line_comment: (
            tuple[re.Pattern, re.Pattern, QTextCharFormat] | None
        ) = None
        self.set_language(lang)

    def set_language(self, lang: str) -> None:
        self._rules.clear()
        self._multi_line_comment = None
        lang = lang.lower()

        fmt_kw = self._fmt("#569cd6", bold=True)
        fmt_type = self._fmt("#4ec9b0")
        fmt_fn = self._fmt("#dcdcaa")
        fmt_str = self._fmt("#ce9178")
        fmt_num = self._fmt("#b5cea8")
        fmt_comment = self._fmt("#6a9955", italic=True)
        fmt_attr = self._fmt("#9cdcfe")
        fmt_tag = self._fmt("#569cd6")

        if lang in ("python", "py"):
            keywords = [
                "and",
                "as",
                "assert",
                "async",
                "await",
                "break",
                "class",
                "continue",
                "def",
                "del",
                "elif",
                "else",
                "except",
                "finally",
                "for",
                "from",
                "global",
                "if",
                "import",
                "in",
                "is",
                "lambda",
                "nonlocal",
                "not",
                "or",
                "pass",
                "raise",
                "return",
                "try",
                "while",
                "with",
                "yield",
                "True",
                "False",
                "None",
            ]
            self._add_keywords(keywords, fmt_kw)
            self._add_rule(r"\bself\b", self._fmt("#569cd6", italic=True))
            self._add_rule(r"@[a-zA-Z0-9_]+", fmt_fn)
            self._add_rule(r"\bdef\s+([a-zA-Z_][a-zA-Z0-9_]*)", fmt_fn, group=1)
            self._add_rule(r"\bclass\s+([a-zA-Z_][a-zA-Z0-9_]*)", fmt_type, group=1)
            self._add_rule(r"#.*$", fmt_comment)
            self._add_rule(r'"[^"\\]*(\\.[^"\\]*)*"', fmt_str)
            self._add_rule(r"'[^'\\]*(\\.[^'\\]*)*'", fmt_str)
            self._add_rule(r"\b\d+(\.\d+)?\b", fmt_num)

        elif lang in ("json",):
            self._add_rule(r'"([^"\\]*(\\.[^"\\]*)*)"\s*:', fmt_attr, group=1)
            self._add_rule(r':\s*"([^"\\]*(\\.[^"\\]*)*)"', fmt_str, group=1)
            self._add_rule(r"\b(true|false|null)\b", fmt_kw)
            self._add_rule(r"\b-?\d+(\.\d+)?([eE][+-]?\d+)?\b", fmt_num)

        elif lang in ("javascript", "typescript", "js", "ts", "jsx", "tsx"):
            keywords = [
                "abstract",
                "any",
                "as",
                "async",
                "await",
                "boolean",
                "break",
                "case",
                "catch",
                "class",
                "const",
                "continue",
                "debugger",
                "default",
                "delete",
                "do",
                "else",
                "enum",
                "export",
                "extends",
                "false",
                "finally",
                "for",
                "from",
                "function",
                "if",
                "implements",
                "import",
                "in",
                "instanceof",
                "interface",
                "let",
                "new",
                "null",
                "number",
                "of",
                "package",
                "private",
                "protected",
                "public",
                "return",
                "static",
                "string",
                "super",
                "switch",
                "this",
                "throw",
                "true",
                "try",
                "typeof",
                "undefined",
                "var",
                "void",
                "while",
                "with",
                "yield",
            ]
            self._add_keywords(keywords, fmt_kw)
            self._add_rule(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", fmt_fn, group=1)
            self._add_rule(r"//.*$", fmt_comment)
            self._add_rule(r'"[^"\\]*(\\.[^"\\]*)*"', fmt_str)
            self._add_rule(r"'[^'\\]*(\\.[^'\\]*)*'", fmt_str)
            self._add_rule(r"`[^`\\]*(\\.[^`\\]*)*`", fmt_str)
            self._add_rule(r"\b\d+(\.\d+)?\b", fmt_num)
            self._multi_line_comment = (
                re.compile(r"/\*"),
                re.compile(r"\*/"),
                fmt_comment,
            )

        elif lang in ("html", "xml", "svg"):
            self._add_rule(r"</?[a-zA-Z0-9_-]+", fmt_tag)
            self._add_rule(r"[a-zA-Z0-9_-]+=", fmt_attr)
            self._add_rule(r'"[^"]*"', fmt_str)
            self._add_rule(r"'[^']*'", fmt_str)
            self._add_rule(r"<!--.*-->", fmt_comment)

        elif lang in ("css", "scss", "less"):
            self._add_rule(r"/[*].*?[*]/", fmt_comment)
            self._add_rule(r"//.*$", fmt_comment)
            self._add_rule(r"[.#][a-zA-Z0-9_-]+", fmt_fn)
            self._add_rule(r"[a-zA-Z_-]+\s*:", fmt_attr)
            self._add_rule(r"#[a-fA-F0-9]{3,8}\b", fmt_num)
            self._add_rule(r"\b\d+(px|em|rem|%|vh|vw|pt|s|ms)?\b", fmt_num)

        elif lang in ("sql",):
            keywords = [
                "SELECT",
                "FROM",
                "WHERE",
                "INSERT",
                "INTO",
                "UPDATE",
                "DELETE",
                "CREATE",
                "TABLE",
                "DROP",
                "ALTER",
                "JOIN",
                "LEFT",
                "RIGHT",
                "INNER",
                "OUTER",
                "ON",
                "GROUP",
                "BY",
                "ORDER",
                "HAVING",
                "LIMIT",
                "OFFSET",
                "AND",
                "OR",
                "NOT",
                "NULL",
                "AS",
                "SET",
                "VALUES",
                "INTEGER",
                "TEXT",
                "REAL",
                "BLOB",
                "PRIMARY",
                "KEY",
                "FOREIGN",
                "REFERENCES",
                "UNION",
            ]
            self._add_keywords(keywords, fmt_kw, case_insensitive=True)
            self._add_rule(r"--.*$", fmt_comment)
            self._add_rule(r"'[^'\\]*(\\.[^'\\]*)*'", fmt_str)
            self._add_rule(r"\b\d+\b", fmt_num)

        elif lang in ("yaml", "yml", "toml", "ini", "env"):
            self._add_rule(r"#.*$", fmt_comment)
            self._add_rule(r"^\s*([a-zA-Z0-9_.-]+)\s*:", fmt_attr, group=1)
            self._add_rule(r"^\s*([a-zA-Z0-9_.-]+)\s*=", fmt_attr, group=1)
            self._add_rule(r'"[^"\\]*(\\.[^"\\]*)*"', fmt_str)
            self._add_rule(r"'[^'\\]*(\\.[^'\\]*)*'", fmt_str)
            self._add_rule(r"\b(true|false|yes|no|on|off)\b", fmt_kw)
            self._add_rule(r"\b\d+(\.\d+)?\b", fmt_num)

        elif lang in ("markdown", "md"):
            self._add_rule(r"^#+.*$", self._fmt("#569cd6", bold=True))
            self._add_rule(r"\*\*.*?\*\*", self._fmt("#d4d4d4", bold=True))
            self._add_rule(r"\*.*?\*", self._fmt("#d4d4d4", italic=True))
            self._add_rule(r"`.*?`", fmt_str)
            self._add_rule(r"\[.*?\]\(.*?\)", fmt_attr)

        elif lang in ("shell", "bash", "sh", "zsh", "ps1"):
            keywords = [
                "if",
                "then",
                "else",
                "fi",
                "for",
                "in",
                "do",
                "done",
                "while",
                "case",
                "esac",
                "function",
                "return",
                "exit",
                "echo",
                "local",
                "export",
            ]
            self._add_keywords(keywords, fmt_kw)
            self._add_rule(r"#.*$", fmt_comment)
            self._add_rule(r"\$([a-zA-Z0-9_]+|\{[^}]+\})", fmt_attr)
            self._add_rule(r'"[^"\\]*(\\.[^"\\]*)*"', fmt_str)
            self._add_rule(r"'[^'\\]*(\\.[^'\\]*)*'", fmt_str)

        self.rehighlight()

    def _fmt(
        self, color: str, bold: bool = False, italic: bool = False
    ) -> QTextCharFormat:
        f = QTextCharFormat()
        f.setForeground(QColor(color))
        if bold:
            f.setFontWeight(QFont.Weight.Bold)
        if italic:
            f.setFontItalic(True)
        return f

    def _add_rule(self, pattern: str, fmt: QTextCharFormat, group: int = 0) -> None:
        self._rules.append((re.compile(pattern, re.MULTILINE), fmt, group))

    def _add_keywords(
        self, keywords: list[str], fmt: QTextCharFormat, case_insensitive: bool = False
    ) -> None:
        flags = re.IGNORECASE if case_insensitive else 0
        pattern = r"\b(" + "|".join(re.escape(k) for k in keywords) + r")\b"
        self._rules.append((re.compile(pattern, flags), fmt, 0))

    def highlightBlock(self, text: str) -> None:
        for pattern, fmt, group in self._rules:
            for match in pattern.finditer(text):
                start = match.start(group)
                if start < 0:
                    continue
                length = match.end(group) - start
                self.setFormat(start, length, fmt)

        if self._multi_line_comment:
            start_pat, end_pat, fmt = self._multi_line_comment
            self.setCurrentBlockState(0)
            start_idx = 0
            if self.previousBlockState() != 1:
                start_match = start_pat.search(text)
                start_idx = start_match.start() if start_match else -1

            while start_idx >= 0:
                end_match = end_pat.search(text, start_idx)
                if not end_match:
                    self.setCurrentBlockState(1)
                    comment_len = len(text) - start_idx
                    self.setFormat(start_idx, comment_len, fmt)
                    break
                end_idx = end_match.end()
                comment_len = end_idx - start_idx
                self.setFormat(start_idx, comment_len, fmt)
                next_match = start_pat.search(text, end_idx)
                start_idx = next_match.start() if next_match else -1
