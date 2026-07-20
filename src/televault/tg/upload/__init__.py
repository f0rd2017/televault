"""Uploading files to Telegram (TgUploader).

The class is split into mixins following the same principle as the
MainWindow panels (televault/ui/panels). This module is the public re-export.
"""

from __future__ import annotations

from televault.tg.adaptive import _AdaptiveUploadController
from televault.tg.upload.uploader import TgUploader

__all__ = ["TgUploader", "_AdaptiveUploadController"]
