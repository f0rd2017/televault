"""Авто-эскалация прокси при устойчивых сбоях соединения посреди сессии.

Цепочка primary→backup→direct применяется при ПОДКЛЮЧЕНИИ аккаунта
(``app.core.accounts``). Этот mixin закрывает второй случай: прокси умер УЖЕ
ПОСЛЕ подключения, посреди передачи.

Триггер сознательно консервативный (горячий путь передач не трогаем — ни
одной проверки на успешном пути, накладные расходы возникают только на уже
проваленной операции):

- только когда tenacity исчерпал ВСЕ ретраи, т.е. max_attempts подряд упали;
- только на ошибках уровня соединения: ``ConnectionError``/``TimeoutError``.
  ``RPCError`` не считается (сервер ответил — прокси жив), «широкий»
  ``OSError`` тоже (дисковая ошибка вроде ENOSPC не повод уводить трафик с
  рабочего прокси);
- не чаще одного раза на клиента за ``_PROXY_ESCALATION_COOLDOWN_SEC``.

Сама провалившаяся джоба НЕ перезапускается — следующая попытка/джоба пойдёт
уже через новый уровень цепочки.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class ProxyEscalationMixin:
    """Подмешивается в TgUploader/TgDownloader; см. модульный docstring."""

    # Ставит worker после конструирования: async callable(client) -> str
    # (обычно AccountManager.escalate_proxy). None = фича выключена.
    proxy_escalator = None
    _PROXY_ESCALATION_COOLDOWN_SEC = 120.0

    async def _on_persistent_connection_failure(self, client, exc) -> None:
        """Вызывать из ``except`` воронок ретраев, когда ретраи исчерпаны.

        Никогда не бросает: исходная ошибка передачи важнее проблем эскалации.
        """
        escalator = getattr(self, "proxy_escalator", None)
        if escalator is None or client is None:
            return
        if not isinstance(exc, (ConnectionError, TimeoutError)):
            return
        last_map = getattr(self, "_proxy_escalation_last", None)
        if last_map is None:
            last_map = {}
            self._proxy_escalation_last = last_map
        now = time.monotonic()
        last = last_map.get(id(client), float("-inf"))
        if now - last < self._PROXY_ESCALATION_COOLDOWN_SEC:
            return
        last_map[id(client)] = now
        try:
            label = await escalator(client)
        except Exception:  # noqa: BLE001 — не маскировать исходную ошибку
            logger.exception("Proxy escalation attempt failed")
            return
        logger.warning(
            "Persistent connection failure (%s: %s) — proxy escalated to '%s'",
            type(exc).__name__,
            exc,
            label,
        )
