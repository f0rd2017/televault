"""Загрузка файлов в Telegram (TgUploader).

Класс разбит на mixin'ы по тому же принципу, что и панели MainWindow
(app/ui/panels). Здесь — публичный re-export.
"""

from __future__ import annotations

from app.tg.adaptive import _AdaptiveUploadController
from app.tg.upload.uploader import TgUploader

__all__ = ["TgUploader", "_AdaptiveUploadController"]
