"""Регрессия на сопоставление клиента по chat_id в TgScanner.

Раньше использовался lstrip("100"), который жадно убирает любые ведущие
символы '1'/'0', а не префикс "-100" супергрупп Telegram, из-за чего мог
выбираться неправильный клиент/аккаунт.
"""

from __future__ import annotations

import pytest

from app.core.types import AppConfig
from app.tg.scan import TgScanner


def _make_config() -> AppConfig:
    return AppConfig(
        tg_api_id=1,
        tg_api_hash="hash",
        tg_session_path="/tmp/s.session",
        cache_dir="/tmp/cache",
    )


def test_normalize_chat_id_strips_only_real_prefix() -> None:
    norm = TgScanner._normalize_chat_id_for_match
    # Полный peer-формат супергруппы.
    assert norm("-1001234567890") == "1234567890"
    # Обычный отрицательный id.
    assert norm("-987654") == "987654"
    # Голый id не калечится (lstrip съел бы ведущую '1').
    assert norm("1234567890") == "1234567890"
    # id, который начинается с '1'/'0' но без префикса, остаётся как есть.
    assert norm("1023456") == "1023456"


def test_get_client_matches_peer_form_to_bare_id() -> None:
    config = _make_config()
    bare_client = object()
    scanner = TgScanner(
        config,
        repo=None,  # не используется при выборе клиента
        chats=[object()],
        chat_ids=["1234567890"],
        client_by_chat_id={"1234567890": bare_client},
    )
    # Запрос в peer-форме должен найти клиента, зарегистрированного по голому id.
    assert scanner._get_client_for_chat("-1001234567890") is bare_client
    # И точное совпадение по-прежнему работает.
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
    # Незнакомый chat_id без main-клиента отдаёт единственного в маппинге.
    assert scanner._get_client_for_chat("999") is only


def test_get_client_raises_without_any_client() -> None:
    config = _make_config()
    # Мульти-канальный режим без клиентов: маппинг пуст, main-клиента нет.
    scanner = TgScanner(
        config,
        repo=None,
        chats=[object()],
        chat_ids=["111"],
    )
    with pytest.raises(ValueError):
        scanner._get_client_for_chat("111")
