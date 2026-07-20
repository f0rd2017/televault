"""Additional tests for config helpers and edge cases."""

from __future__ import annotations

from pathlib import Path

from televault.config.config import (
    _deep_merge,
    _normalize_string_list,
    config_exists,
    load_public_config,
    save_public_config,
)


# ── _deep_merge ─────────────────────────────────────────────────────


def test_deep_merge_simple() -> None:
    a = {"x": 1, "y": 2}
    b = {"y": 3, "z": 4}
    result = _deep_merge(a, b)
    assert result["x"] == 1
    assert result["y"] == 3
    assert result["z"] == 4


def test_deep_merge_nested() -> None:
    a = {"crypto": {"enabled": False, "key_env": "X"}}
    b = {"crypto": {"enabled": True}}
    result = _deep_merge(a, b)
    assert result["crypto"]["enabled"] is True
    assert result["crypto"]["key_env"] == "X"


def test_deep_merge_list_replaces() -> None:
    a = {"chats": [1, 2, 3]}
    b = {"chats": [4, 5]}
    result = _deep_merge(a, b)
    assert result["chats"] == [4, 5]


def test_deep_merge_empty() -> None:
    a = {"x": 1}
    assert _deep_merge(a, {}) == {"x": 1}
    assert _deep_merge({}, a) == {"x": 1}


# ── _normalize_string_list ──────────────────────────────────────────


def test_normalize_comma_separated() -> None:
    assert _normalize_string_list("a,b,c") == ["a", "b", "c"]


def test_normalize_newline_separated() -> None:
    assert _normalize_string_list("a\nb\nc") == ["a", "b", "c"]


def test_normalize_strips_whitespace() -> None:
    assert _normalize_string_list("  a ,  b  ") == ["a", "b"]


def test_normalize_empty() -> None:
    assert _normalize_string_list("") == []


# ── config_exists ───────────────────────────────────────────────────


def test_config_exists_true(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text("{}")
    assert config_exists(p) is True


def test_config_exists_false(tmp_path: Path) -> None:
    assert config_exists(tmp_path / "nonexistent.json") is False


# ── load_public_config / save_public_config ─────────────────────────


def test_public_config_roundtrip(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    original = {
        "chunk_size_mb": 128,
        "concurrency": 4,
        "crypto": {"enabled": True, "key_env": "MY_KEY"},
        "retry": {"max_attempts": 3, "base_delay": 2.0},
    }
    save_public_config(original, cfg_path)
    loaded = load_public_config(cfg_path)
    assert loaded["chunk_size_mb"] == 128
    assert loaded["concurrency"] == 4
    assert loaded["crypto"]["enabled"] is True
    assert loaded["retry"]["max_attempts"] == 3
