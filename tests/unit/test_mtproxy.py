from __future__ import annotations

import pytest

from televault.core import proxy as proxy_mod
from televault.core.utils import (
    build_telethon_proxy,
    is_mtproxy,
    parse_mtproxy,
    proxy_endpoint,
    proxy_for_set_proxy,
    resolve_working_proxy,
    telethon_client_kwargs,
)

SECRET = "dd000102030405060708090a0b0c0d0e0f"


# ── Detector ─────────────────────────────────────────────────────────────────


def test_is_mtproxy_detects_schemes():
    assert is_mtproxy(f"tg://proxy?server=h&port=443&secret={SECRET}")
    assert is_mtproxy(f"https://t.me/proxy?server=h&port=443&secret={SECRET}")
    assert is_mtproxy(f"mtproto://h:443:{SECRET}")
    assert is_mtproxy(f"mtproxy://h:443?secret={SECRET}")
    # socks/http and empty — not mtproxy
    assert not is_mtproxy("socks5://user:pass@h:1080")
    assert not is_mtproxy("h:1080")
    assert not is_mtproxy("")
    assert not is_mtproxy(None)
    # https without a secret — a regular link, not mtproxy
    assert not is_mtproxy("https://t.me/joinchat/abc")


# ── Parser ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            f"tg://proxy?server=1.2.3.4&port=443&secret={SECRET}",
            ("1.2.3.4", 443, SECRET),
        ),
        (
            f"https://t.me/proxy?server=ex.com&port=8443&secret={SECRET}",
            ("ex.com", 8443, SECRET),
        ),
        (f"mtproto://1.2.3.4:443:{SECRET}", ("1.2.3.4", 443, SECRET)),
        (f"mtproxy://host.net:9999?secret={SECRET}", ("host.net", 9999, SECRET)),
        (f"1.2.3.4:443:{SECRET}", ("1.2.3.4", 443, SECRET)),
    ],
)
def test_parse_mtproxy_formats(raw, expected):
    assert parse_mtproxy(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "mtproto://justhost",
        f"mtproto://h:99999:{SECRET}",  # port out of range
        "tg://proxy?server=h&port=443",  # no secret
        "h:443",  # short form without a secret
    ],
)
def test_parse_mtproxy_rejects_bad(raw):
    with pytest.raises(ValueError):
        parse_mtproxy(raw)


# ── Client construction ──────────────────────────────────────────────────────


def test_build_telethon_proxy_mtproxy_marker():
    assert build_telethon_proxy(f"mtproto://1.2.3.4:443:{SECRET}") == (
        "mtproxy",
        "1.2.3.4",
        443,
        SECRET,
    )


def test_build_telethon_proxy_socks_unchanged():
    # socks stays a python_socks tuple (leading type string, no 'mtproxy').
    out = build_telethon_proxy("socks5://user:pass@1.2.3.4:1080")
    assert out[0] == "socks5"


def test_telethon_client_kwargs_mtproxy():
    kwargs = telethon_client_kwargs(("mtproxy", "h", 443, SECRET))
    assert kwargs["proxy"] == ("h", 443, SECRET)
    assert kwargs["connection"].__name__ == "ConnectionTcpMTProxyRandomizedIntermediate"


def test_telethon_client_kwargs_socks_and_none():
    assert telethon_client_kwargs(("socks5", "h", 1080, True)) == {
        "proxy": ("socks5", "h", 1080, True)
    }
    assert telethon_client_kwargs(None) == {}


def test_proxy_for_set_proxy_strips_marker():
    # MTProto 4-tuple → 3-tuple (host, port, secret) for client.set_proxy.
    assert proxy_for_set_proxy(("mtproxy", "h", 443, SECRET)) == ("h", 443, SECRET)
    # socks/None — no change.
    assert proxy_for_set_proxy(("socks5", "h", 1080, True)) == (
        "socks5",
        "h",
        1080,
        True,
    )
    assert proxy_for_set_proxy(None) is None


def test_proxy_endpoint_mtproxy():
    assert proxy_endpoint(f"mtproto://1.2.3.4:443:{SECRET}") == "mtproxy://1.2.3.4:443"


# ── Resolution along the chain (probe mocked) ────────────────────────────────


def test_resolve_working_proxy_mtproxy_reachable(monkeypatch):
    monkeypatch.setattr(proxy_mod, "_probe_tcp", lambda h, p, timeout: True)
    proxy, label = resolve_working_proxy(f"mtproto://1.2.3.4:443:{SECRET}")
    assert proxy == ("mtproxy", "1.2.3.4", 443, SECRET)
    assert label == "mtproxy://1.2.3.4:443"


def test_resolve_working_proxy_mtproxy_unreachable(monkeypatch):
    monkeypatch.setattr(proxy_mod, "_probe_tcp", lambda h, p, timeout: False)
    proxy, label = resolve_working_proxy(f"mtproto://1.2.3.4:443:{SECRET}")
    assert proxy is None
    assert "direct" in label


def test_resolve_working_proxy_mtproxy_invalid_is_direct():
    proxy, label = resolve_working_proxy("mtproto://nohostorsecret")
    assert proxy is None
    assert "direct" in label
