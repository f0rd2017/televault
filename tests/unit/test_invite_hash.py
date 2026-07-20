from __future__ import annotations

from televault.core.accounts import _parse_invite_hash


def test_parses_plus_invite_link():
    assert _parse_invite_hash("https://t.me/+AbCdEfGh12345678") == "AbCdEfGh12345678"


def test_parses_joinchat_link():
    assert _parse_invite_hash("https://t.me/joinchat/AbCdEf123") == "AbCdEf123"


def test_parses_bare_plus_hash():
    assert _parse_invite_hash("+X_9yZwVuTs87654r") == "X_9yZwVuTs87654r"


def test_parses_without_scheme():
    assert _parse_invite_hash("t.me/+Qq7W3_-eRt12YuI9") == "Qq7W3_-eRt12YuI9"


def test_public_username_has_no_invite_hash():
    assert _parse_invite_hash("https://t.me/somechannel") is None
    assert _parse_invite_hash("@somechannel") is None


def test_empty_or_chat_id_returns_none():
    assert _parse_invite_hash("") is None
    assert _parse_invite_hash("   ") is None
    assert _parse_invite_hash("3978587188") is None
