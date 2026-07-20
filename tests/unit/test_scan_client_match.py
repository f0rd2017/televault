"""Regression for matching a client by chat_id in TgScanner.

It used to use lstrip("100"), which greedily strips any leading
'1'/'0' characters rather than the "-100" prefix of Telegram supergroups, so it could
pick the wrong client/account.
"""

from __future__ import annotations

import pytest

from televault.core.types import AppConfig
from televault.tg.scan import TgScanner


def _make_config() -> AppConfig:
    return AppConfig(
        tg_api_id=1,
        tg_api_hash="hash",
        tg_session_path="/tmp/s.session",
        cache_dir="/tmp/cache",
    )


def test_normalize_chat_id_strips_only_real_prefix() -> None:
    norm = TgScanner._normalize_chat_id_for_match
    # Full supergroup peer format.
    assert norm("-1001234567890") == "1234567890"
    # A regular negative id.
    assert norm("-987654") == "987654"
    # A bare id is not mangled (lstrip would eat the leading '1').
    assert norm("1234567890") == "1234567890"
    # An id that starts with '1'/'0' but has no prefix stays as-is.
    assert norm("1023456") == "1023456"


def test_get_client_matches_peer_form_to_bare_id() -> None:
    config = _make_config()
    bare_client = object()
    scanner = TgScanner(
        config,
        repo=None,  # unused when selecting a client
        chats=[object()],
        chat_ids=["1234567890"],
        client_by_chat_id={"1234567890": bare_client},
    )
    # A peer-form query must find the client registered by the bare id.
    assert scanner._get_client_for_chat("-1001234567890") is bare_client
    # And the exact match still works.
    assert scanner._get_client_for_chat("1234567890") is bare_client


def test_get_client_falls_back_to_only_mapped_client() -> None:
    config = _make_config()
    only = object()
    scanner = TgScanner(
        config,
        repo=None,
        chats=[object()],
        chat_ids=["111"],
        client_by_chat_id={"111": only},
    )
    # An unknown chat_id with no main client returns the only one in the mapping.
    assert scanner._get_client_for_chat("999") is only


def test_get_client_raises_without_any_client() -> None:
    config = _make_config()
    # Multi-channel mode with no clients: the mapping is empty, no main client.
    scanner = TgScanner(
        config,
        repo=None,
        chats=[object()],
        chat_ids=["111"],
    )
    with pytest.raises(ValueError):
        scanner._get_client_for_chat("111")
