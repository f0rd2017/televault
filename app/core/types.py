from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass(frozen=True, slots=True)
class RetryConfig:
    max_attempts: int = 6
    base_delay: float = 1.0


@dataclass(frozen=True, slots=True)
class TelegramAccount:
    """Telegram user аккаунт для upload."""

    id: int  # Уникальный ID в системе (не Telegram ID)
    label: str  # Отображаемое имя, например "Аккаунт 1"
    session_path: str  # Путь к .session файлу
    tg_api_id: int  # API ID для этого аккаунта
    tg_api_hash: str  # API Hash
    chat_target: str  # Ссылка/username канала куда писать
    is_active: bool = True  # Включён ли аккаунт
    is_primary: bool = False  # Основной аккаунт (без прокси)
    proxy: str = ""  # SOCKS5 прокси для этого аккаунта (пусто = без прокси)
    proxy_backup: str = ""  # Резервный прокси (fallback, если основной недоступен)
    phone_masked: str = ""  # Маскированный номер для отображения
    user_id: int = 0  # Telegram user ID (заполняется после авторизации)
    username: str = ""  # Telegram username
    is_premium: bool = False  # Premium статус


@dataclass(frozen=True, slots=True)
class CryptoConfig:
    enabled: bool = False
    key_env: str | None = "TG_CRYPTO_KEY_B64"


@dataclass(frozen=True, slots=True)
class ApiConfig:
    """Локальный REST API поверх ядра (инкремент 5). Выключен по умолчанию.

    token пустой → авторизация отключена (полагаемся на привязку к 127.0.0.1).
    Непустой token → требуется заголовок ``Authorization: Bearer <token>``
    (или ``?token=``)."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 20451
    token: str = ""


@dataclass(frozen=True)
class AppConfig:
    tg_api_id: int
    tg_api_hash: str
    tg_session_path: str
    cache_dir: str
    # Куда сохранять скачанные файлы. Пусто → используется cache_dir (поведение
    # по умолчанию). Внутренний кэш (.batch_blob_cache и т.п.) всегда в cache_dir.
    download_dir: str = ""
    # Превью картинок в гриде: показывать миниатюры; и тянуть их фоном для ещё
    # не скачанных картинок (использует трафик).
    show_thumbnails: bool = True
    fetch_thumbnails: bool = True
    ui_icon_size: int = 56
    main_channel_index: int = 0
    channel_sharding_mode: str = "single"
    chunk_size_mb: int = 32
    concurrency: int = 6
    caption_prefix: str = "FC1|"
    scan_search: str = "FC1|"
    use_sha_as_key: bool = True
    cache_max_size_mb: int = 0
    # Лимит кэша частей стриминга (.share_cache/.stream), МБ. 0 = без лимита.
    stream_cache_max_mb: int = 2048
    max_active_jobs: int = 8
    download_integrity_mode: str = "strict"
    keep_partial_on_failure: bool = True
    upload_compression_mode: str = "auto"
    upload_limit_safety_mb: int = 100
    balanced_part_sizing_enabled: bool = True
    balanced_part_min_file_mb: int = 512
    # Stripe a single file across accounts only when it's at least this big;
    # smaller files go whole to one rotating account (file-level parallelism).
    multi_client_shard_min_mb: int = 100
    balanced_part_target_regular_mb: int = 1024
    balanced_part_target_premium_mb: int = 2560
    small_file_batching_enabled: bool = True
    small_file_threshold_kb: int = 8192
    small_file_batch_target_mb: int = 48
    small_upload_parallel_jobs: int = 4
    small_batch_mode: str = "global"
    small_batch_max_files: int = 512
    small_batch_manifest_mode: str = "inline_local"
    send_media_rate_limit: float = 8.0
    get_file_rate_limit: float = 24.0
    upload_throttle_mbps: float = 0.0  # лимит полосы загрузки, МБ/с (0 = без лимита)
    download_throttle_mbps: float = (
        0.0  # лимит полосы скачивания, МБ/с (0 = без лимита)
    )
    lane_upload_small_max: int = 4
    lane_upload_large_max: int = 4
    lane_download_max: int = 6
    perf_telemetry_window_sec: float = 1.0
    retry: RetryConfig = field(default_factory=RetryConfig)
    crypto: CryptoConfig = field(default_factory=CryptoConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    tg_proxy: str | None = None
    accounts: list[TelegramAccount] = field(
        default_factory=list
    )  # Мультиаккаунты для upload

    @property
    def chunk_size_bytes(self) -> int:
        return self.chunk_size_mb * 1024 * 1024

    @property
    def download_root(self) -> str:
        """Корневая папка для скачанных файлов: явный download_dir или cache_dir."""
        explicit = str(self.download_dir or "").strip()
        return explicit or self.cache_dir

    def as_public_dict(self) -> dict[str, Any]:
        return {
            # Локальный дамп для диалога настроек; без него сохранение настроек
            # затирало бы креды в config.json (save_public_config не мержит).
            "tg_api_id": int(self.tg_api_id),
            "tg_api_hash": self.tg_api_hash,
            "tg_session_path": self.tg_session_path,
            "main_channel_index": int(self.main_channel_index),
            "channel_sharding_mode": self.channel_sharding_mode,
            "cache_dir": self.cache_dir,
            "download_dir": self.download_dir,
            "show_thumbnails": self.show_thumbnails,
            "fetch_thumbnails": self.fetch_thumbnails,
            "ui_icon_size": self.ui_icon_size,
            "chunk_size_mb": self.chunk_size_mb,
            "concurrency": self.concurrency,
            "caption_prefix": self.caption_prefix,
            "scan_search": self.scan_search,
            "use_sha_as_key": self.use_sha_as_key,
            "cache_max_size_mb": self.cache_max_size_mb,
            "stream_cache_max_mb": self.stream_cache_max_mb,
            "max_active_jobs": self.max_active_jobs,
            "download_integrity_mode": self.download_integrity_mode,
            "keep_partial_on_failure": self.keep_partial_on_failure,
            "upload_compression_mode": self.upload_compression_mode,
            "upload_limit_safety_mb": self.upload_limit_safety_mb,
            "balanced_part_sizing_enabled": self.balanced_part_sizing_enabled,
            "balanced_part_min_file_mb": self.balanced_part_min_file_mb,
            "balanced_part_target_regular_mb": self.balanced_part_target_regular_mb,
            "balanced_part_target_premium_mb": self.balanced_part_target_premium_mb,
            "small_file_batching_enabled": self.small_file_batching_enabled,
            "small_file_threshold_kb": self.small_file_threshold_kb,
            "small_file_batch_target_mb": self.small_file_batch_target_mb,
            "small_upload_parallel_jobs": self.small_upload_parallel_jobs,
            "small_batch_mode": self.small_batch_mode,
            "small_batch_max_files": self.small_batch_max_files,
            "small_batch_manifest_mode": self.small_batch_manifest_mode,
            "send_media_rate_limit": self.send_media_rate_limit,
            "get_file_rate_limit": self.get_file_rate_limit,
            "upload_throttle_mbps": self.upload_throttle_mbps,
            "download_throttle_mbps": self.download_throttle_mbps,
            "lane_upload_small_max": self.lane_upload_small_max,
            "lane_upload_large_max": self.lane_upload_large_max,
            "lane_download_max": self.lane_download_max,
            "perf_telemetry_window_sec": self.perf_telemetry_window_sec,
            "retry": {
                "max_attempts": self.retry.max_attempts,
                "base_delay": self.retry.base_delay,
            },
            "crypto": {
                "enabled": self.crypto.enabled,
                "key_env": self.crypto.key_env,
            },
            "api": {
                "enabled": self.api.enabled,
                "host": self.api.host,
                "port": self.api.port,
                "token": self.api.token,
            },
            "tg_proxy": "",  # Скрываем
        }


@dataclass(frozen=True, slots=True)
class PartMeta:
    folder_path: str
    file_key: str
    part_index: int
    parts_total: int
    orig_name: str
    sha256: str | None = None
    orig_size: int | None = None
    part_size: int | None = None
    enc: bool | None = None


@dataclass(frozen=True, slots=True)
class BatchBlobCaption:
    version: int
    kind: str
    folder_path: str
    blob_key: str
    orig_name: str
    members_count: int
    manifest_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class PartRecord:
    msg_id: int
    chat_id: str
    folder_path: str
    file_key: str
    part_index: int
    parts_total: int
    orig_name: str
    file_size: int | None
    caption_raw: str | None
    date_ts: int
    lost_ts: int | None = None


@dataclass(frozen=True, slots=True)
class ScanStats:
    processed_messages: int
    indexed_parts: int
    max_msg_id: int
    deleted_marked: int = 0
    parse_skipped: int = 0


@dataclass(frozen=True, slots=True)
class TgTransferLimits:
    is_premium: bool = False
    request_size_bytes: int = 524288
    max_fileparts: int = 4000
    max_file_size_bytes: int = 4000 * 524288


@dataclass(frozen=True, slots=True)
class FolderEntry:
    folder_path: str
    created_ts: int
    pinned: int = 0


@dataclass(frozen=True, slots=True)
class ObjectEntry:
    file_key: str
    folder_path: str
    orig_name: str
    parts_total: int
    have_parts: int
    status: str
    total_size: int | None
    last_seen_ts: int
    storage_kind: str = "regular"
    blob_key: str | None = None


@dataclass(frozen=True, slots=True)
class BatchBlobEntry:
    blob_key: str
    folder_path: str
    chat_id: str
    msg_id: int
    blob_name: str
    blob_size: int | None
    blob_sha256: str | None
    manifest_json: str
    is_deleted: int
    created_ts: int
    last_seen_ts: int


@dataclass(frozen=True, slots=True)
class BatchMemberEntry:
    folder_path: str
    file_key: str
    blob_key: str
    orig_name: str
    member_index: int
    member_size: int | None
    member_sha256: str | None
    deleted_ts: int | None
    name_pinned: int
    created_ts: int
    updated_ts: int


class JobType(str, Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    DELETE = "delete"
    DELETE_FOLDER = "delete_folder"
    RENAME = "rename"
    REFRESH = "refresh"
    RECONCILE = "reconcile"
    REINDEX = "reindex"


class JobStatus(str, Enum):
    QUEUED = "queued"
    STARTED = "started"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class JobEvent:
    job_id: int
    job_type: str
    status: JobStatus
    progress: float = 0.0
    message: str = ""
    error: str | None = None
    payload: dict[str, Any] | None = None
    result: Any = None
