from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


class _RoutesMixin:
    def _register_route(self, chat_id: str, client, chat, label: str) -> None:
        normalized_chat_id = str(chat_id or "").strip()

        # Fallback: если chat_id пустой, попытаться получить из chat объекта
        if not normalized_chat_id and chat is not None:
            resolved_id = getattr(chat, "id", None)
            if resolved_id is not None:
                normalized_chat_id = str(resolved_id).strip()

        # Fallback: если chat_id всё ещё пустой, попытаться получить из client
        if not normalized_chat_id and client is not None:
            try:
                # Попытка получить self from client (для основного пользователя)
                if hasattr(client, "_self") and client._self is not None:
                    resolved_id = getattr(client._self, "id", None)
                    if resolved_id is not None:
                        normalized_chat_id = str(resolved_id).strip()
            except Exception:
                pass

        # Если chat_id всё ещё пустой — пропускаем
        if not normalized_chat_id:
            logger.warning(
                "Skipping route registration: cannot determine chat_id for label=%s",
                label,
            )
            return

        # Проверяем, что объект чата действителен перед регистрацией
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

        # Если chat_id всё ещё пустой или "0" — пробуем использовать self.chat_id из объекта
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

        # Попытка разрешить чат если роут не найден (используем main client)
        if not routes:
            resolved = await self._resolve_chat_for_main_client(normalized_chat_id)
            if resolved is not None:
                self._register_route(
                    normalized_chat_id, self.client, resolved, "main:resolved"
                )
                routes = self._routes_by_chat_id.get(normalized_chat_id, [])

        # Попытка разрешить чат через все доступные клиенты (если main не сработал)
        if not routes:
            all_clients = [self.client]
            for clients_list in self._routes_by_chat_id.values():
                for c, _, _ in clients_list:
                    if c not in all_clients:
                        all_clients.append(c)

            for ca_client in all_clients[:5]:  # Ограничиваем до 5 клиентов
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

        # Fallback: если в роуте chat=None, попытаться разрешить заново
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
                        # Пропускаем этот маршрут, если не удалось разрешить чат
                        logger.warning(
                            "Could not resolve chat for route with client for chat_id=%s, skipping route",
                            normalized_chat_id,
                        )
                else:
                    updated_routes.append((route_client, route_chat, route_label))

            # Обновляем маршруты только с валидными чатами
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

            # Проверяем, что сущность действительна (имеет id)
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

            # Проверяем, что сущность действительна (имеет id)
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
