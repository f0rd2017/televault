from __future__ import annotations

import logging

from televault.core.types import AppConfig
from televault.db.repo import DbRepo
from televault.tg.delete._ops import _OpsMixin
from televault.tg.delete._retry import _RetryMixin
from televault.tg.delete._routes import _RoutesMixin

logger = logging.getLogger(__name__)


class TgDeleter(_OpsMixin, _RetryMixin, _RoutesMixin):
    def __init__(
        self,
        config: AppConfig,
        repo: DbRepo,
        client,
        chat,
        chat_id: str,
        *,
        chats: list[object] | None = None,
        chat_ids: list[str] | None = None,
        delete_endpoints=None,
    ) -> None:
        self.config = config
        self.repo = repo
        self.client = client
        self.chat = chat
        self.chat_id = chat_id
        self._routes_by_chat_id: dict[str, list[tuple[object, object, str]]] = {}

        # Validate the main chat_obj
        if chat is None:
            logger.warning(
                "TgDeleter initialized with chat=None for chat_id=%s. "
                "Delete operations may fail unless channels are resolved at runtime.",
                chat_id,
            )

        endpoint_chat_ids = {
            str(getattr(endpoint, "chat_id", "") or "").strip()
            for endpoint in list(delete_endpoints or [])
            if str(getattr(endpoint, "chat_id", "") or "").strip()
        }
        self._register_route(str(chat_id), client, chat, "main")

        resolved_chats = list(chats or [])
        resolved_chat_ids = [str(item) for item in (chat_ids or [])]
        if len(resolved_chats) == len(resolved_chat_ids):
            for idx, resolved_chat in enumerate(resolved_chats):
                route_chat_id = resolved_chat_ids[idx]
                if route_chat_id in endpoint_chat_ids:
                    continue
                # Validate additional chats
                if resolved_chat is None:
                    logger.warning(
                        "TgDeleter initialized with chats[%d]=None for chat_id=%s. "
                        "Delete operations may fail unless channel is resolved at runtime.",
                        idx,
                        route_chat_id,
                    )
                self._register_route(
                    route_chat_id,
                    client,
                    resolved_chat,
                    f"main:ch{idx + 1}",
                )

        for endpoint in list(delete_endpoints or []):
            endpoint_chat_id = str(getattr(endpoint, "chat_id", "") or "").strip()
            endpoint_client = getattr(endpoint, "client", None)
            endpoint_chat = getattr(endpoint, "chat", None)
            if not endpoint_chat_id or endpoint_client is None:
                logger.warning(
                    "Skipping invalid delete endpoint: chat_id=%s client=%s",
                    endpoint_chat_id,
                    endpoint_client is not None,
                )
                continue
            # Validate endpoint.chat
            if endpoint_chat is None:
                logger.warning(
                    "Delete endpoint has chat=None for chat_id=%s. "
                    "Delete operations may fail unless channel is resolved at runtime.",
                    endpoint_chat_id,
                )
                continue
            label = str(getattr(endpoint, "label", "client") or "client")
            self._register_route(
                endpoint_chat_id, endpoint_client, endpoint_chat, label
            )
