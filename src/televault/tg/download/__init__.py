"""Downloading files from Telegram (TgDownloader).

The class is split into mixins following the same principle as the
MainWindow panels (televault/ui/panels). This module is the public re-export.
"""

from __future__ import annotations

from televault.tg.adaptive import _AdaptiveDownloadController
from televault.tg.download.downloader import TgDownloader

__all__ = ["TgDownloader", "_AdaptiveDownloadController"]
