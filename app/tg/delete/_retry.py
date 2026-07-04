from __future__ import annotations

import logging

from telethon.errors import (
    FloodWaitError,
    MessageDeleteForbiddenError,
    MessageIdInvalidError,
    MsgIdInvalidError,
)
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.core.jobs import CancelToken, JobCancelledError
from app.tg.delete._helpers import (
    _FLOOD_WAIT_MAX_RETRIES,
    _interruptible_sleep,
    _is_retryable_error,
    _is_route_unusable_error,
)

logger = logging.getLogger(__name__)


class _RetryMixin:
    async def _delete_with_retry(
        self,
        chat_id: str,
        msg_ids: list[int],
        cancel_token: CancelToken | None = None,
    ) -> tuple[list[int], list[int], str, str | None]:
        ids = list(dict.fromkeys(int(msg_id) for msg_id in msg_ids))
        if not ids:
            return [], [], "main", None
        try:
            routes = await self._resolve_routes(chat_id)
        except RuntimeError as e:
            logger.error("Could not pick route for chat_id=%s: %s", chat_id, e)
            return [], ids, "none", str(e)

        unusable_route_errors: list[str] = []
        for route_client, route_chat, route_label in routes:
            if route_chat is None:
                unusable_route_errors.append(f"{route_label}: chat is None")
                continue
            try:
                await self._delete_batch_with_retry(
                    route_client, route_chat, ids, cancel_token=cancel_token
                )
                return ids, [], route_label, None
            except (
                MessageIdInvalidError,
                MsgIdInvalidError,
                MessageDeleteForbiddenError,
            ):
                # A mixed batch can fail due to one bad/forbidden message id.
                # Fall back to single deletes so good ids are still cleaned up.
                pass
            except JobCancelledError:
                raise
            except Exception as exc:
                if _is_route_unusable_error(exc):
                    unusable_route_errors.append(f"{route_label}: {exc}")
                    logger.warning(
                        "Skipping unusable delete route for chat_id=%s label=%s: %s",
                        chat_id,
                        route_label,
                        exc,
                    )
                    continue
                logger.warning(
                    "Batch delete failed for chat_id=%s via %s: %s. Falling back to single deletes.",
                    chat_id,
                    route_label,
                    exc,
                )

            try:
                deleted_ids: list[int] = []
                failed_ids: list[int] = []
                fallback_reason: str | None = None
                for msg_id in ids:
                    if cancel_token is not None:
                        cancel_token.raise_if_cancelled()
                    ok, reason = await self._delete_single_with_retry(
                        route_client,
                        route_chat,
                        msg_id,
                        cancel_token=cancel_token,
                    )
                    if ok:
                        deleted_ids.append(msg_id)
                    else:
                        failed_ids.append(msg_id)
                        if fallback_reason is None and reason:
                            fallback_reason = reason
                return deleted_ids, failed_ids, route_label, fallback_reason
            except JobCancelledError:
                raise
            except Exception as exc:
                if _is_route_unusable_error(exc):
                    unusable_route_errors.append(f"{route_label}: {exc}")
                    logger.warning(
                        "Skipping unusable single-delete route for chat_id=%s label=%s: %s",
                        chat_id,
                        route_label,
                        exc,
                    )
                    continue
                raise

        if unusable_route_errors:
            joined = "; ".join(unusable_route_errors)
            logger.error(
                "All delete routes unusable for chat_id=%s: %s",
                chat_id,
                joined,
            )
            return [], ids, "none", f"all delete routes unusable: {joined}"
        return [], ids, "none", "no usable delete route"

    async def _delete_batch_with_retry(
        self,
        client,
        chat,
        msg_ids: list[int],
        cancel_token: CancelToken | None = None,
    ) -> None:
        # Validate chat object before attempting to delete messages
        if chat is None:
            raise RuntimeError(
                f"Cannot delete messages in chat_id={'unknown'}: "
                "chat object is None. The channel entity was not resolved successfully. "
                "Reconnect accounts and verify channel access."
            )

        # Get the chat ID needed to perform the deletion
        chat_id = getattr(chat, "id", None)
        if chat_id is None:
            # Try to get the ID from chat_id if the chat object doesn't contain one
            chat_id = getattr(self, "chat_id", None)

        if chat_id is None:
            raise RuntimeError(
                "Could not determine chat_id for deletion. The channel entity does not contain an ID."
            )

        flood_wait_retries = 0
        while True:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            try:
                async for attempt in AsyncRetrying(
                    retry=retry_if_exception(_is_retryable_error),
                    wait=wait_exponential(
                        multiplier=self.config.retry.base_delay, min=1, max=60
                    ),
                    stop=stop_after_attempt(self.config.retry.max_attempts),
                    reraise=True,
                ):
                    with attempt:
                        try:
                            # Use the chat object directly for better entity resolution in Telethon
                            await client.delete_messages(chat, msg_ids)
                        except Exception as e:
                            # Fallback for private chats/groups when an "Invalid channel object" error occurs
                            err_str = str(e)
                            if "Invalid channel object" in err_str:
                                logger.debug(
                                    "Invalid channel object in batch delete, falling back to peerless delete for chat_id=%s",
                                    chat_id,
                                )
                                await client.delete_messages(None, msg_ids)
                            else:
                                raise
                        return
            except (TypeError, AttributeError, ValueError) as e:
                # A type or value error that can happen when trying to delete with an invalid chat object
                err_str = str(e)
                if "Invalid channel object" in err_str or "input entity" in err_str:
                    raise RuntimeError(
                        f"Delete route unusable for chat {chat_id}: {err_str}"
                    ) from e

                logger.error(
                    "Invalid channel object for deletion: %s (chat_type=%s)",
                    e,
                    type(chat),
                )
                raise RuntimeError(f"Invalid channel object for deletion: {e}")

            except Exception as e:
                # Any other error, including RPC errors that may indicate an invalid entity type
                if "channel object" in str(
                    e
                ).lower() or "channel entity does not contain an ID" in str(e):
                    logger.error("Invalid channel object for deletion: %s", e)
                    # Instead of raising, log the error and re-raise it further up
                    # so the outer handler can decide on a partial result
                    raise RuntimeError(f"Invalid channel object for deletion: {e}")
                else:
                    # If it's a different error, propagate it further up
                    raise e
            except FloodWaitError as exc:
                flood_wait_retries += 1
                if flood_wait_retries > _FLOOD_WAIT_MAX_RETRIES:
                    logger.error(
                        "Delete batch flood wait exhausted after %d retries, giving up",
                        flood_wait_retries,
                    )
                    raise
                wait_seconds = float(exc.seconds) + 1.0
                logger.warning(
                    "Delete batch flood wait: %.0fs (retry %d/%d)",
                    wait_seconds,
                    flood_wait_retries,
                    _FLOOD_WAIT_MAX_RETRIES,
                )
                await _interruptible_sleep(wait_seconds, cancel_token)

    async def _validate_or_resolve_chat(self, chat, client):
        """
        Validates if chat object is valid for deletion operations, and if not, attempts to resolve it.
        """
        # Check if chat is valid by trying to access its id
        if chat is not None:
            try:
                chat_id = getattr(chat, "id", None)
                if chat_id is not None:
                    return chat
            except Exception:
                pass  # Chat object is invalid, try to resolve it

        # If chat is None or invalid, try to resolve it using the client
        # First, try to get the client's own user entity
        try:
            await client.get_me()
            # Don't use the user entity for deleting messages
            logger.debug("Client's user entity is not suitable for message deletion")
            return None
        except Exception:
            logger.debug("Could not get client's user entity for deletion")

        # If getting the user entity failed, return None
        # This indicates that we cannot perform deletion operations
        return None

    async def _delete_single_with_retry(
        self,
        client,
        chat,
        msg_id: int,
        cancel_token: CancelToken | None = None,
    ) -> tuple[bool, str | None]:
        # Validate chat object before attempting to delete messages
        if chat is None:
            logger.warning("Cannot delete message %s: chat object is None", msg_id)
            return False, "chat object is None"

        # Get the chat ID needed to perform the deletion
        chat_id = getattr(chat, "id", None)
        if chat_id is None:
            # Try to get the ID from chat_id if the chat object doesn't contain one
            chat_id = getattr(self, "chat_id", None)

        if chat_id is None:
            logger.warning(
                "Could not determine chat_id for deletion of msg_id=%s", msg_id
            )
            return False, "could not determine chat_id"

        flood_wait_retries = 0
        while True:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            try:
                async for attempt in AsyncRetrying(
                    retry=retry_if_exception(_is_retryable_error),
                    wait=wait_exponential(
                        multiplier=self.config.retry.base_delay, min=1, max=60
                    ),
                    stop=stop_after_attempt(self.config.retry.max_attempts),
                    reraise=True,
                ):
                    with attempt:
                        try:
                            # Use the chat object directly for better entity resolution in Telethon
                            await client.delete_messages(chat, [int(msg_id)])
                        except Exception as e:
                            # Fallback for private chats/groups when an "Invalid channel object" error occurs
                            err_str = str(e)
                            if "Invalid channel object" in err_str:
                                logger.debug(
                                    "Invalid channel object in single delete, falling back to peerless delete for msg_id=%s",
                                    msg_id,
                                )
                                await client.delete_messages(None, [int(msg_id)])
                            else:
                                raise
                        return True, None
            except (MessageIdInvalidError, MsgIdInvalidError):
                # Already missing remotely, treat as deleted for index cleanup.
                return True, None
            except ValueError as e:
                # If Telethon cannot resolve the entity, we cannot delete it anyway.
                if "Could not find the input entity" in str(e):
                    raise RuntimeError(
                        f"Delete route unusable for msg_id={msg_id}: {e}"
                    ) from e
                raise
            except MessageDeleteForbiddenError:
                return (
                    False,
                    "message delete forbidden (no rights to delete this message)",
                )
            except (TypeError, AttributeError, ValueError) as e:
                # A type or value error that can happen when trying to delete with an invalid chat object
                err_str = str(e)
                if "Invalid channel object" in err_str or "input entity" in err_str:
                    raise RuntimeError(
                        f"Delete route unusable for msg_id={msg_id}: {err_str}"
                    ) from e

                logger.error("Error in single delete for msg_id=%s: %s", msg_id, e)
                return False, err_str or "invalid channel object"

            except Exception as e:
                # Check whether this is an Invalid channel object error
                if "Invalid channel object" in str(
                    e
                ) or "channel entity does not contain an ID" in str(e):
                    raise RuntimeError(
                        f"Delete route unusable for msg_id={msg_id}: {e}"
                    ) from e
                else:
                    # If it's a different error, propagate it further up
                    raise e
            except FloodWaitError as exc:
                flood_wait_retries += 1
                if flood_wait_retries > _FLOOD_WAIT_MAX_RETRIES:
                    logger.error(
                        "Delete single flood wait exhausted after %d retries, msg_id=%s",
                        flood_wait_retries,
                        msg_id,
                    )
                    raise
                wait_seconds = float(exc.seconds) + 1.0
                logger.warning(
                    "Delete single flood wait: %.0fs msg_id=%s (retry %d/%d)",
                    wait_seconds,
                    msg_id,
                    flood_wait_retries,
                    _FLOOD_WAIT_MAX_RETRIES,
                )
                await _interruptible_sleep(wait_seconds, cancel_token)
