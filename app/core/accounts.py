"""
AccountManager — управляет несколькими Telegram user аккаунтами для upload.
Каждый аккаунт = отдельная сессия + свой канал = независимый поток ~12 MB/s.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, asdict, field
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import RPCError
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.core.types import AppConfig, TelegramAccount
from app.core.utils import (
    ensure_parent_dir,
    proxy_endpoint,
    proxy_for_set_proxy,
    resolve_working_proxy,
    select_working_proxy_from_chain,
    telethon_client_kwargs,
)
from app.db.repo import DbRepo

logger = logging.getLogger(__name__)

_CONNECT_MAX_ATTEMPTS = 3
_CONNECT_BASE_DELAY = 1.0


def _is_connect_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (OSError, TimeoutError, RPCError))


def _parse_invite_hash(chat_target: str) -> str | None:
    """Извлечь invite-хэш из приглашения в приватный канал.

    Поддерживает ``t.me/+HASH``, ``t.me/joinchat/HASH`` (с любой схемой/доменом)
    и «голый» ``+HASH``. Для публичных ссылок/юзернеймов возвращает None —
    в них вступать через ImportChatInvite нельзя.
    """
    raw = str(chat_target or "").strip()
    if not raw:
        return None
    for marker in ("/joinchat/", "/+"):
        idx = raw.find(marker)
        if idx != -1:
            tail = raw[idx + len(marker) :].strip().strip("/")
            return tail or None
    if raw.startswith("+"):
        return raw[1:].strip() or None
    return None


@dataclass
class ConnectedAccount:
    """Подключённый аккаунт с клиентом."""

    account: TelegramAccount
    client: TelegramClient
    is_authorized: bool = False
    chat_obj: object = None
    chat_id: str = ""
    is_premium: bool = False
    # Proxy fallback chain state: candidates are the non-empty [proxy, proxy_backup];
    # proxy_tier is the index of the currently-used candidate, or len(chain) = direct.
    proxy_chain: list[str] = field(default_factory=list)
    proxy_tier: int = 0
    proxy_label: str = "direct"


class AccountManager:
    """Управление несколькими Telegram аккаунтами."""

    def __init__(self, config: AppConfig, repo: DbRepo) -> None:
        self.config = config
        self.repo = repo
        self._connected: dict[int, ConnectedAccount] = {}
        self._escalate_lock = asyncio.Lock()

    async def load_and_connect_all(self) -> list[ConnectedAccount]:
        """Загрузить все аккаунты из БД и подключить их."""
        accounts = self.repo.list_accounts()
        if not accounts:
            logger.info("No multi-accounts configured, using main session only")
            return []

        connected = []
        for acc in accounts:
            try:
                ca = await self.connect_account(acc)
                if ca:
                    connected.append(ca)
            except Exception:
                logger.exception("Failed to connect account: %s", acc.label)

        logger.info(
            "Multi-accounts ready: %d/%d (primary=%s)",
            len(connected),
            len(accounts),
            next((a.account.label for a in connected if a.account.is_primary), "none"),
        )
        return connected

    async def connect_account(
        self, account: TelegramAccount
    ) -> ConnectedAccount | None:
        """Подключить один аккаунт."""
        session_path = Path(account.session_path)
        ensure_parent_dir(session_path)

        # Primary account connects directly — others use the proxy fallback chain.
        # Chain order: primary proxy → backup proxy → direct. Each candidate is
        # probed (off the event loop); the first reachable one is used, otherwise
        # we connect directly instead of failing.
        proxy_chain = (
            []
            if account.is_primary
            else [
                p for p in (account.proxy, account.proxy_backup) if str(p or "").strip()
            ]
        )
        if proxy_chain:
            proxy, proxy_label, proxy_tier = await asyncio.to_thread(
                select_working_proxy_from_chain, proxy_chain
            )
            if proxy is None:
                logger.warning(
                    "Account '%s': no proxy in chain [%s] reachable — connecting DIRECTLY this session",
                    account.label,
                    ", ".join(proxy_endpoint(p) for p in proxy_chain),
                )
            elif proxy_tier > 0:
                logger.warning(
                    "Account '%s': primary proxy unreachable — using BACKUP proxy %s",
                    account.label,
                    proxy_label,
                )
        else:
            proxy, proxy_label, proxy_tier = None, "direct", 0

        logger.info("Connecting account '%s' via %s", account.label, proxy_label)

        client = TelegramClient(
            str(session_path),
            account.tg_api_id,
            account.tg_api_hash,
            **telethon_client_kwargs(proxy),
        )
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception(_is_connect_retryable),
                wait=wait_exponential(multiplier=_CONNECT_BASE_DELAY, min=1, max=30),
                stop=stop_after_attempt(_CONNECT_MAX_ATTEMPTS),
                reraise=True,
            ):
                with attempt:
                    await client.connect()
        except Exception as e:
            logger.exception(
                "Failed to connect account after %d attempts: %s (%s)",
                _CONNECT_MAX_ATTEMPTS,
                account.label,
                str(e),
            )
            try:
                await client.disconnect()
            except Exception as disconnect_err:
                logger.debug(
                    "Error during disconnect after failed connection: %s",
                    str(disconnect_err),
                )
            return None

        is_authorized = False
        is_premium = False
        chat_obj = None
        chat_id = ""

        try:
            is_authorized = await client.is_user_authorized()
        except Exception as e:
            logger.exception(
                "Authorization check failed for %s (%s)", account.label, str(e)
            )
            try:
                await client.disconnect()
            except Exception as disconnect_err:
                logger.debug(
                    "Error during disconnect after failed authorization: %s",
                    str(disconnect_err),
                )
            return None

        if is_authorized:
            try:
                me = await client.get_me()
                is_premium = bool(getattr(me, "premium", False))
                # Обновить инфу в БД
                self.repo.update_account(
                    account.id,
                    user_id=getattr(me, "id", 0),
                    username=getattr(me, "username", "") or "",
                    is_premium=1 if is_premium else 0,
                )
            except Exception as e:
                logger.exception(
                    "Failed to get account info for %s (%s)", account.label, str(e)
                )

            # Разрешить чат — пробуем несколькими способами с retry
            chat_resolve_attempts = 0
            max_chat_resolve_attempts = 3
            invite_hash = _parse_invite_hash(account.chat_target)
            join_attempted = False
            while chat_resolve_attempts < max_chat_resolve_attempts:
                try:
                    chat_obj = await client.get_entity(account.chat_target)
                    chat_id = str(getattr(chat_obj, "id", ""))
                    logger.debug(
                        "Chat '%s' resolved for '%s': id=%s",
                        account.chat_target,
                        account.label,
                        chat_id,
                    )
                    break
                except Exception as exc:
                    # Авто-join: если это инвайт-ссылка и аккаунт ещё не в канале —
                    # вступаем по хэшу и сразу повторяем резолв (одна попытка).
                    if invite_hash and not join_attempted:
                        join_attempted = True
                        if await self._try_join_via_invite(
                            client, invite_hash, account.label
                        ):
                            continue
                    chat_resolve_attempts += 1
                    if chat_resolve_attempts >= max_chat_resolve_attempts:
                        # Через invite link не получилось — пробуем по chat_id из БД
                        # (если аккаунт раньше уже подключался к этому каналу)
                        logger.warning(
                            "Cannot resolve chat '%s' for '%s' after %d attempts — account stays in pool but may fail delete operations (%s)",
                            account.chat_target,
                            account.label,
                            chat_resolve_attempts,
                            str(exc),
                        )
                        chat_obj = None
                        chat_id = ""
                    else:
                        logger.debug(
                            "Chat resolution attempt %d/%d failed for '%s', retrying... (%s)",
                            chat_resolve_attempts,
                            max_chat_resolve_attempts,
                            account.label,
                            str(exc),
                        )
                        await asyncio.sleep(
                            0.5 * chat_resolve_attempts
                        )  # Exponential backoff

        ca = ConnectedAccount(
            account=account,
            client=client,
            is_authorized=is_authorized,
            chat_obj=chat_obj,
            chat_id=chat_id,
            is_premium=is_premium,
            proxy_chain=proxy_chain,
            proxy_tier=proxy_tier,
            proxy_label=proxy_label,
        )
        self._connected[account.id] = ca

        proxy_label = proxy_endpoint(account.proxy) if account.proxy else "direct"
        logger.info(
            "Account '%s': authorized=%s premium=%s chat=%s proxy=%s",
            account.label,
            is_authorized,
            is_premium,
            chat_id or "unresolved",
            proxy_label,
        )
        return ca

    async def _try_join_via_invite(
        self, client: TelegramClient, invite_hash: str, label: str
    ) -> bool:
        """Вступить в приватный канал по invite-хэшу. True — если после этого
        аккаунт точно участник (вступил сейчас либо уже состоял)."""
        from telethon.errors import UserAlreadyParticipantError
        from telethon.tl.functions.messages import ImportChatInviteRequest

        try:
            await client(ImportChatInviteRequest(invite_hash))
            logger.info("Account '%s': auto-joined channel via invite link", label)
            return True
        except UserAlreadyParticipantError:
            logger.debug("Account '%s': already a participant of the channel", label)
            return True
        except Exception as exc:
            logger.warning(
                "Account '%s': auto-join via invite link failed (%s)", label, str(exc)
            )
            return False

    async def add_account(self, account: TelegramAccount) -> int | None:
        """Добавить новый аккаунт в БД и подключить. Возвращает ID или None."""
        data = asdict(account)
        data["id"] = 0  # AUTOINCREMENT
        new_account = TelegramAccount(**data)
        account_id = self.repo.insert_account(new_account)
        # Перечитываем с правильным ID
        saved_account = self.repo.get_account(account_id)
        if saved_account is None:
            return None
        ca = await self.connect_account(saved_account)
        return account_id if ca else None

    async def remove_account(self, account_id: int) -> None:
        """Удалить аккаунт и отключить."""
        ca = self._connected.pop(account_id, None)
        if ca:
            try:
                await ca.client.disconnect()
            except Exception as e:
                logger.debug(
                    "Error during disconnect when removing account %d: %s",
                    account_id,
                    str(e),
                )
        self.repo.delete_account(account_id)

    async def disconnect_all(self) -> None:
        """Отключить все аккаунты."""
        for ca in list(self._connected.values()):
            try:
                await ca.client.disconnect()
            except Exception as exc:
                logger.debug(
                    "Error during disconnect_all for %s: %s",
                    ca.account.label,
                    str(exc),
                )
        self._connected.clear()

    def get_connected(self) -> list[ConnectedAccount]:
        return list(self._connected.values())

    async def escalate_proxy(self, client) -> str:
        """Переключить клиент на следующий уровень proxy-цепочки на лету.

        Вызывается, когда соединение через текущий прокси устойчиво падает в
        середине сессии: пробуем следующий кандидат в цепочке (резервный прокси),
        затем direct. Применяется через ``client.set_proxy`` + переподключение.
        Возвращает метку нового уровня; на ``direct`` дальше не двигается.
        """
        ca = next((c for c in self._connected.values() if c.client is client), None)
        if ca is None:
            return "direct"
        async with self._escalate_lock:
            chain = ca.proxy_chain
            if ca.proxy_tier >= len(chain):
                return ca.proxy_label  # already at direct, nothing left to try

            next_tier = ca.proxy_tier + 1
            new_proxy: tuple | None = None
            new_label = "direct"
            chosen_tier = len(chain)
            while next_tier < len(chain):
                probed, label = await asyncio.to_thread(
                    resolve_working_proxy, chain[next_tier]
                )
                if probed is not None:
                    new_proxy, new_label, chosen_tier = probed, label, next_tier
                    break
                next_tier += 1

            try:
                client.set_proxy(proxy_for_set_proxy(new_proxy))
                await client.disconnect()
                await client.connect()
            except Exception as exc:
                logger.exception(
                    "Account '%s': proxy escalation reconnect failed: %s",
                    ca.account.label,
                    str(exc),
                )
                return ca.proxy_label

            ca.proxy_tier = chosen_tier
            ca.proxy_label = new_label
            logger.warning(
                "Account '%s': escalated connection to %s after proxy failure",
                ca.account.label,
                new_label,
            )
            return new_label

    def get_active_endpoints(self) -> list[ConnectedAccount]:
        """Вернуть авторизованные аккаунты с разрешённым чатом.
        chat_obj должен быть не None для корректной загрузки."""
        return [
            ca
            for ca in self._connected.values()
            if ca.is_authorized and ca.chat_obj is not None
        ]
