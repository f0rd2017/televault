"""Авто-эскалация прокси при устойчивых сбоях соединения посреди сессии."""

from __future__ import annotations

import asyncio

import pytest
from telethon.errors import RPCError

from app.tg.proxy_escalation import ProxyEscalationMixin


class _Guard(ProxyEscalationMixin):
    """Голый носитель mixin'а: фиксирует вызовы эскалатора."""

    def __init__(self) -> None:
        self.calls: list[object] = []

        async def _escalate(client) -> str:
            self.calls.append(client)
            return "backup"

        self.proxy_escalator = _escalate


async def test_connection_error_triggers_escalation() -> None:
    guard = _Guard()
    client = object()
    await guard._on_persistent_connection_failure(client, ConnectionError("dead"))
    assert guard.calls == [client]


async def test_timeout_error_triggers_escalation() -> None:
    guard = _Guard()
    client = object()
    await guard._on_persistent_connection_failure(client, TimeoutError("slow"))
    assert guard.calls == [client]


async def test_non_connection_errors_do_not_escalate() -> None:
    guard = _Guard()
    client = object()
    # RPCError: сервер ответил — прокси жив. OSError: может быть диск (ENOSPC).
    await guard._on_persistent_connection_failure(client, RPCError(None, "err"))
    await guard._on_persistent_connection_failure(client, OSError(28, "ENOSPC"))
    await guard._on_persistent_connection_failure(client, RuntimeError("misc"))
    assert guard.calls == []


async def test_cooldown_limits_escalation_rate(monkeypatch) -> None:
    guard = _Guard()
    client = object()
    fake_now = [1000.0]
    monkeypatch.setattr("app.tg.proxy_escalation.time.monotonic", lambda: fake_now[0])

    await guard._on_persistent_connection_failure(client, ConnectionError("x"))
    await guard._on_persistent_connection_failure(client, ConnectionError("x"))
    assert len(guard.calls) == 1  # вторая — внутри cooldown

    fake_now[0] += guard._PROXY_ESCALATION_COOLDOWN_SEC + 1
    await guard._on_persistent_connection_failure(client, ConnectionError("x"))
    assert len(guard.calls) == 2  # после cooldown — снова можно

    # Cooldown ПО-КЛИЕНТСКИ: другой клиент эскалируется независимо.
    other = object()
    await guard._on_persistent_connection_failure(other, ConnectionError("x"))
    assert guard.calls[-1] is other


async def test_no_escalator_is_noop() -> None:
    guard = _Guard()
    guard.proxy_escalator = None
    await guard._on_persistent_connection_failure(object(), ConnectionError("x"))
    assert guard.calls == []


async def test_escalator_exception_is_swallowed() -> None:
    guard = _Guard()

    async def _boom(client) -> str:
        raise RuntimeError("escalation infra broke")

    guard.proxy_escalator = _boom
    # Не должно бросить: исходная ошибка передачи важнее проблем эскалации.
    await guard._on_persistent_connection_failure(object(), ConnectionError("x"))


async def test_send_with_retry_escalates_on_exhausted_connection_errors() -> None:
    """Интеграционно: воронка _send_with_retry после исчерпания ретраев
    ошибкой соединения зовёт эскалатор и пробрасывает исходную ошибку."""
    from app.core.types import AppConfig, RetryConfig
    from app.tg.upload.uploader import TgUploader

    class _DeadClient:
        async def send_file(self, *a, **k):
            raise ConnectionError("proxy is gone")

    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="a" * 32,
        tg_session_path="unused",
        cache_dir="unused",
        retry=RetryConfig(max_attempts=2, base_delay=0.0),
    )
    client = _DeadClient()
    uploader = TgUploader(
        config=config,
        repo=None,
        client=client,
        chat=object(),
        chat_id="1",
    )

    escalated: list[object] = []

    async def _escalate(c) -> str:
        escalated.append(c)
        return "backup"

    uploader.proxy_escalator = _escalate

    # Быстрые ретраи: без реальных пауз tenacity/лимитеров.
    async def _noop_acquire(*a, **k) -> None:
        return None

    uploader._send_media_limiter.acquire = _noop_acquire
    uploader._upload_bandwidth.acquire = _noop_acquire

    with pytest.raises(ConnectionError):
        await asyncio.wait_for(
            uploader._send_with_retry(payload=b"data", caption="c", file_name="f.bin"),
            timeout=30.0,
        )
    assert escalated == [client]
