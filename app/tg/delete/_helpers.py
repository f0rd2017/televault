from __future__ import annotations

import asyncio

from telethon.errors import (
    FloodWaitError,
    MessageDeleteForbiddenError,
    MessageIdInvalidError,
    MsgIdInvalidError,
    RPCError,
)

from app.core.jobs import CancelToken

_FLOOD_WAIT_CHECK_INTERVAL = 0.5  # seconds between cancel checks during FloodWait sleep
_FLOOD_WAIT_MAX_RETRIES = 5  # max times we retry after FloodWait before giving up


def _is_retryable_error(exc: BaseException) -> bool:
    return isinstance(exc, (OSError, TimeoutError, RPCError)) and not isinstance(
        exc,
        (
            FloodWaitError,
            MessageDeleteForbiddenError,
            MessageIdInvalidError,
            MsgIdInvalidError,
        ),
    )


def _is_route_unusable_error(exc: BaseException) -> bool:
    text = str(exc or "").lower()
    return (
        "could not find the input entity" in text
        or "input entity" in text
        or "invalid channel object" in text
        or "channel entity does not contain an id" in text
    )


async def _interruptible_sleep(
    seconds: float, cancel_token: CancelToken | None
) -> None:
    """Sleep for `seconds`, checking cancel_token every interval."""
    if cancel_token is None:
        await asyncio.sleep(seconds)
        return
    remaining = float(seconds)
    while remaining > 0:
        cancel_token.raise_if_cancelled()
        chunk = min(remaining, _FLOOD_WAIT_CHECK_INTERVAL)
        await asyncio.sleep(chunk)
        remaining -= chunk
    cancel_token.raise_if_cancelled()
