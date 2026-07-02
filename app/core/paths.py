"""Базовая директория приложения — единая точка отсчёта для config.json, .env
и var/ (данные, кэш, логи).

НЕ cwd: собранное PyInstaller-приложение запускают двойным кликом из
произвольной рабочей директории (а на Windows cwd может быть вообще
недоступен на запись). Portable-стиль: всё лежит рядом с исполняемым файлом;
при запуске из исходников — в корне проекта, как раньше.
"""

from __future__ import annotations

from pathlib import Path
import sys


def app_base_dir() -> Path:
    """Папка приложения: рядом с exe (PyInstaller, ``sys.frozen``) или корень
    проекта (родитель пакета ``app``) при запуске из исходников."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resolve_app_path(value: str | Path) -> Path:
    """Абсолютный путь: относительные значения конфига (``./var/cache``)
    отсчитываются от :func:`app_base_dir`, а не от cwd."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return app_base_dir() / path
