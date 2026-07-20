"""The app's base directory — the single reference point for config.json, .env
and var/ (data, cache, logs).

NOT cwd: a bundled PyInstaller app is launched by double-clicking from an
arbitrary working directory (and on Windows cwd may not even be writable).
Portable style: everything lives next to the executable; when running from
source, it's the project root, as before.
"""

from __future__ import annotations

from pathlib import Path
import sys


def app_base_dir() -> Path:
    """The app's folder: next to the exe (PyInstaller, ``sys.frozen``), or the
    repo root (the parent of ``src/``) when running from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # This file lives at src/televault/core/paths.py — parents[3] is the repo root.
    return Path(__file__).resolve().parents[3]


def resolve_app_path(value: str | Path) -> Path:
    """Return an absolute path: relative config values (``./var/cache``) are
    resolved against :func:`app_base_dir`, not against cwd."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return app_base_dir() / path
