"""Downloading files from Telegram (TgDownloader).

The class is split into mixins following the same principle as the
MainWindow panels (app/ui/panels). This module is the public re-export.
"""

from __future__ import annotations

from app.tg.adaptive import _AdaptiveDownloadController
from app.tg.download.downloader import TgDownloader

__all__ = ["TgDownloader", "_AdaptiveDownloadController"]
