"""
AccountManager — manages multiple Telegram user accounts for uploads.
Each account = a separate session + its own channel = an independent ~12 MB/s stream.
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
    """Extract the invite hash from a private channel invite.

    Supports ``t.me/+HASH``, ``t.me/joinchat/HASH`` (with any scheme/domain)
    and a bare ``+HASH``. Returns None for public links/usernames — those
    cannot be joined via ImportChatInvite.
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
    """A connected account with its client."""

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
    """Manages multiple Telegram accounts."""

    def __init__(self, config: AppConfig, repo: DbRepo) -> None:
        self.config = config
        self.repo = repo
        self._connected: dict[int, ConnectedAccount] = {}
        self._escalate_lock = asyncio.Lock()

    async def load_and_connect_all(self) -> list[ConnectedAccount]:
        """Load all accounts from the DB and connect them."""
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
        """Connect a single account."""
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
                # Update the info in the DB
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

            # Resolve the chat — try several approaches with retry
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
                    # Auto-join: if this is an invite link and the account isn't
                    # in the channel yet — join via the hash and immediately retry
                    # the resolve (one attempt).
                    if invite_hash and not join_attempted:
                        join_attempted = True
                        if await self._try_join_via_invite(
                            client, invite_hash, account.label
                        ):
                            continue
                    chat_resolve_attempts += 1
                    if chat_resolve_attempts >= max_chat_resolve_attempts:
                        # Couldn't resolve via the invite link — falling back to
                        # the chat_id from the DB (if the account has connected
                        # to this channel before)
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
        """Join a private channel via an invite hash. Returns True if the account
        is now definitely a member (just joined, or was already a participant)."""
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
        """Add a new account to the DB and connect it. Returns its ID or None."""
        data = asdict(account)
        data["id"] = 0  # AUTOINCREMENT
        new_account = TelegramAccount(**data)
        account_id = self.repo.insert_account(new_account)
        # Re-read with the correct ID
        saved_account = self.repo.get_account(account_id)
        if saved_account is None:
            return None
        ca = await self.connect_account(saved_account)
        return account_id if ca else None

    async def remove_account(self, account_id: int) -> None:
        """Delete an account and disconnect it."""
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
        """Disconnect all accounts."""
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
        """Switch the client to the next tier of the proxy chain on the fly.

        Called when the connection through the current proxy keeps failing
        mid-session: we try the next candidate in the chain (backup proxy),
        then direct. Applied via ``client.set_proxy`` plus a reconnect.
        Returns the label of the new tier; once at ``direct`` it won't move
        any further.
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
        """Return authorized accounts with a resolved chat.
        chat_obj must not be None for the upload to work correctly."""
        return [
            ca
            for ca in self._connected.values()
            if ca.is_authorized and ca.chat_obj is not None
        ]
