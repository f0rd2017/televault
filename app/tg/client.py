from __future__ import annotations

import asyncio
from dataclasses import dataclass
from getpass import getpass
import logging
import os
from pathlib import Path

from telethon import TelegramClient
from telethon import functions
from telethon.errors import ChannelPrivateError, SessionPasswordNeededError

from app.core.types import AppConfig, TgTransferLimits
from app.core.utils import (
    build_telethon_proxy,
    ensure_parent_dir,
    proxy_endpoint,
    telethon_client_kwargs,
)

logger = logging.getLogger(__name__)


_TG_REQUEST_SIZE = 524288
_TG_DEFAULT_MAX_FILEPARTS = 4000
_TG_PREMIUM_MAX_FILEPARTS = 8000


@dataclass(slots=True)
class TgClientEndpoint:
    client: TelegramClient
    chat: object
    chat_id: str
    channel_index: int
    role: str
    label: str


@dataclass(slots=True)
class TgSession:
    client: TelegramClient
    chat: object
    chat_id: str
    resolved_chats_by_index: list[object]
    chat_ids_by_index: list[str]
    upload_endpoints: list[TgClientEndpoint]
    download_endpoints: dict[str, list[TgClientEndpoint]]
    transfer_limits: TgTransferLimits


class TgClientManager:
    def __init__(self, config: AppConfig, skip_bots: bool = True) -> None:
        _ = skip_bots
        self.config = config
        self._client: TelegramClient | None = None

    async def start(self, account_targets: list[str] | None = None) -> TgSession:
        """Start a Telegram session.

        Args:
            account_targets: list of chat_target strings from DB accounts.
                             If None, an empty list is used (main session only).
        """
        if account_targets is None:
            account_targets = []
        session_path = Path(self.config.tg_session_path)
        ensure_parent_dir(session_path)
        main_proxy = build_telethon_proxy(self.config.tg_proxy)
        if main_proxy is not None:
            logger.info(
                "Main Telegram session proxy enabled: %s",
                proxy_endpoint(self.config.tg_proxy),
            )

        client = TelegramClient(
            str(session_path),
            self.config.tg_api_id,
            self.config.tg_api_hash,
            **telethon_client_kwargs(main_proxy),
        )
        await client.connect()

        if not await client.is_user_authorized():
            await _authorize_with_env(client)
            if not await client.is_user_authorized():
                raise RuntimeError(
                    "Telegram session is not authorized. "
                    "Launch with terminal and complete login flow."
                )

        is_premium = False
        try:
            me = await client.get_me()
            is_premium = bool(getattr(me, "premium", False))
        except Exception:  # noqa: BLE001
            logger.exception(
                "Unable to detect Telegram account tier, falling back to default limits"
            )

        transfer_limits = await _fetch_transfer_limits(client, is_premium)
        try:
            resolved_chats_by_index, chat_ids_by_index = await self._resolve_main_chats(
                client, account_targets
            )
        except Exception as exc:  # noqa: BLE001
            await client.disconnect()
            self._client = None
            failed_target = account_targets[0] if account_targets else "<no channels>"
            raise RuntimeError(_humanize_entity_error(failed_target, exc)) from exc

        main_channel_index = int(self.config.main_channel_index)
        chat = resolved_chats_by_index[main_channel_index]
        chat_id = chat_ids_by_index[main_channel_index]

        self._client = client

        # Check access to channels
        try:
            channel_checks = await check_channels_access(
                client=client,
                chats=resolved_chats_by_index,
                chat_ids=chat_ids_by_index,
                targets=list(account_targets),
            )
        except Exception:
            logger.exception("Failed to check channels access")
            channel_checks = []

        # Log the access report
        try:
            log_access_report(me, is_premium, channel_checks)
        except Exception:
            logger.exception("Failed to log access report")

        main_upload_endpoint = TgClientEndpoint(
            client=client,
            chat=chat,
            chat_id=chat_id,
            channel_index=main_channel_index,
            role="main",
            label=f"main:ch{main_channel_index + 1}",
        )
        upload_endpoints: list[TgClientEndpoint] = [main_upload_endpoint]
        download_endpoints: dict[str, list[TgClientEndpoint]] = {}
        for idx, resolved_chat in enumerate(resolved_chats_by_index):
            endpoint = TgClientEndpoint(
                client=client,
                chat=resolved_chat,
                chat_id=chat_ids_by_index[idx],
                channel_index=idx,
                role="main",
                label=f"main:ch{idx + 1}",
            )
            download_endpoints.setdefault(endpoint.chat_id, []).append(endpoint)

        logger.info(
            (
                "TG channel profile: channels=%d upload_endpoints=%d download_channel_refs=%d "
                "sharding=%s main_channel=%d"
            ),
            len(resolved_chats_by_index),
            len(upload_endpoints),
            len(download_endpoints),
            self.config.channel_sharding_mode,
            main_channel_index,
        )
        for idx, chat_id_item in enumerate(chat_ids_by_index):
            target_label = (
                account_targets[idx] if idx < len(account_targets) else "<unknown>"
            )
            logger.info(
                "TG channel #%d chat_id=%s target=%s",
                idx + 1,
                chat_id_item,
                target_label,
            )
        for endpoint in upload_endpoints:
            logger.info(
                "TG upload endpoint: role=%s label=%s channel=%d chat_id=%s",
                endpoint.role,
                endpoint.label,
                endpoint.channel_index + 1,
                endpoint.chat_id,
            )

        return TgSession(
            client=client,
            chat=chat,
            chat_id=chat_id,
            resolved_chats_by_index=resolved_chats_by_index,
            chat_ids_by_index=chat_ids_by_index,
            upload_endpoints=upload_endpoints,
            download_endpoints=download_endpoints,
            transfer_limits=transfer_limits,
        )

    async def _resolve_main_chats(
        self, client: TelegramClient, account_targets: list[str]
    ) -> tuple[list[object], list[str]]:
        """Resolve chat_target strings from DB accounts into Telegram entity objects."""
        resolved_chats: list[object] = []
        chat_ids: list[str] = []
        for raw_target in account_targets:
            target = str(raw_target or "").strip()
            try:
                resolved = await client.get_entity(target)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(_humanize_entity_error(target, exc)) from exc
            resolved_chats.append(resolved)
            chat_ids.append(str(getattr(resolved, "id", target)))
        return resolved_chats, chat_ids

    async def stop(self) -> None:
        async def _disconnect_client(tg_client: TelegramClient) -> None:
            try:
                await tg_client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(tg_client.disconnected, timeout=5.0)
            except Exception:  # noqa: BLE001
                pass

        if self._client is not None:
            await _disconnect_client(self._client)
            self._client = None


async def ensure_session_authorized(
    config: AppConfig, interactive: bool = False
) -> None:
    session_path = Path(config.tg_session_path)
    ensure_parent_dir(session_path)
    main_proxy = build_telethon_proxy(config.tg_proxy)

    client = TelegramClient(
        str(session_path),
        config.tg_api_id,
        config.tg_api_hash,
        **telethon_client_kwargs(main_proxy),
    )
    await client.connect()
    try:
        if await client.is_user_authorized():
            return

        if await _authorize_with_env(client):
            return

        if not interactive:
            raise RuntimeError(
                "Telegram session is not authorized. "
                "Set TG_PHONE and TG_LOGIN_CODE, or run interactive login once."
            )

        phone = input("Enter phone in international format (e.g. +123456789): ").strip()
        if not phone:
            raise RuntimeError("Phone is required for Telegram login.")

        try:
            await client.send_code_request(phone)
            code = input("Enter Telegram login code: ").strip()
            if not code:
                raise RuntimeError("Telegram login code is required.")

            try:
                await client.sign_in(phone=phone, code=code)
            except SessionPasswordNeededError:
                password = os.getenv("TG_LOGIN_PASSWORD", "").strip() or getpass(
                    "Enter Telegram 2FA password: "
                )
                if not password:
                    raise RuntimeError("2FA password is required.")
                await client.sign_in(password=password)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(_humanize_auth_error(exc)) from exc

        if not await client.is_user_authorized():
            raise RuntimeError("Telegram authorization failed.")
    finally:
        await client.disconnect()


async def _authorize_with_env(client: TelegramClient) -> bool:
    phone = os.getenv("TG_PHONE", "").strip()
    code = os.getenv("TG_LOGIN_CODE", "").strip()
    password = os.getenv("TG_LOGIN_PASSWORD", "").strip()

    if not phone or not code:
        return False

    try:
        await client.send_code_request(phone)
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            if not password:
                raise RuntimeError(
                    "Two-step verification is enabled. Set TG_LOGIN_PASSWORD in .env."
                ) from None
            await client.sign_in(password=password)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(_humanize_auth_error(exc)) from exc
    return await client.is_user_authorized()


def _build_transfer_limits(
    config_obj: object | None, is_premium: bool
) -> TgTransferLimits:
    def _safe_int(value: object, fallback: int) -> int:
        try:
            casted = int(value)
        except (TypeError, ValueError):
            return fallback
        return casted if casted > 0 else fallback

    default_parts = _safe_int(
        getattr(config_obj, "upload_max_fileparts_default", _TG_DEFAULT_MAX_FILEPARTS),
        _TG_DEFAULT_MAX_FILEPARTS,
    )
    premium_parts = _safe_int(
        getattr(config_obj, "upload_max_fileparts_premium", _TG_PREMIUM_MAX_FILEPARTS),
        max(default_parts, _TG_PREMIUM_MAX_FILEPARTS),
    )
    max_fileparts = premium_parts if is_premium else default_parts
    max_file_size_bytes = max_fileparts * _TG_REQUEST_SIZE
    return TgTransferLimits(
        is_premium=bool(is_premium),
        request_size_bytes=_TG_REQUEST_SIZE,
        max_fileparts=max_fileparts,
        max_file_size_bytes=max_file_size_bytes,
    )


async def _fetch_transfer_limits(
    client: TelegramClient, is_premium: bool
) -> TgTransferLimits:
    try:
        cfg = await client(functions.help.GetConfigRequest())
        limits = _build_transfer_limits(cfg, is_premium=is_premium)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Unable to fetch Telegram config, using fallback transfer limits"
        )
        limits = _build_transfer_limits(None, is_premium=is_premium)

    logger.info(
        "Telegram limits: premium=%s max_fileparts=%d max_file_size=%d bytes request_size=%d bytes",
        limits.is_premium,
        limits.max_fileparts,
        limits.max_file_size_bytes,
        limits.request_size_bytes,
    )
    return limits


def _humanize_auth_error(exc: Exception) -> str:
    name = exc.__class__.__name__
    base = str(exc).strip()

    mapping = {
        "ApiIdInvalidError": "Неверный TG_API_ID или TG_API_HASH.",
        "PhoneNumberInvalidError": "Неверный формат номера телефона.",
        "PhoneNumberBannedError": "Этот номер заблокирован в Telegram.",
        "PhoneNumberFloodError": "Слишком много попыток входа. Подожди и попробуй позже.",
        "PhoneCodeInvalidError": "Неверный код подтверждения.",
        "PhoneCodeExpiredError": "Код подтверждения истёк. Запроси новый код.",
        "PasswordHashInvalidError": "Неверный пароль 2FA.",
        "FloodWaitError": "Telegram временно ограничил запросы (FloodWait). Подожди и повтори.",
    }
    message = mapping.get(name)
    if message:
        return f"{message} [{name}]"
    if base:
        return f"Ошибка авторизации Telegram: {base} [{name}]"
    return f"Ошибка авторизации Telegram [{name}]"


def _humanize_entity_error(chat_target: str, exc: Exception) -> str:
    name = exc.__class__.__name__
    base = str(exc).strip()

    mapping = {
        "ChannelPrivateError": (
            "Доступ к чату/каналу закрыт. "
            "Убедись, что аккаунт добавлен в этот private чат/канал и имеет доступ."
        ),
        "UsernameNotOccupiedError": (
            "Указанный @username не существует или уже не занят."
        ),
        "InviteHashExpiredError": (
            "Ссылка-приглашение истекла. Используй новую invite ссылку."
        ),
        "InviteHashInvalidError": (
            "Неверная ссылка-приглашение. Проверь chat_target в аккаунте (БД)."
        ),
        "ValueError": (
            "Не удалось распознать chat_target. "
            "Используй @username, numeric id вида -100..., или ссылку t.me/..."
        ),
    }
    mapped = mapping.get(name)
    if mapped:
        return f"{mapped} [{name}]"

    if "Cannot find any entity corresponding to" in base:
        return (
            "Не удалось найти чат/канал. "
            f"Текущее значение chat_target: {chat_target!r}. "
            "Укажи корректный @username, numeric id вида -100..., или ссылку t.me/..."
        )
    if base:
        return f"Ошибка выбора чата/канала: {base} [{name}]"
    return f"Ошибка выбора чата/канала [{name}]"


@dataclass(slots=True)
class ChannelAccessCheck:
    channel_index: int
    chat_id: str
    target: str
    accessible: bool
    title: str
    members_count: int
    is_group: bool
    is_channel: bool
    user_permissions: str  # "admin", "member", "restricted", "no_access"
    error: str | None = None


async def check_channels_access(
    client: TelegramClient,
    chats: list[object],
    chat_ids: list[str],
    targets: list[str],
) -> list[ChannelAccessCheck]:
    """Check access to all channels and return detailed information."""
    results: list[ChannelAccessCheck] = []
    for idx, (chat_obj, chat_id, target) in enumerate(zip(chats, chat_ids, targets)):
        accessible = True
        title = "<unknown>"
        members_count = 0
        is_group = False
        is_channel = False
        user_permissions = "no_access"
        error: str | None = None

        try:
            full = await client(
                functions.channels.GetFullChannelRequest(channel=chat_obj)
            )
            title = (
                getattr(chat_obj, "title", "")
                or getattr(chat_obj, "username", "")
                or target
            )
            is_channel = True
            members_count = getattr(full.full_chat, "participants_count", 0)

            try:
                me_info = await client.get_me()
                participant = await client(
                    functions.channels.GetParticipantRequest(
                        channel=chat_obj,
                        participant=me_info,
                    )
                )
                from telethon.tl.types import (
                    ChannelParticipantCreator,
                    ChannelParticipantAdmin,
                    ChannelParticipantSelf,
                )

                if isinstance(
                    participant.participant,
                    (ChannelParticipantCreator, ChannelParticipantSelf),
                ):
                    user_permissions = "admin"
                elif isinstance(participant.participant, ChannelParticipantAdmin):
                    user_permissions = "admin"
                else:
                    user_permissions = "member"
            except ChannelPrivateError:
                user_permissions = "no_access"
                accessible = False
                error = "Нет доступа к каналу (закрытый или удалён)"
            except Exception as e:
                user_permissions = "restricted"
                error = f"Не удалось определить права: {e}"
                logger.warning(
                    "Не удалось определить права участника для канала %s: %s",
                    chat_id,
                    str(e),
                )
        except ChannelPrivateError as e:
            accessible = False
            is_channel = True
            title = getattr(chat_obj, "title", "") or target
            error = f"Канал приватный или недоступен: {e}"
            user_permissions = "no_access"
        except Exception as e:
            accessible = False
            error = f"Ошибка проверки: {e}"
            title = getattr(chat_obj, "title", "") or target
            logger.warning("Ошибка проверки доступа к каналу %s: %s", chat_id, str(e))

        results.append(
            ChannelAccessCheck(
                channel_index=idx,
                chat_id=chat_id,
                target=target,
                accessible=accessible,
                title=title,
                members_count=members_count,
                is_group=is_group,
                is_channel=is_channel,
                user_permissions=user_permissions,
                error=error,
            )
        )
    return results


def log_access_report(
    me: object,
    is_premium: bool,
    channel_checks: list[ChannelAccessCheck],
) -> None:
    """Log a detailed access report."""
    logger.info("=" * 60)
    logger.info("🔐 TELEGRAM ACCOUNT INFO")
    logger.info("=" * 60)
    logger.info(
        "Аккаунт: %s (ID: %s, Premium: %s)",
        getattr(me, "username", "<no username>") or f"ID: {getattr(me, 'id', '?')}",
        getattr(me, "id", "?"),
        "Да ✅" if is_premium else "Нет",
    )
    if hasattr(me, "phone") and getattr(me, "phone"):
        phone = str(me.phone)
        masked = phone[:-4] + "****" if len(phone) > 4 else "****"
        logger.info("Телефон: %s", masked)
    logger.info("")

    logger.info("=" * 60)
    logger.info("📡 КАНАЛЫ (куда отправляются файлы)")
    logger.info("=" * 60)
    all_accessible = True
    for check in channel_checks:
        status = "✅ ДОСТУПЕН" if check.accessible else "❌ НЕДОСТУПЕН"
        logger.info(
            "Канал #%d: %s | %s | Участников: %d | Права: %s",
            check.channel_index + 1,
            status,
            check.title,
            check.members_count,
            check.user_permissions,
        )
        if check.error:
            logger.warning("  ⚠️ %s", check.error)
            all_accessible = False

    logger.info("")
    if all_accessible:
        logger.info("✅ Все каналы доступны, файлы будут отправляться корректно")
    else:
        logger.warning(
            "⚠️ Некоторые каналы недоступны — файлы не смогут быть отправлены туда!"
        )
    logger.info("=" * 60)
