"""Общие примитивы REST API: контекст, ответы, ошибки и парсинг запросов."""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)

# Защита от случайно гигантского тела (пути файлов — небольшие JSON).
MAX_BODY_BYTES = 1 * 1024 * 1024


@dataclass
class ApiContext:
    """То, что нужно обработчикам: чтение (repo), запись (worker), авторизация.

    ``config`` нужен шар-ссылкам (download_root для поиска уже скачанного файла);
    ``share_dir`` — куда собирать файл для раздачи, если локально его ещё нет.
    """

    repo: Any
    worker: Any
    token: str = ""
    config: Any = None
    share_dir: str = ""


@dataclass
class FileResponse:
    """Ответ-файл (вместо JSON): обработчик стримит его с поддержкой Range."""

    path: str
    filename: str
    content_type: str = "application/octet-stream"


@dataclass
class StreamResponse:
    """Ответ-стрим: файл собирается ИЗ ЧАНКОВ на лету по запрошенному Range —
    скачиваются только нужные части, а не весь файл (инкремент 9/10).

    ``layout`` — :class:`app.core.stream.StreamLayout` (plaintext-смещения частей),
    ``cache_dir`` — куда складывать расшифрованные части (переиспользуются между
    запросами/перемоткой)."""

    token: str
    folder: str
    file_key: str
    filename: str
    content_type: str
    layout: Any
    cache_dir: str


class ApiError(Exception):
    """HTTP-ошибка с кодом и сообщением (сериализуется в ``{"error": ...}``)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _first(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    if not values:
        return default
    return str(values[0])


def _require_token(ctx: ApiContext, headers: dict[str, str], query: dict) -> None:
    token = str(ctx.token or "").strip()
    if not token:
        return  # авторизация отключена (полагаемся на host=127.0.0.1)
    provided = ""
    auth = ""
    for key, value in (headers or {}).items():
        if key.lower() == "authorization":
            auth = str(value or "")
            break
    if auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
    if not provided:
        provided = _first(query, "token").strip()
    if not provided or not secrets.compare_digest(provided, token):
        raise ApiError(401, "Unauthorized")


def _parse_json_body(body: bytes | None) -> dict[str, Any]:
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(400, f"Invalid JSON body: {exc}") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "JSON body must be an object")
    return data


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v or "").strip()]


