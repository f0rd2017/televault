"""Tests for AccountManager: connection, filtering, proxy handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from televault.core.accounts import AccountManager, ConnectedAccount
from televault.core.types import TelegramAccount


def _make_account(
    account_id: int = 1,
    label: str = "Test",
    chat_target: str = "test_channel",
    proxy: str = "",
    is_primary: bool = False,
    session_path: str = "/tmp/test.session",
) -> TelegramAccount:
    return TelegramAccount(
        id=account_id,
        label=label,
        session_path=session_path,
        tg_api_id=12345,
        tg_api_hash="abc123def456",
        chat_target=chat_target,
        proxy=proxy,
        is_primary=is_primary,
    )


class FakeRepo:
    """Minimal fake DbRepo."""

    def __init__(self) -> None:
        self._accounts: list[TelegramAccount] = []
        self._updated: dict[int, dict] = {}

    def list_accounts(self) -> list[TelegramAccount]:
        return list(self._accounts)

    def add(self, acc: TelegramAccount) -> None:
        self._accounts.append(acc)

    def update_account(self, account_id: int, **kwargs) -> None:
        self._updated.setdefault(account_id, {}).update(kwargs)

    def get_account(self, account_id: int) -> TelegramAccount | None:
        for a in self._accounts:
            if a.id == account_id:
                return a
        return None


def _make_fake_client(authorized: bool = True, premium: bool = False, chat_obj=None):
    """Create a mock TelegramClient."""
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=authorized)
    me = MagicMock()
    me.premium = premium
    me.id = 123456
    me.username = "testuser"
    client.get_me = AsyncMock(return_value=me)
    if chat_obj is not None:
        client.get_entity = AsyncMock(return_value=chat_obj)
    else:
        client.get_entity = AsyncMock(
            side_effect=ValueError("Could not find the entity")
        )
    return client


@pytest.mark.asyncio
async def test_connect_account_success(tmp_path) -> None:
    """Account connects successfully when everything works."""
    repo = FakeRepo()
    mgr = AccountManager(MagicMock(cache_dir=""), repo)

    chat_mock = MagicMock()
    chat_mock.id = -1001234567890
    client = _make_fake_client(authorized=True, chat_obj=chat_mock)

    acc = _make_account(account_id=1, session_path=str(tmp_path / "s.session"))

    with patch("televault.core.accounts.TelegramClient", return_value=client):
        ca = await mgr.connect_account(acc)

    assert ca is not None
    assert ca.is_authorized is True
    assert ca.chat_obj is chat_mock
    assert ca.chat_id == "-1001234567890"
    assert 1 in mgr._connected


@pytest.mark.asyncio
async def test_connect_account_chat_resolve_fails_returns_ca(tmp_path) -> None:
    """If get_entity() fails but account is authorized, still returns ConnectedAccount."""
    repo = FakeRepo()
    mgr = AccountManager(MagicMock(cache_dir=""), repo)

    client = _make_fake_client(authorized=True, chat_obj=None)

    acc = _make_account(account_id=1, session_path=str(tmp_path / "s.session"))

    with patch("televault.core.accounts.TelegramClient", return_value=client):
        ca = await mgr.connect_account(acc)

    # Account IS returned even if chat unresolved
    assert ca is not None
    assert ca.is_authorized is True
    assert ca.chat_obj is None
    # Account is in _connected but NOT in active endpoints (chat_obj required for upload)
    assert 1 in mgr._connected
    assert len(mgr.get_active_endpoints()) == 0


@pytest.mark.asyncio
async def test_connect_account_not_authorized_still_returns_ca(tmp_path) -> None:
    """If not authorized, returns ConnectedAccount but is_authorized=False.
    It will be filtered out by get_active_endpoints()."""
    repo = FakeRepo()
    mgr = AccountManager(MagicMock(cache_dir=""), repo)

    client = _make_fake_client(authorized=False)

    acc = _make_account(account_id=1, session_path=str(tmp_path / "s.session"))

    with patch("televault.core.accounts.TelegramClient", return_value=client):
        ca = await mgr.connect_account(acc)

    # Account IS returned but not authorized
    assert ca is not None
    assert ca.is_authorized is False
    assert ca.chat_obj is None  # chat resolution skipped
    # Will be filtered by get_active_endpoints()
    assert mgr.get_active_endpoints() == []


@pytest.mark.asyncio
async def test_connect_account_connect_fails_returns_none(tmp_path) -> None:
    """If client.connect() fails, returns None."""
    repo = FakeRepo()
    mgr = AccountManager(MagicMock(cache_dir=""), repo)

    client = AsyncMock()
    client.connect = AsyncMock(side_effect=ConnectionError("network error"))
    client.disconnect = AsyncMock()

    acc = _make_account(account_id=1, session_path=str(tmp_path / "s.session"))

    with patch("televault.core.accounts.TelegramClient", return_value=client):
        ca = await mgr.connect_account(acc)

    assert ca is None


@pytest.mark.asyncio
async def test_get_active_endpoints_returns_all_authorized() -> None:
    """get_active_endpoints returns ALL authorized accounts, even those with unresolved chat."""
    repo = FakeRepo()
    mgr = AccountManager(MagicMock(cache_dir=""), repo)

    # Account 1: fully connected
    ca1 = ConnectedAccount(
        account=_make_account(account_id=1, label="Acc1"),
        client=AsyncMock(),
        is_authorized=True,
        chat_obj=MagicMock(),
        chat_id="-1001",
        is_premium=False,
    )
    # Account 2: authorized but chat unresolved (common with proxy)
    ca2 = ConnectedAccount(
        account=_make_account(account_id=2, label="Acc2", proxy="1.2.3.4:1080"),
        client=AsyncMock(),
        is_authorized=True,
        chat_obj=None,
        chat_id="",
        is_premium=False,
    )
    # Account 3: not authorized
    ca3 = ConnectedAccount(
        account=_make_account(account_id=3, label="Acc3"),
        client=AsyncMock(),
        is_authorized=False,
        chat_obj=None,
        chat_id="",
        is_premium=False,
    )

    mgr._connected[1] = ca1
    mgr._connected[2] = ca2
    mgr._connected[3] = ca3

    active = mgr.get_active_endpoints()
    # Only accounts with chat_obj are returned (chat_obj is required for upload)
    assert len(active) == 1
    labels = {ca.account.label for ca in active}
    assert labels == {"Acc1"}
    assert "Acc2" not in labels  # excluded because chat_obj is None
    assert "Acc3" not in labels  # excluded because not authorized


@pytest.mark.asyncio
async def test_get_active_endpoints_excludes_not_authorized() -> None:
    """get_active_endpoints excludes accounts that are not authorized."""
    repo = FakeRepo()
    mgr = AccountManager(MagicMock(cache_dir=""), repo)

    ca = ConnectedAccount(
        account=_make_account(account_id=1),
        client=AsyncMock(),
        is_authorized=False,
        chat_obj=MagicMock(),
        chat_id="-1001",
    )
    mgr._connected[1] = ca

    assert mgr.get_active_endpoints() == []


@pytest.mark.asyncio
async def test_load_and_connect_all_connects_all(tmp_path) -> None:
    """load_and_connect_all connects all accounts from DB."""
    repo = FakeRepo()

    chat_mock = MagicMock()
    chat_mock.id = -100123

    clients = []
    for i in range(3):
        c = _make_fake_client(authorized=True, chat_obj=chat_mock)
        clients.append(c)

    for i in range(3):
        acc = _make_account(
            account_id=i + 1,
            session_path=str(tmp_path / f"acc{i}.session"),
        )
        repo.add(acc)

    mgr = AccountManager(MagicMock(cache_dir=""), repo)

    call_idx = 0

    async def fake_connect_account(account):
        nonlocal call_idx
        client = clients[call_idx]
        call_idx += 1
        ca = ConnectedAccount(
            account=account,
            client=client,
            is_authorized=True,
            chat_obj=chat_mock,
            chat_id=str(chat_mock.id),
            is_premium=False,
        )
        mgr._connected[account.id] = ca
        return ca

    with patch.object(mgr, "connect_account", fake_connect_account):
        connected = await mgr.load_and_connect_all()

    assert len(connected) == 3
    assert len(mgr.get_active_endpoints()) == 3
