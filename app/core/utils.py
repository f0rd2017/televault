from __future__ import annotations

import base64
import hashlib
import importlib.util
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Iterator

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.proxy import (  # noqa: F401
    build_telethon_proxy,
    is_mtproxy,
    parse_mtproxy,
    parse_proxy,
    parse_socks5_proxy,
    probe_proxy,
    proxy_endpoint,
    proxy_for_set_proxy,
    resolve_working_proxy,
    select_working_proxy_from_chain,
    telethon_client_kwargs,
)

logger = logging.getLogger(__name__)

_INVALID_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_MAX_FOLDER_PATH_LEN = 255
_MAX_FOLDER_SEGMENT_LEN = 64


def now_ts() -> int:
    return int(time.time())


def normalize_folder_path(raw: str) -> str:
    cleaned = _normalize_text(raw).replace("\\", "/")
    parts: list[str] = []
    for part in cleaned.split("/"):
        part = _normalize_text(part)
        if not part:
            continue
        if part in {".", ".."}:
            raise ValueError("Relative path segments are not allowed")
        part = _INVALID_FILE_CHARS.sub("_", part)
        if len(part) > _MAX_FOLDER_SEGMENT_LEN:
            raise ValueError(
                f"Folder segment is too long (max {_MAX_FOLDER_SEGMENT_LEN} chars): {part[:24]}..."
            )
        parts.append(part)
    if not parts:
        raise ValueError("Folder path cannot be empty")
    normalized = "/".join(parts)
    if len(normalized) > _MAX_FOLDER_PATH_LEN:
        raise ValueError("Folder path is too long")
    return normalized


def sanitize_filename(raw: str, max_len: int = 120) -> str:
    basename = _normalize_text(Path(raw).name)
    if not basename:
        basename = "file.bin"
    basename = _INVALID_FILE_CHARS.sub("_", basename)
    if basename in {".", ".."}:
        basename = "file.bin"
    max_len = max(16, min(int(max_len), 255))
    if len(basename) > max_len:
        stem = Path(basename).stem[: max_len - 10]
        suffix = Path(basename).suffix[:10]
        basename = f"{stem}{suffix}"
    return basename


def build_safe_output_path(
    base_dir: str | Path, folder_path: str, file_name: str
) -> Path:
    base = Path(base_dir).expanduser().resolve()
    target = (
        base / normalize_folder_path(folder_path) / sanitize_filename(file_name)
    ).resolve()
    if base != target and base not in target.parents:
        raise ValueError("Unsafe output path")
    if len(str(target)) > 1024:
        raise ValueError("Output path is too long")
    return target


def ensure_parent_dir(path: str | Path) -> None:
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def ensure_dir(path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target


def clear_dir_files(path: str | Path) -> int:
    """Best-effort: удалить все файлы в директории (не рекурсивно). Для очистки
    эфемерных temp-папок (напр. .thumb_fetch). Возвращает число удалённых."""
    target = Path(path).expanduser()
    if not target.is_dir():
        return 0
    removed = 0
    for entry in target.iterdir():
        if entry.is_file():
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def evict_dir_to_limit(path: str | Path, max_files: int) -> int:
    """LRU-эвикция кэш-папки: если файлов больше max_files, удалить самые старые
    по mtime до лимита. Best-effort. Возвращает число удалённых."""
    target = Path(path).expanduser()
    if not target.is_dir() or max_files < 0:
        return 0
    files = [p for p in target.iterdir() if p.is_file()]
    if len(files) <= max_files:
        return 0
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0)
    to_remove = files[: len(files) - max_files]
    removed = 0
    for entry in to_remove:
        try:
            entry.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def ffmpeg_available() -> bool:
    """Есть ли ffmpeg в PATH — для построения видео-постеров (инкремент 4)."""
    return shutil.which("ffmpeg") is not None


def extract_video_poster_png(
    video_path: str | Path,
    out_png: str | Path,
    *,
    box: int = 320,
    seek_sec: float = 1.0,
    timeout: float = 25.0,
) -> bool:
    """Извлечь кадр-постер из локального видео в PNG через ffmpeg.

    Кадр масштабируется так, чтобы вписаться в квадрат ``box`` с сохранением
    пропорций. Сперва пробуем кадр на ``seek_sec`` (репрезентативнее чёрного
    первого кадра); если видео короче — фолбэк на самое начало. Блокирующая
    функция (subprocess) — вызывать из потока/executor, не из UI/loop напрямую.
    Возвращает True при успехе (PNG записан и непустой).
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    src = Path(video_path)
    try:
        if not src.is_file() or src.stat().st_size == 0:
            return False
    except OSError:
        return False
    out = Path(out_png)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    box = max(16, int(box))
    vf = f"scale={box}:{box}:force_original_aspect_ratio=decrease"

    def _attempt(seek: float) -> bool:
        cmd = [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, float(seek)):.3f}",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-vf",
            vf,
            str(out),
        ]
        try:
            proc = subprocess.run(  # noqa: S603
                cmd, capture_output=True, timeout=timeout, check=False
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("ffmpeg poster extraction failed for %s: %s", src, exc)
            return False
        try:
            return proc.returncode == 0 and out.is_file() and out.stat().st_size > 0
        except OSError:
            return False

    if seek_sec > 0 and _attempt(seek_sec):
        return True
    return _attempt(0.0)


def convert_image_to_png(
    src_image: str | Path,
    out_png: str | Path,
    *,
    timeout: float = 30.0,
) -> bool:
    """Сконвертировать изображение в PNG через ffmpeg.

    Нужно для форматов, которые Qt не умеет декодировать сам (heic/avif/
    tiff/raw/psd и т.п.). Берём первый кадр (для многослойных/анимированных)
    и сохраняем в PNG без масштабирования. Блокирующая функция (subprocess);
    для одиночного открытия по запросу пользователя это приемлемо.
    Возвращает True при успехе (PNG записан и непустой).
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    src = Path(src_image)
    try:
        if not src.is_file() or src.stat().st_size == 0:
            return False
    except OSError:
        return False
    out = Path(out_png)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-frames:v",
        "1",
        "-update",
        "1",
        str(out),
    ]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("ffmpeg image conversion failed for %s: %s", src, exc)
        return False
    try:
        return proc.returncode == 0 and out.is_file() and out.stat().st_size > 0
    except OSError:
        return False


def iter_file_chunks(file_path: str | Path, chunk_size: int) -> Iterator[bytes]:
    with Path(file_path).open("rb") as handle:
        while True:
            data = handle.read(chunk_size)
            if not data:
                break
            yield data


def sha256_file(file_path: str | Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    for chunk in iter_file_chunks(file_path, chunk_size):
        digest.update(chunk)
    return digest.hexdigest()


def file_key_from_sha256(digest_hex: str, length: int = 12) -> str:
    if len(digest_hex) < length:
        raise ValueError("Digest is shorter than requested key length")
    return digest_hex[:length]


def random_file_key(length: int = 12) -> str:
    alphabet = "0123456789abcdef"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def load_aesgcm_key_from_env(env_name: str) -> bytes:
    encoded = os.getenv(env_name, "").strip()
    if not encoded:
        raise ValueError(f"{env_name} is not set")

    padding = "=" * ((4 - len(encoded) % 4) % 4)
    raw = base64.urlsafe_b64decode(encoded + padding)
    if len(raw) != 32:
        raise ValueError("AES-GCM key must be 32 bytes")
    return raw


def encrypt_bytes(plain: bytes, key: bytes) -> bytes:
    nonce = os.urandom(12)
    cipher = AESGCM(key).encrypt(nonce, plain, None)
    return b"ENC1" + nonce + cipher


def decrypt_bytes(payload: bytes, key: bytes) -> bytes:
    if not payload.startswith(b"ENC1"):
        raise ValueError("Encrypted payload header is missing")
    nonce = payload[4:16]
    cipher = payload[16:]
    if len(nonce) != 12:
        raise ValueError("Invalid nonce length")
    return AESGCM(key).decrypt(nonce, cipher, None)


def has_cryptg() -> bool:
    return importlib.util.find_spec("cryptg") is not None


def to_human_size(size: int | None) -> str:
    if size is None:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value))
    cleaned_chars: list[str] = []
    for ch in normalized:
        category = unicodedata.category(ch)
        if category.startswith("C"):
            continue
        cleaned_chars.append(ch)
    return "".join(cleaned_chars).strip()
