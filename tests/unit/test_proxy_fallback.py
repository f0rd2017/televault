"""Tests for the proxy fallback chain (primary -> backup -> direct)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.core.accounts as accounts_mod
import app.core.utils as utils
import app.core.proxy as proxy_mod
from app.core.accounts import AccountManager, ConnectedAccount
from app.core.types import TelegramAccount

_PROXY_TUPLE = ("socks5", "host", 1080, True, None, None)


def _account(**kwargs) -> TelegramAccount:
    base = dict(
        id=1,
        label="Acc",
        session_path="/tmp/s.session",
        tg_api_id=1,
        tg_api_hash="x",
        chat_target="chan",
    )
    base.update(kwargs)
    return TelegramAccount(**base)


def _fake_resolver(working: set[str]):
    def resolve(raw, *, timeout=6.0):
        if str(raw) in working:
            return _PROXY_TUPLE, f"socks5://{raw}"
        return None, "direct"

    return resolve


# --- chain selection ---------------------------------------------------------


def test_chain_uses_primary_when_it_works(monkeypatch):
    monkeypatch.setattr(
        proxy_mod, "resolve_working_proxy", _fake_resolver({"p1", "p2"})
    )
    proxy, label, tier = utils.select_working_proxy_from_chain(["p1", "p2"])
    assert proxy is _PROXY_TUPLE
    assert tier == 0
    assert "p1" in label


def test_chain_falls_back_to_backup(monkeypatch):
    monkeypatch.setattr(proxy_mod, "resolve_working_proxy", _fake_resolver({"p2"}))
    proxy, label, tier = utils.select_working_proxy_from_chain(["p1", "p2"])
    assert proxy is _PROXY_TUPLE
    assert tier == 1
    assert "p2" in label


def test_chain_falls_back_to_direct(monkeypatch):
    monkeypatch.setattr(proxy_mod, "resolve_working_proxy", _fake_resolver(set()))
    proxy, label, tier = utils.select_working_proxy_from_chain(["p1", "p2"])
    assert proxy is None
    assert label == "direct"
    assert tier == 2


def test_chain_ignores_blank_candidates(monkeypatch):
    monkeypatch.setattr(proxy_mod, "resolve_working_proxy", _fake_resolver({"p2"}))
    proxy, _label, tier = utils.select_working_proxy_from_chain(["", None, "p2"])
    assert proxy is _PROXY_TUPLE
    assert tier == 0  # blanks dropped, p2 is the first real candidate


# --- connect_account walks the chain ----------------------------------------


def _connect_client() -> AsyncMock:
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=True)
    me = MagicMock(premium=False, id=1, username="u")
    client.get_me = AsyncMock(return_value=me)
    chat = MagicMock(id=-1001)
    client.get_entity = AsyncMock(return_value=chat)
    return client


@pytest.mark.asyncio
async def test_connect_account_uses_backup_when_primary_dead(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy_mod, "resolve_working_proxy", _fake_resolver({"backup"}))
    mgr = AccountManager(MagicMock(cache_dir=""), MagicMock())
    acc = _account(
        proxy="primary",
        proxy_backup="backup",
        session_path=str(tmp_path / "s.session"),
    )
    with patch("app.core.accounts.TelegramClient", return_value=_connect_client()):
        ca = await mgr.connect_account(acc)
    assert ca is not None
    assert ca.proxy_tier == 1
    assert "backup" in ca.proxy_label
    assert ca.proxy_chain == ["primary", "backup"]


@pytest.mark.asyncio
async def test_connect_account_falls_back_to_direct(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy_mod, "resolve_working_proxy", _fake_resolver(set()))
    mgr = AccountManager(MagicMock(cache_dir=""), MagicMock())
    acc = _account(
        proxy="primary",
        proxy_backup="backup",
        session_path=str(tmp_path / "s.session"),
    )
    with patch("app.core.accounts.TelegramClient", return_value=_connect_client()):
        ca = await mgr.connect_account(acc)
    assert ca is not None
    assert ca.proxy_tier == 2  # len(chain) => direct
    assert ca.proxy_label == "direct"


# --- live escalation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_escalate_proxy_walks_chain_then_direct(monkeypatch):
    monkeypatch.setattr(accounts_mod, "resolve_working_proxy", _fake_resolver({"p2"}))
    mgr = AccountManager(MagicMock(cache_dir=""), MagicMock())
    client = AsyncMock()
    client.set_proxy = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    ca = ConnectedAccount(
        account=_account(proxy="p1", proxy_backup="p2"),
        client=client,
        is_authorized=True,
        proxy_chain=["p1", "p2"],
        proxy_tier=0,
        proxy_label="socks5://p1",
    )
    mgr._connected[1] = ca

    # First escalation: p1 -> backup p2 (which works).
    label = await mgr.escalate_proxy(client)
    assert ca.proxy_tier == 1
    assert "p2" in label
    client.set_proxy.assert_called_with(_PROXY_TUPLE)

    # Second escalation: p2 -> direct (no more working candidates).
    label = await mgr.escalate_proxy(client)
    assert ca.proxy_tier == 2
    assert label == "direct"
    client.set_proxy.assert_called_with(None)

    # Third escalation: already direct -> no-op.
    calls_before = client.set_proxy.call_count
    label = await mgr.escalate_proxy(client)
    assert label == "direct"
    assert client.set_proxy.call_count == calls_before


@pytest.mark.asyncio
async def test_escalate_proxy_unknown_client_returns_direct():
    mgr = AccountManager(MagicMock(cache_dir=""), MagicMock())
    assert await mgr.escalate_proxy(AsyncMock()) == "direct"
