import pytest

from app.core import proxy as proxy_mod
from app.core.utils import (
    build_telethon_proxy,
    build_safe_output_path,
    decrypt_bytes,
    encrypt_bytes,
    file_key_from_sha256,
    normalize_folder_path,
    parse_socks5_proxy,
    proxy_endpoint,
    resolve_working_proxy,
)


def test_normalize_folder_path() -> None:
    assert normalize_folder_path(" Anime\\Cache ") == "Anime/Cache"
    assert normalize_folder_path("//A///B//") == "A/B"
    assert normalize_folder_path("A/\u200bB") == "A/B"


def test_safe_output_path_blocks_traversal(tmp_path) -> None:
    path = build_safe_output_path(tmp_path, "A/B", "file.bin")
    assert path.parent.exists() is False
    # Path uses OS-specific separators: '/' on Linux, '\\' on Windows
    assert path.name == "file.bin"
    assert path.parent.name == "B"

    with pytest.raises(ValueError):
        build_safe_output_path(tmp_path, "../../evil", "x")


def test_encrypt_decrypt() -> None:
    raw_key = b"x" * 32
    payload = b"hello world"
    encrypted = encrypt_bytes(payload, raw_key)
    assert encrypted != payload
    assert decrypt_bytes(encrypted, raw_key) == payload


def test_file_key_prefix() -> None:
    assert file_key_from_sha256("abcdef1234567890", 12) == "abcdef123456"


def test_normalize_folder_path_limits() -> None:
    with pytest.raises(ValueError):
        normalize_folder_path("A/" + ("b" * 80))


def test_parse_socks5_proxy_short_and_url_forms() -> None:
    host, port, user, password, rdns = parse_socks5_proxy("127.0.0.1:1080")
    assert host == "127.0.0.1"
    assert port == 1080
    assert user is None
    assert password is None
    assert rdns is True

    host, port, user, password, rdns = parse_socks5_proxy(
        "socks5://u:p@proxy.example:9050"
    )
    assert host == "proxy.example"
    assert port == 9050
    assert user == "u"
    assert password == "p"
    assert rdns is False


def test_build_telethon_proxy_and_endpoint() -> None:
    parsed = build_telethon_proxy("10.0.0.2:1234:user:pass")
    assert parsed == ("socks5", "10.0.0.2", 1234, True, "user", "pass")
    assert proxy_endpoint("10.0.0.2:1234:user:pass") == "socks5://10.0.0.2:1234"
    # HTTP proxy теперь поддерживается (Telethon 1.36+ через python_socks)
    parsed_http = build_telethon_proxy("http://proxy.example:8080")
    assert parsed_http == ("http", "proxy.example", 8080, False)
    parsed_http_auth = build_telethon_proxy("http://u:p@proxy.example:8080")
    assert parsed_http_auth == ("http", "proxy.example", 8080, False, "u", "p")


def test_resolve_working_proxy_empty_is_direct() -> None:
    assert resolve_working_proxy("") == (None, "direct")
    assert resolve_working_proxy(None) == (None, "direct")


def test_resolve_working_proxy_falls_back_to_direct_when_unreachable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(proxy_mod, "probe_proxy", lambda *a, **k: False)
    proxy, label = resolve_working_proxy("10.0.0.2:1234:user:pass")
    assert proxy is None
    assert "unreachable" in label


def test_resolve_working_proxy_auto_detects_http_when_socks_dead(monkeypatch) -> None:
    # SOCKS5 не отвечает, HTTP отвечает — должен выбраться HTTP.
    def fake_probe(host, port, proxy_type, *a, **k):
        return proxy_type == "http"

    monkeypatch.setattr(proxy_mod, "probe_proxy", fake_probe)
    proxy, label = resolve_working_proxy("10.0.0.2:1234:user:pass")
    assert proxy == ("http", "10.0.0.2", 1234, True, "user", "pass")
    assert label == "http://10.0.0.2:1234"


def test_resolve_working_proxy_explicit_scheme_only_probes_that_type(
    monkeypatch,
) -> None:
    probed_types = []

    def fake_probe(host, port, proxy_type, *a, **k):
        probed_types.append(proxy_type)
        return True

    monkeypatch.setattr(proxy_mod, "probe_proxy", fake_probe)
    proxy, label = resolve_working_proxy("socks5://u:p@proxy.example:9050")
    assert proxy == ("socks5", "proxy.example", 9050, False, "u", "p")
    assert probed_types == ["socks5"]  # http не пробуется при явной схеме


def test_parse_socks5_proxy_rejects_invalid() -> None:
    # http:// теперь поддерживается — НЕ должен вызывать ошибку
    with pytest.raises(ValueError):
        parse_socks5_proxy("ftp://127.0.0.1:8080")
    with pytest.raises(ValueError):
        parse_socks5_proxy("127.0.0.1")
    with pytest.raises(ValueError):
        parse_socks5_proxy("127.0.0.1:abc")
