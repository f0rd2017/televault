"""Выгрузка файлов из Telegram (TgDownloader).

Класс разбит на mixin'ы по тому же принципу, что и панели MainWindow
(app/ui/panels). Здесь — публичный re-export.
"""

from __future__ import annotations

from app.tg.adaptive import _AdaptiveDownloadController
from app.tg.download.downloader import TgDownloader

__all__ = ["TgDownloader", "_AdaptiveDownloadController"]
