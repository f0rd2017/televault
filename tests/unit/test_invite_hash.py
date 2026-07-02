from __future__ import annotations

from app.core.accounts import _parse_invite_hash


def test_parses_plus_invite_link():
    assert _parse_invite_hash("https://t.me/+4GlhQFIW3tQ5Mjdi") == "4GlhQFIW3tQ5Mjdi"


def test_parses_joinchat_link():
    assert _parse_invite_hash("https://t.me/joinchat/AbCdEf123") == "AbCdEf123"


def test_parses_bare_plus_hash():
    assert _parse_invite_hash("+T_5bzFvJJflmY2Qy") == "T_5bzFvJJflmY2Qy"


def test_parses_without_scheme():
    assert _parse_invite_hash("t.me/+LS2E4_-jAu85NzM6") == "LS2E4_-jAu85NzM6"


def test_public_username_has_no_invite_hash():
    assert _parse_invite_hash("https://t.me/somechannel") is None
    assert _parse_invite_hash("@somechannel") is None


def test_empty_or_chat_id_returns_none():
    assert _parse_invite_hash("") is None
    assert _parse_invite_hash("   ") is None
    assert _parse_invite_hash("3978587188") is None
