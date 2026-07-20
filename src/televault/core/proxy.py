"""Parsing and selecting proxies (SOCKS5/HTTP/MTProto) for Telethon."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)


def parse_proxy(raw: str) -> tuple[str, int, str | None, str | None, bool, str]:
    """
    Parses a proxy string with automatic type detection.
    Supported formats:
      - socks5://host:port  or  socks5://user:pass@host:port
      - http://host:port  or  http://user:pass@host:port
      - host:port:user:pass  (auto-detected — defaults to SOCKS5)
      - host:port  (auto-detected — defaults to SOCKS5)

    Returns: (host, port, username, password, rdns, proxy_type)
    proxy_type: 'socks5' or 'http'
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

    # Defaults to SOCKS5 with remote DNS
    return host, port, username, password, True, "socks5"


def parse_socks5_proxy(raw: str) -> tuple[str, int, str | None, str | None, bool]:
    """Kept for backward compatibility — delegates to parse_proxy."""
    host, port, username, password, rdns, _ = parse_proxy(raw)
    return host, port, username, password, rdns


# ── MTProto proxies (in addition to SOCKS5/HTTP) ─────────────────────────────
# Telegram's MTProto proxy works differently: instead of a login/password it
# uses a hex secret, and Telethon requires a special connection class
# (ConnectionTcpMTProxy*). Internally we represent such a proxy as a
# marker tuple ('mtproxy', host, port, secret), so it can flow through the
# same selection chain as socks/http.

_MTPROXY_SCHEMES = {"mtproto", "mtproxy", "mtp"}


def is_mtproxy(raw: str | None) -> bool:
    """Whether this looks like an MTProto proxy (by scheme/link), not socks/http."""
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
    """Parses an MTProto proxy into (host, port, secret_hex).

    Formats:
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

        # tg://proxy?... and t.me/proxy?... — parameters are in the query string.
        secret = _q("secret")
        host = _q("server")
        port_q = _q("port")
        if port_q:
            try:
                port = int(port_q)
            except ValueError as exc:
                raise ValueError("Proxy port must be integer") from exc

        # mtproto://host:port[:secret] — host:port:secret ends up in netloc, so
        # parsed.port/hostname are unreliable (they raise on three segments);
        # parse it manually.
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
        # Short form host:port:secret
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
    """A tuple in the format Telethon accepts (via python_socks).

    proxy_type — 'socks5' | 'socks4' | 'http'. Telethon 1.36+ with python_socks
    supports all three.
    """
    if username is not None or password is not None:
        return (proxy_type, host, int(port), bool(rdns), username, password)
    return (proxy_type, host, int(port), bool(rdns))


def build_telethon_proxy(raw: str | None) -> tuple | None:
    """
    Builds a proxy tuple for Telethon with automatic type detection (SOCKS5/HTTP).

    Telethon (via python_socks) supports SOCKS5, SOCKS4 and HTTP. The type is
    taken from the scheme (socks5:// / http://); for the short form
    host:port:user:pass it defaults to SOCKS5 — the actual probing/selection
    is done by resolve_working_proxy.
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
    """A basic TCP reachability probe (for MTProto proxies: python_socks can't
    check them as a socks proxy). Doesn't guarantee the secret is correct —
    only that host:port accepts a connection."""
    import socket

    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError as exc:
        logger.debug("MTProxy TCP probe failed (%s:%s): %s", host, port, exc)
        return False


def telethon_client_kwargs(proxy: tuple | None) -> dict[str, Any]:
    """Build ``TelegramClient`` kwargs from our internal proxy tuple.

    An MTProto proxy (marked 'mtproxy') needs a special connection class and
    the ``(host, port, secret)`` format; socks/http just need a plain
    ``proxy=``. None → empty dict.
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
    """The argument for ``client.set_proxy`` (on-the-fly escalation). An MTProto
    tuple is reduced to ``(host, port, secret)``; everything else is passed
    through unchanged.

    Note: ``set_proxy`` doesn't change the connection class, so switching
    between MTProto and socks/http on the fly is best-effort (the mode is
    fully set only when the client is constructed)."""
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
    """Checks that a connection to Telegram can actually be established through the proxy.

    A blocking function (python_socks.sync) — call it from a thread/executor.
    """
    try:
        from python_socks import ProxyType
        from python_socks.sync import Proxy
    except Exception as exc:  # python_socks unavailable
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
    """Selects a working proxy for Telethon by probing reachability.

    - If the scheme is given explicitly (socks5://, http://) — only that type
      is probed.
    - For the short form host:port[:user:pass] the type is auto-detected:
      SOCKS5 is tried first, then HTTP.
    - If neither type responds — returns (None, ...), and the caller can fall
      back to connecting directly.

    Returns: (telethon_proxy | None, human_readable_label).
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
    """Pick the first working proxy from a chain of candidates.

    Candidates are probed in order (e.g. [primary, backup]). Returns
    ``(telethon_proxy | None, label, tier)``, where ``tier`` is the index of
    the chosen proxy in the cleaned list, or its length if none are reachable
    (→ direct).
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
