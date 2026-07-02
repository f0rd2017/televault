"""Общие хелперы для пакета upload."""

from __future__ import annotations

from telethon.errors import FilePartsInvalidError, FloodWaitError, RPCError


def _is_retryable_error(exc: BaseException) -> bool:
    return isinstance(exc, (OSError, TimeoutError, RPCError)) and not isinstance(
        exc,
        (FloodWaitError, FilePartsInvalidError),
    )
