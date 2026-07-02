from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any

from dotenv import load_dotenv

from app.config.defaults import DEFAULT_CONFIG
from app.core.paths import app_base_dir, resolve_app_path
from app.core.types import ApiConfig, AppConfig, CryptoConfig, RetryConfig
from app.core.utils import (
    ensure_parent_dir,
    is_mtproxy,
    parse_mtproxy,
    parse_socks5_proxy,
)


class ConfigError(RuntimeError):
    pass


def default_config_path(base_dir: str | Path | None = None) -> Path:
    # Не cwd: frozen-приложение запускают из произвольной директории.
    root = Path(base_dir) if base_dir else app_base_dir()
    return root / "config.json"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _build_config(merged: dict[str, Any]) -> AppConfig:
    # Приоритет: переменные окружения (.env), фолбэк — config.json
    # (заполняется в GUI при первом запуске — работает без ручного .env).
    api_id_raw = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    if not api_id_raw:
        cfg_id = str(merged.get("tg_api_id", "") or "").strip()
        api_id_raw = "" if cfg_id == "0" else cfg_id
    if not api_hash:
        api_hash = str(merged.get("tg_api_hash", "") or "").strip()
    if not api_id_raw or not api_hash:
        raise ConfigError(
            "TG_API_ID and TG_API_HASH must be set in .env "
            "(или заполните «API ID»/«API Hash» в настройках — my.telegram.org/apps)"
        )

    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise ConfigError("TG_API_ID must be integer") from exc
    if api_id <= 0:
        raise ConfigError("TG_API_ID must be > 0")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise ConfigError(
            "TG_API_HASH must be a 32-character hex string from my.telegram.org/apps"
        )

    # Каналы теперь берутся из БД (accounts table), а не из config.json
    # Валидация происходит при загрузке аккаунтов в worker.py

    main_channel_index = int(merged.get("main_channel_index", 0))
    if main_channel_index < 0:
        raise ConfigError("main_channel_index must be >= 0")

    channel_sharding_mode_raw = (
        str(merged.get("channel_sharding_mode", "")).strip().lower()
    )
    if not channel_sharding_mode_raw:
        channel_sharding_mode = (
            "part_striping"  # Определяется динамически по кол-ву аккаунтов в БД
        )
    else:
        channel_sharding_mode = channel_sharding_mode_raw
    if channel_sharding_mode not in {"single", "part_striping"}:
        raise ConfigError("channel_sharding_mode must be one of: single, part_striping")

    retry_obj = merged.get("retry", {})
    crypto_obj = merged.get("crypto", {})

    retry = RetryConfig(
        max_attempts=int(retry_obj.get("max_attempts", 6)),
        base_delay=float(retry_obj.get("base_delay", 1.0)),
    )
    crypto = CryptoConfig(
        enabled=bool(crypto_obj.get("enabled", False)),
        key_env=str(
            crypto_obj.get("key_env", "TG_CRYPTO_KEY_B64") or "TG_CRYPTO_KEY_B64"
        ),
    )

    api_obj = merged.get("api", {}) or {}
    api_port = int(api_obj.get("port", 20451))
    if api_port < 1 or api_port > 65535:
        raise ConfigError("api.port must be in range 1..65535")
    api = ApiConfig(
        enabled=bool(api_obj.get("enabled", False)),
        host=str(api_obj.get("host", "127.0.0.1")).strip() or "127.0.0.1",
        port=api_port,
        token=str(api_obj.get("token", "") or "").strip(),
    )
    fetch_thumbnails = bool(merged.get("fetch_thumbnails", True))
    ui_icon_size = int(merged.get("ui_icon_size", 56))
    if ui_icon_size < 32:
        raise ConfigError("ui_icon_size must be >= 32")
    if ui_icon_size > 256:
        raise ConfigError("ui_icon_size must be <= 256")

    chunk_size_mb = int(merged.get("chunk_size_mb", 32))
    concurrency = int(merged.get("concurrency", 6))
    max_active_jobs = int(merged.get("max_active_jobs", 8))
    download_integrity_mode = (
        str(merged.get("download_integrity_mode", "strict")).strip().lower()
    )
    keep_partial_on_failure = bool(merged.get("keep_partial_on_failure", True))
    upload_compression_mode = (
        str(merged.get("upload_compression_mode", "auto")).strip().lower()
    )
    upload_limit_safety_mb = int(merged.get("upload_limit_safety_mb", 100))
    balanced_part_sizing_enabled = bool(
        merged.get("balanced_part_sizing_enabled", True)
    )
    balanced_part_min_file_mb = int(merged.get("balanced_part_min_file_mb", 512))
    balanced_part_target_regular_mb = int(
        merged.get("balanced_part_target_regular_mb", 1024)
    )
    balanced_part_target_premium_mb = int(
        merged.get("balanced_part_target_premium_mb", 2560)
    )
    small_file_batching_enabled = bool(merged.get("small_file_batching_enabled", True))
    small_file_threshold_kb = int(merged.get("small_file_threshold_kb", 8192))
    small_file_batch_target_mb = int(merged.get("small_file_batch_target_mb", 48))
    small_upload_parallel_jobs = int(merged.get("small_upload_parallel_jobs", 4))
    small_batch_mode = str(merged.get("small_batch_mode", "global")).strip().lower()
    # High cap so the byte target (small_file_batch_target_mb) is what bounds a
    # batch, not the file count — otherwise many tiny files cap the archive far
    # below target and upload slow (single-account, small payload).
    small_batch_max_files = int(merged.get("small_batch_max_files", 1024))
    small_batch_manifest_mode = (
        str(merged.get("small_batch_manifest_mode", "inline_local")).strip().lower()
    )
    send_media_rate_limit = float(merged.get("send_media_rate_limit", 8.0))
    get_file_rate_limit = float(merged.get("get_file_rate_limit", 24.0))
    upload_throttle_mbps = float(merged.get("upload_throttle_mbps", 0.0))
    download_throttle_mbps = float(merged.get("download_throttle_mbps", 0.0))
    lane_upload_small_max = int(merged.get("lane_upload_small_max", 4))
    lane_upload_large_max = int(merged.get("lane_upload_large_max", 4))
    lane_download_max = int(merged.get("lane_download_max", 6))
    perf_telemetry_window_sec = float(merged.get("perf_telemetry_window_sec", 1.0))
    if chunk_size_mb <= 0:
        raise ConfigError("chunk_size_mb must be > 0")
    if chunk_size_mb > 2048:
        raise ConfigError("chunk_size_mb must be <= 2048")
    if concurrency <= 0:
        raise ConfigError("concurrency must be > 0")
    if concurrency > 16:
        raise ConfigError("concurrency must be <= 16")
    if max_active_jobs <= 0:
        raise ConfigError("max_active_jobs must be > 0")
    if max_active_jobs > 16:
        raise ConfigError("max_active_jobs must be <= 16")
    if download_integrity_mode not in {"strict", "fast"}:
        raise ConfigError("download_integrity_mode must be either 'strict' or 'fast'")
    if upload_compression_mode not in {"off", "auto", "force"}:
        raise ConfigError("upload_compression_mode must be one of: off, auto, force")
    if upload_limit_safety_mb < 0:
        raise ConfigError("upload_limit_safety_mb must be >= 0")
    if upload_limit_safety_mb > 1024:
        raise ConfigError("upload_limit_safety_mb must be <= 1024")
    if balanced_part_min_file_mb < 64:
        raise ConfigError("balanced_part_min_file_mb must be >= 64")
    if balanced_part_min_file_mb > 10_240:
        raise ConfigError("balanced_part_min_file_mb must be <= 10240")
    if balanced_part_target_regular_mb < 128:
        raise ConfigError("balanced_part_target_regular_mb must be >= 128")
    if balanced_part_target_regular_mb > 4096:
        raise ConfigError("balanced_part_target_regular_mb must be <= 4096")
    if balanced_part_target_premium_mb < 256:
        raise ConfigError("balanced_part_target_premium_mb must be >= 256")
    if balanced_part_target_premium_mb > 4096:
        raise ConfigError("balanced_part_target_premium_mb must be <= 4096")
    if balanced_part_target_premium_mb < balanced_part_target_regular_mb:
        raise ConfigError(
            "balanced_part_target_premium_mb must be >= balanced_part_target_regular_mb"
        )
    if small_file_threshold_kb <= 0:
        raise ConfigError("small_file_threshold_kb must be > 0")
    if small_file_threshold_kb > 64 * 1024:
        raise ConfigError("small_file_threshold_kb must be <= 65536")
    if small_file_batch_target_mb <= 0:
        raise ConfigError("small_file_batch_target_mb must be > 0")
    if small_file_batch_target_mb > 512:
        raise ConfigError("small_file_batch_target_mb must be <= 512")
    if small_upload_parallel_jobs <= 0:
        raise ConfigError("small_upload_parallel_jobs must be > 0")
    if small_upload_parallel_jobs > max_active_jobs:
        raise ConfigError("small_upload_parallel_jobs must be <= max_active_jobs")
    if small_batch_mode not in {"per_folder", "global"}:
        raise ConfigError("small_batch_mode must be one of: per_folder, global")
    if small_batch_max_files <= 0:
        raise ConfigError("small_batch_max_files must be > 0")
    if small_batch_max_files > 4096:
        raise ConfigError("small_batch_max_files must be <= 4096")
    if small_batch_manifest_mode not in {"inline_local"}:
        raise ConfigError("small_batch_manifest_mode must be 'inline_local'")
    if send_media_rate_limit <= 0:
        raise ConfigError("send_media_rate_limit must be > 0")
    if send_media_rate_limit > 100:
        raise ConfigError("send_media_rate_limit must be <= 100")
    if get_file_rate_limit <= 0:
        raise ConfigError("get_file_rate_limit must be > 0")
    if get_file_rate_limit > 200:
        raise ConfigError("get_file_rate_limit must be <= 200")
    if upload_throttle_mbps < 0 or upload_throttle_mbps > 10_000:
        raise ConfigError("upload_throttle_mbps must be in range 0..10000 (0 = off)")
    if download_throttle_mbps < 0 or download_throttle_mbps > 10_000:
        raise ConfigError("download_throttle_mbps must be in range 0..10000 (0 = off)")
    if lane_upload_small_max <= 0:
        raise ConfigError("lane_upload_small_max must be > 0")
    if lane_upload_small_max > 16:
        raise ConfigError("lane_upload_small_max must be <= 16")
    if lane_upload_large_max <= 0:
        raise ConfigError("lane_upload_large_max must be > 0")
    if lane_upload_large_max > 16:
        raise ConfigError("lane_upload_large_max must be <= 16")
    if lane_download_max <= 0:
        raise ConfigError("lane_download_max must be > 0")
    if lane_download_max > 16:
        raise ConfigError("lane_download_max must be <= 16")
    if perf_telemetry_window_sec < 0.2:
        raise ConfigError("perf_telemetry_window_sec must be >= 0.2")
    if perf_telemetry_window_sec > 60:
        raise ConfigError("perf_telemetry_window_sec must be <= 60")
    if retry.max_attempts <= 0 or retry.max_attempts > 20:
        raise ConfigError("retry.max_attempts must be in range 1..20")
    if retry.base_delay < 0.1 or retry.base_delay > 60.0:
        raise ConfigError("retry.base_delay must be in range 0.1..60.0")

    caption_prefix = str(merged.get("caption_prefix", "FC1|")).strip() or "FC1|"
    if len(caption_prefix) > 32:
        raise ConfigError("caption_prefix must be <= 32 chars")
    scan_search = (
        str(merged.get("scan_search", caption_prefix)).strip() or caption_prefix
    )
    if len(scan_search) > 64:
        raise ConfigError("scan_search must be <= 64 chars")

    cache_max_size_mb = int(merged.get("cache_max_size_mb", 0))
    if cache_max_size_mb < 0:
        raise ConfigError("cache_max_size_mb must be >= 0")

    stream_cache_max_mb = int(merged.get("stream_cache_max_mb", 2048))
    if stream_cache_max_mb < 0:
        raise ConfigError("stream_cache_max_mb must be >= 0 (0 = no limit)")

    tg_proxy_value = str(merged.get("tg_proxy", "") or "").strip()
    tg_proxy = tg_proxy_value or None
    if tg_proxy is not None:
        try:
            if is_mtproxy(tg_proxy):
                parse_mtproxy(tg_proxy)
            else:
                parse_socks5_proxy(tg_proxy)
        except ValueError as exc:
            raise ConfigError(f"Invalid tg_proxy: {exc}") from exc

    # В config.json пути могут быть относительными (./var/…) — файл остаётся
    # переносимым. В AppConfig кладём абсолютные: относительные считаются от
    # app_base_dir() (рядом с exe / корень проекта), а не от произвольной cwd.
    download_dir_raw = str(merged.get("download_dir", "")).strip()
    return AppConfig(
        tg_api_id=api_id,
        tg_api_hash=api_hash,
        tg_session_path=str(
            resolve_app_path(
                merged.get("tg_session_path", "./var/data/session.session")
            )
        ),
        main_channel_index=main_channel_index,
        channel_sharding_mode=channel_sharding_mode,
        cache_dir=str(resolve_app_path(merged.get("cache_dir", "./var/cache"))),
        download_dir=(
            str(resolve_app_path(download_dir_raw)) if download_dir_raw else ""
        ),
        show_thumbnails=bool(merged.get("show_thumbnails", True)),
        fetch_thumbnails=fetch_thumbnails,
        ui_icon_size=ui_icon_size,
        chunk_size_mb=chunk_size_mb,
        concurrency=concurrency,
        caption_prefix=caption_prefix,
        scan_search=scan_search,
        use_sha_as_key=bool(merged.get("use_sha_as_key", True)),
        cache_max_size_mb=cache_max_size_mb,
        stream_cache_max_mb=stream_cache_max_mb,
        max_active_jobs=max_active_jobs,
        download_integrity_mode=download_integrity_mode,
        keep_partial_on_failure=keep_partial_on_failure,
        upload_compression_mode=upload_compression_mode,
        upload_limit_safety_mb=upload_limit_safety_mb,
        balanced_part_sizing_enabled=balanced_part_sizing_enabled,
        balanced_part_min_file_mb=balanced_part_min_file_mb,
        balanced_part_target_regular_mb=balanced_part_target_regular_mb,
        balanced_part_target_premium_mb=balanced_part_target_premium_mb,
        small_file_batching_enabled=small_file_batching_enabled,
        small_file_threshold_kb=small_file_threshold_kb,
        small_file_batch_target_mb=small_file_batch_target_mb,
        small_upload_parallel_jobs=small_upload_parallel_jobs,
        small_batch_mode=small_batch_mode,
        small_batch_max_files=small_batch_max_files,
        small_batch_manifest_mode=small_batch_manifest_mode,
        send_media_rate_limit=send_media_rate_limit,
        get_file_rate_limit=get_file_rate_limit,
        upload_throttle_mbps=upload_throttle_mbps,
        download_throttle_mbps=download_throttle_mbps,
        lane_upload_small_max=lane_upload_small_max,
        lane_upload_large_max=lane_upload_large_max,
        lane_download_max=lane_download_max,
        perf_telemetry_window_sec=perf_telemetry_window_sec,
        retry=retry,
        crypto=crypto,
        api=api,
        tg_proxy=tg_proxy,
    )


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        # Support comma/newline separated forms from config editors.
        raw_items = re.split(r"[,\r\n]+", value)
        return [item.strip() for item in raw_items if item.strip()]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            stripped = item.strip()
            if stripped:
                result.append(stripped)
        return result
    return []


def _normalize_int_list(value: Any) -> list[int]:
    if isinstance(value, str):
        items = [part.strip() for part in re.split(r"[,\r\n]+", value) if part.strip()]
        result: list[int] = []
        for item in items:
            try:
                result.append(int(item))
            except ValueError:
                continue
        return result
    if isinstance(value, list):
        result: list[int] = []
        for item in value:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
        return result
    return []


def _build_public_config(override: dict[str, Any]) -> dict[str, Any]:
    return _deep_merge(DEFAULT_CONFIG, override)


def load_app_config(
    config_path: str | Path | None = None,
    dotenv_path: str | Path | None = None,
) -> AppConfig:
    # Явный путь к .env: дефолтный поиск load_dotenv идёт от cwd и во frozen-
    # сборке файл рядом с exe не находит.
    load_dotenv(dotenv_path=dotenv_path or (app_base_dir() / ".env"))

    path = Path(config_path) if config_path else default_config_path()
    raw: dict[str, Any] = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))

    merged = _build_public_config(raw)
    return _build_config(merged)


def load_public_config(config_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else default_config_path()
    raw: dict[str, Any] = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    return _build_public_config(raw)


def save_public_config(
    public_config: dict[str, Any], config_path: str | Path | None = None
) -> Path:
    path = Path(config_path) if config_path else default_config_path()
    ensure_parent_dir(path)
    merged = _build_public_config(public_config)
    path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return path


def config_exists(config_path: str | Path | None = None) -> bool:
    path = Path(config_path) if config_path else default_config_path()
    return path.exists()
