"""Парсинг и подбор прокси (SOCKS5/HTTP/MTProto) для Telethon."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)


def parse_proxy(raw: str) -> tuple[str, int, str | None, str | None, bool, str]:
    """
    Парсит прокси строку с автоопределением типа.
    Поддерживаемые форматы:
      - socks5://host:port  или  socks5://user:pass@host:port
      - http://host:port  или  http://user:pass@host:port
      - host:port:user:pass  (автоопределение — по умолчанию SOCKS5)
      - host:port  (автоопределение — по умолчанию SOCKS5)

    Возвращает: (host, port, username, password, rdns, proxy_type)
    proxy_type: 'socks5' или 'http'
    """
    value = str(raw or "").strip()
    if not value:
        raise ValueError("Proxy string is empty")

    # URL form: scheme://user:pass@host:port
    if "://" in value:
        parsed = urlparse(value)
        scheme = str(parsed.scheme or "").strip().lower()

        if scheme in {"socks5", "socks5h"}:
            proxy_type = "socks5"
        elif scheme in {"http", "https"}:
            proxy_type = "http"
        else:
            raise ValueError(
                f"Unsupported proxy scheme: {scheme}. Use socks5:// or http://"
            )

        host = str(parsed.hostname or "").strip()
        port = int(parsed.port or 0)
        if not host:
            raise ValueError("Proxy host is missing")
        if port <= 0 or port > 65535:
            raise ValueError("Proxy port must be in range 1..65535")
        username = unquote(parsed.username) if parsed.username else None
        password = unquote(parsed.password) if parsed.password else None
        rdns = scheme == "socks5h"
        return host, port, username, password, rdns, proxy_type

    # Short form: host:port[:username:password]
    parts = [p.strip() for p in value.split(":")]
    if len(parts) not in {2, 4}:
        raise ValueError("Proxy must be host:port or host:port:user:pass")

    host = parts[0]
    if not host:
        raise ValueError("Proxy host is missing")
    try:
        port = int(parts[1])
    except ValueError as exc:
        raise ValueError("Proxy port must be integer") from exc
    if port <= 0 or port > 65535:
        raise ValueError("Proxy port must be in range 1..65535")

    username: str | None = None
    password: str | None = None
    if len(parts) == 4:
        username = parts[2] or None
        password = parts[3] or None
        if not username or not password:
            raise ValueError("Both proxy username and password are required")

    # По умолчанию SOCKS5 с удалённым DNS
    return host, port, username, password, True, "socks5"


def parse_socks5_proxy(raw: str) -> tuple[str, int, str | None, str | None, bool]:
    """Обратная совместимость — делегирует parse_proxy."""
    host, port, username, password, rdns, _ = parse_proxy(raw)
    return host, port, username, password, rdns


# ── MTProto-прокси (в дополнение к SOCKS5/HTTP) ──────────────────────────────
# MTProto-прокси телеграма устроен иначе: вместо логина/пароля — hex-секрет, и
# Telethon требует особый класс соединения (ConnectionTcpMTProxy*). Внутри проги
# такой прокси представляем кортежем-с-маркером ('mtproxy', host, port, secret),
# чтобы он проходил по той же цепочке выбора, что и socks/http.

_MTPROXY_SCHEMES = {"mtproto", "mtproxy", "mtp"}


def is_mtproxy(raw: str | None) -> bool:
    """Похоже ли на MTProto-прокси (по схеме/ссылке), а не socks/http."""
    value = str(raw or "").strip()
    if not value:
        return False
    lowered = value.lower()
    if "://" in lowered:
        scheme = lowered.split("://", 1)[0]
        if scheme in _MTPROXY_SCHEMES:
            return True
        if (
            scheme in {"tg", "https", "http"}
            and "proxy" in lowered
            and "secret=" in lowered
        ):
            return True
    return False


def parse_mtproxy(raw: str) -> tuple[str, int, str]:
    """Парсит MTProto-прокси в (host, port, secret_hex).

    Форматы:
      - tg://proxy?server=H&port=P&secret=S
      - https://t.me/proxy?server=H&port=P&secret=S
      - mtproto://H:P:S  /  mtproxy://H:P:S
      - mtproto://H:P?secret=S
    """
    value = str(raw or "").strip()
    if not value:
        raise ValueError("Proxy string is empty")

    host = ""
    port = 0
    secret = ""

    if "://" in value:
        parsed = urlparse(value)
        params = parse_qs(parsed.query)

        def _q(name: str) -> str:
            vals = params.get(name)
            return str(vals[0]).strip() if vals else ""

        # tg://proxy?... и t.me/proxy?... — параметры в query.
        secret = _q("secret")
        host = _q("server")
        port_q = _q("port")
        if port_q:
            try:
                port = int(port_q)
            except ValueError as exc:
                raise ValueError("Proxy port must be integer") from exc

        # mtproto://host:port[:secret] — host:port:secret в netloc, поэтому
        # parsed.port/hostname ненадёжны (бросают на трёх сегментах); парсим вручную.
        if not host or not port or not secret:
            segs = parsed.netloc.split(":")
            if not host and segs and segs[0]:
                host = segs[0]
            if not port and len(segs) >= 2 and segs[1]:
                try:
                    port = int(segs[1])
                except ValueError as exc:
                    raise ValueError("Proxy port must be integer") from exc
            if not secret and len(segs) >= 3 and segs[2]:
                secret = segs[2].strip()
    else:
        # Короткая форма host:port:secret
        parts = [p.strip() for p in value.split(":")]
        if len(parts) != 3:
            raise ValueError(
                "MTProto proxy must be host:port:secret or a tg://proxy?... link"
            )
        host = parts[0]
        try:
            port = int(parts[1])
        except ValueError as exc:
            raise ValueError("Proxy port must be integer") from exc
        secret = parts[2]

    if not host:
        raise ValueError("Proxy host is missing")
    if port <= 0 or port > 65535:
        raise ValueError("Proxy port must be in range 1..65535")
    secret = secret.strip()
    if not secret:
        raise ValueError("MTProto proxy secret is missing")
    return host, port, secret


def _telethon_proxy_tuple(
    proxy_type: str,
    host: str,
    port: int,
    rdns: bool,
    username: str | None,
    password: str | None,
) -> tuple:
    """Кортеж в формате, который принимает Telethon (через python_socks).

    proxy_type — 'socks5' | 'socks4' | 'http'. Telethon 1.36+ с python_socks
    поддерживает все три.
    """
    if username is not None or password is not None:
        return (proxy_type, host, int(port), bool(rdns), username, password)
    return (proxy_type, host, int(port), bool(rdns))


def build_telethon_proxy(raw: str | None) -> tuple | None:
    """
    Создаёт прокси-кортеж для Telethon с автоопределением типа (SOCKS5/HTTP).

    Telethon (через python_socks) поддерживает SOCKS5, SOCKS4 и HTTP. Тип
    берётся из схемы (socks5:// / http://); для короткой формы host:port:user:pass
    по умолчанию SOCKS5 — реальную проверку/выбор делает resolve_working_proxy.
    """
    value = str(raw or "").strip()
    if not value:
        return None

    if is_mtproxy(value):
        host, port, secret = parse_mtproxy(value)
        return ("mtproxy", host, int(port), secret)

    host, port, username, password, rdns, proxy_type = parse_proxy(value)
    return _telethon_proxy_tuple(proxy_type, host, port, rdns, username, password)


def _probe_tcp(host: str, port: int, timeout: float) -> bool:
    """Базовая TCP-проба достижимости (для MTProto-прокси: python_socks его не
    умеет проверять как socks). Не гарантирует, что секрет верный — только что
    хост:порт принимает соединение."""
    import socket

    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError as exc:
        logger.debug("MTProxy TCP probe failed (%s:%s): %s", host, port, exc)
        return False


def telethon_client_kwargs(proxy: tuple | None) -> dict[str, Any]:
    """По внутреннему прокси-кортежу собрать kwargs для ``TelegramClient``.

    MTProto-прокси (маркер 'mtproxy') требует особый класс соединения и формат
    ``(host, port, secret)``; socks/http — обычный ``proxy=``. None → пусто.
    """
    if proxy is None:
        return {}
    if isinstance(proxy, tuple) and len(proxy) == 4 and proxy[0] == "mtproxy":
        from telethon import connection as _conn

        _, host, port, secret = proxy
        return {
            "connection": _conn.ConnectionTcpMTProxyRandomizedIntermediate,
            "proxy": (host, int(port), secret),
        }
    return {"proxy": proxy}


def proxy_for_set_proxy(proxy: tuple | None) -> tuple | None:
    """Аргумент для ``client.set_proxy`` (эскалация на лету). MTProto-кортеж
    приводим к ``(host, port, secret)``; остальные — без изменений.

    Замечание: ``set_proxy`` не меняет класс соединения, поэтому переключение
    между MTProto и socks/http на лету — best-effort (полноценно режим задаётся
    при конструировании клиента)."""
    if isinstance(proxy, tuple) and len(proxy) == 4 and proxy[0] == "mtproxy":
        _, host, port, secret = proxy
        return (host, int(port), secret)
    return proxy


def probe_proxy(
    host: str,
    port: int,
    proxy_type: str,
    username: str | None = None,
    password: str | None = None,
    *,
    timeout: float = 6.0,
    dest_host: str = "149.154.167.51",  # Telegram DC2
    dest_port: int = 443,
) -> bool:
    """Проверяет, что через прокси реально устанавливается соединение до Telegram.

    Блокирующая функция (python_socks.sync) — вызывать из потока/executor.
    """
    try:
        from python_socks import ProxyType
        from python_socks.sync import Proxy
    except Exception as exc:  # python_socks недоступен
        logger.debug("python_socks unavailable for proxy probe: %s", exc)
        return False

    type_map = {
        "socks5": ProxyType.SOCKS5,
        "socks4": ProxyType.SOCKS4,
        "http": ProxyType.HTTP,
    }
    pt = type_map.get(str(proxy_type).lower())
    if pt is None:
        return False

    sock = None
    try:
        proxy = Proxy.create(
            proxy_type=pt,
            host=host,
            port=int(port),
            username=username or None,
            password=password or None,
        )
        sock = proxy.connect(dest_host=dest_host, dest_port=dest_port, timeout=timeout)
        return True
    except Exception as exc:
        logger.debug("Proxy probe failed (%s://%s:%s): %s", proxy_type, host, port, exc)
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def resolve_working_proxy(
    raw: str | None,
    *,
    timeout: float = 6.0,
) -> tuple[tuple | None, str]:
    """Подбирает рабочий прокси для Telethon, проверяя доступность.

    - Если схема задана явно (socks5://, http://) — проверяется только этот тип.
    - Для короткой формы host:port[:user:pass] тип определяется автоматически:
      сначала пробуется SOCKS5, затем HTTP.
    - Если ни один тип не отвечает — возвращается (None, ...), и вызывающий код
      может подключиться напрямую.

    Возвращает: (telethon_proxy | None, человекочитаемая_метка).
    """
    value = str(raw or "").strip()
    if not value:
        return None, "direct"

    if is_mtproxy(value):
        try:
            host, port, secret = parse_mtproxy(value)
        except ValueError as exc:
            logger.warning(
                "Invalid MTProto proxy '%s' (%s) — will connect directly", raw, exc
            )
            return None, "direct (invalid proxy)"
        if _probe_tcp(host, port, timeout=timeout):
            return ("mtproxy", host, int(port), secret), f"mtproxy://{host}:{int(port)}"
        return None, f"direct (mtproxy {host}:{int(port)} unreachable)"

    try:
        host, port, username, password, rdns, declared_type = parse_proxy(value)
    except ValueError as exc:
        logger.warning("Invalid proxy '%s' (%s) — will connect directly", raw, exc)
        return None, "direct (invalid proxy)"

    explicit_scheme = "://" in value
    candidates = [declared_type] if explicit_scheme else ["socks5", "http"]

    for ptype in candidates:
        if probe_proxy(host, port, ptype, username, password, timeout=timeout):
            label = f"{ptype}://{host}:{int(port)}"
            return _telethon_proxy_tuple(
                ptype, host, port, rdns, username, password
            ), label

    return None, f"direct (proxy {host}:{int(port)} unreachable)"


def select_working_proxy_from_chain(
    candidates: list[str | None],
    *,
    timeout: float = 6.0,
) -> tuple[tuple | None, str, int]:
    """Подобрать первый рабочий прокси из цепочки кандидатов.

    Кандидаты пробуются по порядку (например, [основной, резервный]). Возвращает
    ``(telethon_proxy | None, метка, tier)``, где ``tier`` — индекс выбранного
    прокси в очищенном списке, либо его длина, если все недоступны (→ direct).
    """
    cleaned = [str(c).strip() for c in candidates if str(c or "").strip()]
    for idx, candidate in enumerate(cleaned):
        proxy, label = resolve_working_proxy(candidate, timeout=timeout)
        if proxy is not None:
            return proxy, label, idx
    return None, "direct", len(cleaned)


def proxy_endpoint(raw: str | None) -> str:
    value = str(raw or "").strip()
    if not value:
        return "-"
    try:
        if is_mtproxy(value):
            host, port, _secret = parse_mtproxy(value)
            return f"mtproxy://{host}:{int(port)}"
        host, port, _username, _password, _rdns, proxy_type = parse_proxy(value)
        return f"{proxy_type}://{host}:{int(port)}"
    except Exception:
        return "invalid"
