from __future__ import annotations

import json

import pytest

from app.config.config import ConfigError, load_app_config


def _write_config(tmp_path, payload: dict) -> str:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _base_public_config() -> dict:
    return {
        "tg_session_path": "./data/session.session",
        "tg_chat": "@ok_chat",
        "cache_dir": "./cache",
        "chunk_size_mb": 32,
        "concurrency": 2,
        "caption_prefix": "FC1|",
        "scan_search": "FC1|",
        "use_sha_as_key": True,
        "balanced_part_sizing_enabled": True,
        "balanced_part_min_file_mb": 512,
        "balanced_part_target_regular_mb": 1024,
        "balanced_part_target_premium_mb": 2560,
        "small_file_batching_enabled": True,
        "small_file_threshold_kb": 512,
        "small_file_batch_target_mb": 16,
        "small_upload_parallel_jobs": 1,
        "small_batch_mode": "global",
        "small_batch_max_files": 256,
        "small_batch_manifest_mode": "inline_local",
        "send_media_rate_limit": 6.0,
        "get_file_rate_limit": 16.0,
        "lane_upload_small_max": 2,
        "lane_upload_large_max": 2,
        "lane_download_max": 2,
        "perf_telemetry_window_sec": 1.0,
        "retry": {"max_attempts": 6, "base_delay": 1.0},
        "crypto": {"enabled": False, "key_env": "TG_CRYPTO_KEY_B64"},
    }


def test_load_app_config_valid(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "a" * 32)

    payload = _base_public_config()
    payload["max_active_jobs"] = 5
    path = _write_config(tmp_path, payload)
    cfg = load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")
    assert cfg.chunk_size_mb == 32
    assert cfg.concurrency == 2
    assert cfg.max_active_jobs == 5
    assert cfg.upload_compression_mode == "auto"
    assert cfg.upload_limit_safety_mb == 100
    assert cfg.balanced_part_sizing_enabled is True
    assert cfg.balanced_part_min_file_mb == 512
    assert cfg.balanced_part_target_regular_mb == 1024
    assert cfg.balanced_part_target_premium_mb == 2560
    assert cfg.small_file_batching_enabled is True
    assert cfg.small_file_threshold_kb == 512
    assert cfg.small_file_batch_target_mb == 16
    assert cfg.small_upload_parallel_jobs == 1
    assert cfg.small_batch_mode == "global"
    assert cfg.small_batch_max_files == 256
    assert cfg.small_batch_manifest_mode == "inline_local"
    assert cfg.send_media_rate_limit == 6.0
    assert cfg.get_file_rate_limit == 16.0
    assert cfg.lane_upload_small_max == 2
    assert cfg.lane_upload_large_max == 2
    assert cfg.lane_download_max == 2
    assert cfg.perf_telemetry_window_sec == 1.0
    assert cfg.tg_proxy is None
    # download_dir не задан → download_root падает обратно на cache_dir.
    assert cfg.download_dir == ""
    assert cfg.download_root == cfg.cache_dir


def test_mtproxy_tg_proxy_accepted(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "a" * 32)

    payload = _base_public_config()
    secret = "dd" + "00" * 16
    payload["tg_proxy"] = f"mtproto://1.2.3.4:443:{secret}"
    path = _write_config(tmp_path, payload)
    cfg = load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")
    assert cfg.tg_proxy == f"mtproto://1.2.3.4:443:{secret}"


def test_invalid_mtproxy_tg_proxy_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "a" * 32)

    payload = _base_public_config()
    payload["tg_proxy"] = "mtproto://1.2.3.4:443"  # нет секрета
    path = _write_config(tmp_path, payload)
    with pytest.raises(ConfigError, match="tg_proxy"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")


def test_throttle_mbps_loaded_and_validated(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "a" * 32)

    payload = _base_public_config()
    payload["upload_throttle_mbps"] = 5.0
    payload["download_throttle_mbps"] = 12.5
    path = _write_config(tmp_path, payload)
    cfg = load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")
    assert cfg.upload_throttle_mbps == 5.0
    assert cfg.download_throttle_mbps == 12.5

    payload["download_throttle_mbps"] = -1.0  # вне диапазона
    path = _write_config(tmp_path, payload)
    with pytest.raises(ConfigError, match="download_throttle_mbps"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")


def test_stream_cache_max_mb_loaded_and_validated(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "a" * 32)

    # Не задан → дефолт 2048 МБ.
    payload = _base_public_config()
    path = _write_config(tmp_path, payload)
    cfg = load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")
    assert cfg.stream_cache_max_mb == 2048

    payload["stream_cache_max_mb"] = 512
    path = _write_config(tmp_path, payload)
    cfg = load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")
    assert cfg.stream_cache_max_mb == 512

    payload["stream_cache_max_mb"] = -1  # вне диапазона
    path = _write_config(tmp_path, payload)
    with pytest.raises(ConfigError, match="stream_cache_max_mb"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")


def test_api_block_loaded(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "a" * 32)

    payload = _base_public_config()
    payload["api"] = {"enabled": True, "host": "0.0.0.0", "port": 20451, "token": "t"}
    path = _write_config(tmp_path, payload)
    cfg = load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")
    assert cfg.api.enabled and cfg.api.port == 20451 and cfg.api.token == "t"


def test_download_dir_overrides_download_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "a" * 32)

    payload = _base_public_config()
    payload["download_dir"] = "/data/downloads"
    path = _write_config(tmp_path, payload)
    cfg = load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")
    assert cfg.download_dir == "/data/downloads"
    assert cfg.download_root == "/data/downloads"


def test_blank_download_dir_falls_back_to_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "a" * 32)

    payload = _base_public_config()
    payload["cache_dir"] = "./mycache"
    payload["download_dir"] = "   "  # только пробелы → считаем пустым
    path = _write_config(tmp_path, payload)
    cfg = load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")
    # Относительные пути конфига резолвятся от app_base_dir (переносимость
    # frozen-сборки), поэтому сравниваем резолвленные значения.
    from app.core.paths import resolve_app_path

    assert cfg.download_root == cfg.cache_dir
    assert cfg.cache_dir == str(resolve_app_path("./mycache"))


def test_load_app_config_invalid_limits(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "b" * 32)

    cfg = _base_public_config()
    cfg["chunk_size_mb"] = 4096
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="chunk_size_mb"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["concurrency"] = 64
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="concurrency"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["retry"] = {"max_attempts": 0, "base_delay": 1.0}
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="retry.max_attempts"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["max_active_jobs"] = 32
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="max_active_jobs"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["download_integrity_mode"] = "invalid"
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="download_integrity_mode"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["upload_compression_mode"] = "invalid"
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="upload_compression_mode"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["upload_limit_safety_mb"] = -1
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="upload_limit_safety_mb"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["balanced_part_min_file_mb"] = 16
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="balanced_part_min_file_mb"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["balanced_part_target_regular_mb"] = 5000
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="balanced_part_target_regular_mb"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["balanced_part_target_regular_mb"] = 2048
    cfg["balanced_part_target_premium_mb"] = 1024
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="balanced_part_target_premium_mb"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["small_file_threshold_kb"] = 0
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="small_file_threshold_kb"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["small_file_batch_target_mb"] = 0
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="small_file_batch_target_mb"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["small_upload_parallel_jobs"] = 2
    cfg["max_active_jobs"] = 1
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="small_upload_parallel_jobs"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["small_batch_mode"] = "bad"
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="small_batch_mode"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["small_batch_max_files"] = 0
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="small_batch_max_files"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["send_media_rate_limit"] = 0
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="send_media_rate_limit"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["get_file_rate_limit"] = 0
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="get_file_rate_limit"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")

    cfg = _base_public_config()
    cfg["lane_download_max"] = 32
    cfg["max_active_jobs"] = 3
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ConfigError, match="lane_download_max"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")


def test_load_app_config_proxy_fields(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "c" * 32)

    payload = _base_public_config()
    payload["tg_proxy"] = "95.214.92.144:64909:q4VCiQF9:YUhT22HA"
    path = _write_config(tmp_path, payload)
    cfg = load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")
    assert cfg.tg_proxy == "95.214.92.144:64909:q4VCiQF9:YUhT22HA"

    payload = _base_public_config()
    payload["tg_proxy"] = "bad_proxy_format"
    path = _write_config(tmp_path, payload)
    with pytest.raises(ConfigError, match="tg_proxy"):
        load_app_config(config_path=path, dotenv_path=tmp_path / "missing.env")
