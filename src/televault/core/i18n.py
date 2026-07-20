"""Application language registry and runtime switching.

Source strings written in ``self.tr(...)`` calls throughout the UI are in
English — English is the *source* language, so it needs no ``.qm`` file.
Russian and Ukrainian are real translations compiled from
``src/televault/i18n/<code>.ts`` into ``src/televault/i18n/<code>.qm`` (see
``scripts/update_translations.sh`` / ``scripts/compile_translations.sh``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSettings, QTranslator
from PySide6.QtWidgets import QApplication

SETTINGS_ORG = "TeleVault"
SETTINGS_APP = "App"
SETTINGS_KEY = "language"

# The language the literal strings in the source code are written in.
# No .qm file is loaded for it — Qt just shows the source text as-is.
SOURCE_LANGUAGE = "en_US"


@dataclass(frozen=True)
class Language:
    code: str
    native_name: str
    flag: str


LANGUAGES: tuple[Language, ...] = (
    Language("en_US", "English", "🇬🇧"),
    Language("ru_RU", "Русский", "🇷🇺"),
    Language("uk_UA", "Українська", "🇺🇦"),
)

_LANGUAGE_BY_CODE = {lang.code: lang for lang in LANGUAGES}


def available_languages() -> tuple[Language, ...]:
    return LANGUAGES


def language_by_code(code: str) -> Language:
    return _LANGUAGE_BY_CODE.get(code, _LANGUAGE_BY_CODE[SOURCE_LANGUAGE])


def i18n_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "i18n"


def saved_language() -> str:
    """Read the persisted language choice, falling back to the source language."""
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    code = str(settings.value(SETTINGS_KEY, SOURCE_LANGUAGE))
    return code if code in _LANGUAGE_BY_CODE else SOURCE_LANGUAGE


def save_language(code: str) -> None:
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    settings.setValue(SETTINGS_KEY, code)


def install_language(app: QApplication, code: str, translator: QTranslator) -> str:
    """(Re)install the translator for ``code`` on ``app``.

    Removing-then-reinstalling on every switch (instead of only when a
    translator is already active) keeps this idempotent for the initial
    startup call too. Returns the code actually applied (falls back to the
    source language if the requested locale has no compiled .qm yet).
    """
    app.removeTranslator(translator)
    if code == SOURCE_LANGUAGE:
        return SOURCE_LANGUAGE
    if translator.load(f"{code}.qm", str(i18n_dir())):
        app.installTranslator(translator)
        return code
    return SOURCE_LANGUAGE
