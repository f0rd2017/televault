from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


class _RoutesMixin:
    def _register_route(self, chat_id: str, client, chat, label: str) -> None:
        normalized_chat_id = str(chat_id or "").strip()

        # Fallback: if chat_id is empty, try to get it from the chat object
        if not normalized_chat_id and chat is not None:
            resolved_id = getattr(chat, "id", None)
            if resolved_id is not None:
                normalized_chat_id = str(resolved_id).strip()

        # Fallback: if chat_id is still empty, try to get it from the client
        if not normalized_chat_id and client is not None:
            try:
                # Try to get self from the client (for the main user)
                if hasattr(client, "_self") and client._self is not None:
                    resolved_id = getattr(client._self, "id", None)
                    if resolved_id is not None:
                        normalized_chat_id = str(resolved_id).strip()
            except Exception:
                pass

        # If chat_id is still empty, skip
        if not normalized_chat_id:
            logger.warning(
                "Skipping route registration: cannot determine chat_id for label=%s",
                label,
            )
            return

        # Verify the chat object is valid before registering
        if chat is None:
            logger.warning(
                "Skipping route registration for chat_id=%s: chat object is None",
                normalized_chat_id,
            )
            return

        routes = self._routes_by_chat_id.setdefault(normalized_chat_id, [])
        for existing_client, _, _ in routes:
            if existing_client is client:
                return
        routes.append((client, chat, str(label or "client")))

    async def _resolve_routes(self, chat_id: str) -> list[tuple[object, object, str]]:
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            normalized_chat_id = str(self.chat_id)

        # If chat_id is still empty or "0", try using self.chat_id from the object
        if not normalized_chat_id or normalized_chat_id == "0":
            if self.chat and hasattr(self.chat, "id") and self.chat.id:
                normalized_chat_id = str(self.chat.id)
            elif (
                self.client
                and hasattr(self.client, "_self")
                and self.client._self
                and hasattr(self.client._self, "id")
            ):
                normalized_chat_id = str(self.client._self.id)

        routes = self._routes_by_chat_id.get(normalized_chat_id, [])

        # Try to resolve the chat if no route was found (using the main client)
        if not routes:
            resolved = await self._resolve_chat_for_main_client(normalized_chat_id)
            if resolved is not None:
                self._register_route(
                    normalized_chat_id, self.client, resolved, "main:resolved"
                )
                routes = self._routes_by_chat_id.get(normalized_chat_id, [])

        # Try to resolve the chat through all available clients (if main didn't work)
        if not routes:
            all_clients = [self.client]
            for clients_list in self._routes_by_chat_id.values():
                for c, _, _ in clients_list:
                    if c not in all_clients:
                        all_clients.append(c)

            for ca_client in all_clients[:5]:  # Limit to 5 clients
                try:
                    resolved = await self._resolve_chat_for_client(
                        ca_client, normalized_chat_id
                    )
                    if resolved is not None:
                        self._register_route(
                            normalized_chat_id, ca_client, resolved, "main:resolved-alt"
                        )
                        routes = self._routes_by_chat_id.get(normalized_chat_id, [])
                        break
                except Exception as exc:
                    logger.warning(
                        "Failed to resolve chat_id=%s via alternate client: %s",
                        normalized_chat_id,
                        exc,
                    )

        # Fallback: if a route has chat=None, try to resolve it again
        if routes:
            updated_routes = []
            for route_client, route_chat, route_label in routes:
                if route_chat is None:
                    logger.warning(
                        "Route has chat=None for chat_id=%s, attempting to resolve...",
                        normalized_chat_id,
                    )
                    resolved = await self._resolve_chat_for_main_client(
                        normalized_chat_id
                    )
                    if resolved is not None:
                        updated_routes.append((route_client, resolved, route_label))
                    else:
                        # Skip this route if the chat could not be resolved
                        logger.warning(
                            "Could not resolve chat for route with client for chat_id=%s, skipping route",
                            normalized_chat_id,
                        )
                else:
                    updated_routes.append((route_client, route_chat, route_label))

            # Update routes to keep only valid chats
            self._routes_by_chat_id[normalized_chat_id] = updated_routes
            routes = updated_routes

        if not routes:
            raise RuntimeError(
                f"No delete route available for chat_id={normalized_chat_id}. "
                "Reconnect and verify channel access for configured clients."
            )

        return list(routes)

    async def _pick_route(self, chat_id: str) -> tuple[object, object, str]:
        routes = await self._resolve_routes(chat_id)
        return routes[0]

    def _route_label_for_client(self, tg_client) -> str:
        client_id = id(tg_client)
        for routes in self._routes_by_chat_id.values():
            for route_client, _, route_label in routes:
                if id(route_client) == client_id:
                    return str(route_label)
        return "client"

    async def _resolve_chat_for_client(self, client, chat_id: str):
        """Resolve chat entity using a specific client (not necessarily self.client)."""
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return None
        try:
            if normalized_chat_id.lstrip("-").isdigit():
                resolved_entity = await client.get_entity(int(normalized_chat_id))
            else:
                resolved_entity = await client.get_entity(normalized_chat_id)

            # Verify the entity is valid (has an id)
            if hasattr(resolved_entity, "id") and resolved_entity.id:
                return resolved_entity
            return None
        except Exception as e:
            logger.debug(
                "Failed to resolve chat entity for chat_id=%s: %s",
                normalized_chat_id,
                e,
            )
            return None

    async def _resolve_chat_for_main_client(self, chat_id: str):
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return None
        try:
            if normalized_chat_id.lstrip("-").isdigit():
                resolved_entity = await self.client.get_entity(int(normalized_chat_id))
            else:
                resolved_entity = await self.client.get_entity(normalized_chat_id)

            # Verify the entity is valid (has an id)
            if hasattr(resolved_entity, "id") and resolved_entity.id:
                return resolved_entity
            return None
        except Exception as e:
            logger.debug(
                "Failed to resolve chat entity for chat_id=%s: %s",
                normalized_chat_id,
                e,
            )
            return None
