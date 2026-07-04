"""Shared REST API primitives: context, responses, errors, and request parsing."""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)

# Guard against an accidentally huge body (file paths are small JSON payloads).
MAX_BODY_BYTES = 1 * 1024 * 1024


@dataclass
class ApiContext:
    """Everything handlers need: reads (repo), writes (worker), auth.

    ``config`` is needed by share links (download_root, to look for an already
    downloaded file); ``share_dir`` is where to assemble the file for serving
    if it isn't available locally yet.
    """

    repo: Any
    worker: Any
    token: str = ""
    config: Any = None
    share_dir: str = ""


@dataclass
class FileResponse:
    """A file response (instead of JSON): the handler streams it with Range support."""

    path: str
    filename: str
    content_type: str = "application/octet-stream"


@dataclass
class StreamResponse:
    """A streaming response: the file is assembled FROM CHUNKS on the fly for the
    requested Range — only the needed parts are downloaded, not the whole file
    (increment 9/10).

    ``layout`` is a :class:`app.core.stream.StreamLayout` (plaintext part offsets);
    ``cache_dir`` is where decrypted parts are stored (reused across
    requests/seeking)."""

    token: str
    folder: str
    file_key: str
    filename: str
    content_type: str
    layout: Any
    cache_dir: str


@dataclass
class TranscodeResponse:
    """A transcode response: ffmpeg repackages the source into fragmented MP4
    on the fly.

    ``input_path``/``input_query`` are the path and query of THIS SAME server,
    where ffmpeg reads the source from (a normal Range-based serve); the full
    URL is assembled by the handler — only it knows the actual port. The output
    is a chunked stream without Range/seeking support (see app.core.transcode)."""

    input_path: str
    input_query: dict[str, str]
    filename: str


class ApiError(Exception):
    """An HTTP error with a code and message (serialized as ``{"error": ...}``)."""

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
        return  # auth is disabled (relying on host=127.0.0.1)
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
