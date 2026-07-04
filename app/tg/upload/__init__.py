"""Uploading files to Telegram (TgUploader).

The class is split into mixins following the same principle as the
MainWindow panels (app/ui/panels). This module is the public re-export.
"""

from __future__ import annotations

from app.tg.adaptive import _AdaptiveUploadController
from app.tg.upload.uploader import TgUploader

__all__ = ["TgUploader", "_AdaptiveUploadController"]
