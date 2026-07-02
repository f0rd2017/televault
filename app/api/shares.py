"""Публичные шар-ссылки и локальная медиа-раздача REST API."""

from __future__ import annotations

import logging
import mimetypes
from typing import Any

from app.core.sharing import (
    hash_share_password,
    new_share_token,
    verify_share_password,
)
from app.core.utils import build_safe_output_path, now_ts

from app.api.common import (
    ApiContext,
    ApiError,
    FileResponse,
    StreamResponse,
    TranscodeResponse,
    _first,
)


def _transcode_requested(query: dict[str, list[str]]) -> bool:
    return _first(query, "transcode").strip().lower() in {"1", "true", "yes", "on"}


logger = logging.getLogger(__name__)


# ── Шар-ссылки ───────────────────────────────────────────────────────────────


def _share_public_url(ctx: ApiContext, token: str) -> str:
    api = getattr(ctx.config, "api", None) if ctx.config is not None else None
    if api is None:
        return f"/share/{token}"
    host = str(getattr(api, "host", "127.0.0.1") or "127.0.0.1")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"  # для отображения — реальный внешний адрес знает юзер
    return f"http://{host}:{int(getattr(api, 'port', 0))}/share/{token}"


def _lookup_object(ctx: ApiContext, folder: str, file_key: str):
    try:
        for obj in ctx.repo.list_objects_unified(folder):
            if obj.file_key == file_key:
                return obj
    except Exception:  # noqa: BLE001
        return None
    return None


def _handle_create_share(ctx: ApiContext, body: dict[str, Any]) -> dict[str, Any]:
    folder = str(body.get("folder") or body.get("folder_path") or "").strip()
    file_key = str(body.get("file_key") or "").strip()
    if not folder or not file_key:
        raise ApiError(400, "'folder' and 'file_key' are required")

    orig_name = str(body.get("orig_name") or "").strip()
    total_size = body.get("total_size")
    if not orig_name or total_size is None:
        obj = _lookup_object(ctx, folder, file_key)
        if obj is None and not orig_name:
            raise ApiError(404, "Object not found; pass 'orig_name' explicitly")
        if obj is not None:
            orig_name = orig_name or str(obj.orig_name)
            if total_size is None:
                total_size = obj.total_size
    if not orig_name:
        raise ApiError(400, "'orig_name' could not be resolved")

    password = str(body.get("password") or "")
    try:
        expires_in = int(body.get("expires_in_sec") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    expires_ts = (now_ts() + expires_in) if expires_in > 0 else 0

    token = new_share_token()
    ctx.repo.create_share(
        token,
        folder,
        file_key,
        orig_name,
        total_size=int(total_size) if total_size is not None else None,
        password_hash=hash_share_password(password),
        expires_ts=expires_ts,
    )
    return {
        "token": token,
        "url": _share_public_url(ctx, token),
        "path": f"/share/{token}",
        "has_password": bool(password),
        "expires_ts": expires_ts,
    }


def _handle_list_shares(ctx: ApiContext) -> dict[str, Any]:
    shares = [
        {k: v for k, v in s.items() if k != "password_hash"}
        for s in ctx.repo.list_shares()
    ]
    return {"shares": shares}


def _resolve_share(ctx: ApiContext, token: str, password: str) -> dict[str, Any]:
    """Проверить токен/отзыв/срок/пароль. Возвращает запись share или бросает
    ApiError. Чистая (без сокетов) — тестируется напрямую."""
    share = ctx.repo.get_share(token)
    if share is None:
        raise ApiError(404, "Share not found")
    if share.get("revoked"):
        raise ApiError(410, "Share has been revoked")
    expires = int(share.get("expires_ts") or 0)
    if expires > 0 and now_ts() >= expires:
        raise ApiError(410, "Share has expired")
    if share.get("password_hash") and not verify_share_password(
        password, str(share["password_hash"])
    ):
        raise ApiError(401, "Password required or incorrect")
    return share


def _ensure_share_file(ctx: ApiContext, share: dict[str, Any]) -> str | None:
    """Путь к собранному файлу: сперва ищем уже скачанный, иначе собираем из
    чанков через worker (блокирующе). None — собрать не удалось."""
    folder = str(share["folder_path"])
    file_key = str(share["file_key"])
    orig_name = str(share["orig_name"])
    total_size = share.get("total_size")
    if ctx.config is not None:
        try:
            local = build_safe_output_path(ctx.config.download_root, folder, orig_name)
            if local.is_file() and (
                total_size is None or local.stat().st_size == int(total_size)
            ):
                return str(local)
        except Exception:  # noqa: BLE001
            pass
    # Уже собран ранее в share-кэше? Не пересобираем на повторных запросах.
    if ctx.share_dir:
        try:
            cached = build_safe_output_path(ctx.share_dir, folder, orig_name)
            if cached.is_file() and (
                total_size is None or cached.stat().st_size == int(total_size)
            ):
                return str(cached)
        except Exception:  # noqa: BLE001
            pass
    if ctx.worker is None or not ctx.share_dir:
        return None
    return ctx.worker.assemble_file_blocking(folder, file_key, ctx.share_dir)


def _build_stream_layout(ctx: ApiContext, share: dict[str, Any]):
    """Попытаться построить раскладку частей для стрима без полного скачивания.

    Возвращает ``StreamLayout`` или ``None`` (тогда раздача откатится на полную
    сборку файла). Стрим возможен только для чанкованных объектов с известными
    размерами частей; batch-member (мелкие файлы в blob), неполные/несвязные
    объекты, отсутствие worker/config — всё уходит в None. Любая ошибка → None
    (раздача всё равно отдаст файл целиком).

    Контейнеры с индексом в хвосте (AVI/ASF/WMV/FLV/MPEG-PS) сюда НЕ фильтруются
    намеренно: детектор (см. _is_non_streamable_container) сам вынужден качать
    целую первую часть ради 64КБ заголовка, а полная пересборка как фолбэк
    синхронно блокирует HTTP-ответ на десятки секунд для крупных файлов — плеер
    не дожидается и рвёт соединение (таймаут), картинка вообще не появляется.
    Частичный Range-стрим на практике у таких файлов работает нормально —
    демуксер сам сикает куда нужно, сервер отдаёт любой запрошенный диапазон."""
    if ctx.worker is None or not ctx.share_dir or ctx.config is None:
        return None
    if not hasattr(ctx.worker, "fetch_stream_parts_blocking"):
        return None
    folder = str(share["folder_path"])
    file_key = str(share["file_key"])
    try:
        from app.core.stream import LayoutError, build_layout

        storage = str(ctx.repo.resolve_object_storage(folder, file_key)).strip().lower()
        if storage == "batch_member":
            return None  # мелкий файл в общем blob — собираем целиком, дёшево
        parts = ctx.repo.get_parts_for_object(folder_path=folder, file_key=file_key)
        caption_prefix = str(getattr(ctx.config, "caption_prefix", "FC1|"))
        try:
            return build_layout(parts, caption_prefix=caption_prefix)
        except LayoutError:
            return None
    except Exception:  # noqa: BLE001
        logger.debug("Stream layout build failed; falling back to full assembly")
        return None


def _handle_serve_share(
    ctx: ApiContext, token: str, query: dict[str, list[str]]
) -> FileResponse | StreamResponse | TranscodeResponse:
    password = _first(query, "pw") or _first(query, "password")
    share = _resolve_share(ctx, token, password)
    content_type = (
        mimetypes.guess_type(str(share["orig_name"]))[0] or "application/octet-stream"
    )

    # ?transcode=1 — пересобрать в fragmented MP4 на лету (не-нативный формат).
    # ffmpeg читает исходник с этого же сервера по обычному пути раздачи.
    if _transcode_requested(query):
        input_query = {"pw": password} if password else {}
        return TranscodeResponse(
            input_path=f"/share/{token}",
            input_query=input_query,
            filename=str(share["orig_name"]),
        )

    # Путь A — настоящий стрим: качаем только нужные части по Range.
    layout = _build_stream_layout(ctx, share)
    if layout is not None:
        from pathlib import Path

        cache_dir = str(Path(ctx.share_dir) / ".stream" / str(share["file_key"]))
        return StreamResponse(
            token=token,
            folder=str(share["folder_path"]),
            file_key=str(share["file_key"]),
            filename=str(share["orig_name"]),
            content_type=content_type,
            layout=layout,
            cache_dir=cache_dir,
        )

    # Путь B — фолбэк: собрать файл целиком и отдать (тоже с Range, но после сборки).
    path = _ensure_share_file(ctx, share)
    if not path:
        raise ApiError(503, "File could not be assembled from storage")
    ctx.repo.increment_share_downloads(token)
    return FileResponse(
        path=path, filename=str(share["orig_name"]), content_type=content_type
    )


def _mp4_needs_full_assembly(head: bytes) -> bool:
    """True, если в начале MP4/MOV первым крупным боксом идёт ``mdat`` (``moov`` в
    хвосте) — частичный Range-стрим отдаёт mdat без индекса, видеодорожка не
    собирается. False для faststart (``moov`` раньше ``mdat``)."""
    moov_pos = head.find(b"moov")
    mdat_pos = head.find(b"mdat")
    return mdat_pos != -1 and (moov_pos == -1 or mdat_pos < moov_pos)


# Контейнеры с индексом в хвосте файла — теоретически частичный Range-стрим им
# может ломать видеодорожку (демуксер сикает в конец за индексом). На практике
# для реальных файлов это не подтвердилось, а фолбэк (полная сборка) синхронно
# блокирует HTTP-ответ и рвёт плеер по таймауту на крупных файлах — то есть хуже
# исходной проблемы. _is_non_streamable_container ниже НЕ вызывается из раздачи
# (см. _build_stream_layout) — оставлено как готовый детектор на случай, если
# для какого-то формата понадобится не блокирующий, а асинхронный/предзаборный
# фолбэк.
_NON_STREAMABLE_EXT = (
    ".avi",
    ".flv",
    ".wmv",
    ".asf",
    ".vob",
    ".mpg",
    ".mpeg",
    ".ts",
    ".m2ts",
)


def _is_non_streamable_container(
    ctx: ApiContext,
    folder: str,
    file_key: str,
    orig_name: str,
    ext_fallback: tuple[str, ...],
) -> bool:
    """True для контейнеров с индексом в хвосте файла (AVI/ASF/WMV/FLV/MPEG-PS) —
    их частичный Range-стрим ломает видеодорожку. Контейнер определяем по сигнатуре
    первых байт part 0 (а не по расширению: файл может быть MP4 с именем .avi);
    если part 0 прочитать не удалось — откатываемся на эвристику по расширению."""
    try:
        from pathlib import Path

        cache_dir = str(Path(ctx.share_dir) / ".stream" / file_key)
        part_paths = ctx.worker.fetch_stream_parts_blocking(
            folder, file_key, [0], cache_dir
        )
        head_path = part_paths.get(0)
        if not head_path:
            raise FileNotFoundError("part 0 unavailable")
        with open(head_path, "rb") as handle:
            head = handle.read(65536)
    except Exception:  # noqa: BLE001
        logger.debug("Container sniff failed; falling back to extension heuristic")
        return orig_name.lower().endswith(ext_fallback)

    if head[4:8] == b"ftyp" or head[:4] == b"\x1a\x45\xdf\xa3":
        if head[:4] == b"\x1a\x45\xdf\xa3":
            return False  # Matroska/WebM — стримится по Range
        if _mp4_needs_full_assembly(head):
            return True  # moov в хвосте — частичный Range ломает видео
        return False  # faststart MP4/MOV — стримится по Range
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return True
    if head[:4] == b"\x30\x26\xb2\x75":  # ASF/WMV
        return True
    if head[:3] == b"FLV":
        return True
    if head[:4] == b"\x00\x00\x01\xba":  # MPEG-PS
        return True
    return orig_name.lower().endswith(ext_fallback)


def _handle_local_media(
    ctx: ApiContext, query: dict[str, list[str]]
) -> FileResponse | StreamResponse | TranscodeResponse:
    """Локальный просмотр объекта без полного скачивания (для GUI). Требует
    api-токен. Стримит по Range только нужные части; для batch-member/неполных
    объектов откатывается на полную сборку. ``transcode=1`` — пересобрать в
    fragmented MP4 на лету (для форматов, которые плеер не берёт нативно)."""
    folder = _first(query, "folder")
    file_key = _first(query, "file_key")
    if not folder or not file_key:
        raise ApiError(400, "folder and file_key are required")
    if ctx.repo is None:
        raise ApiError(503, "repository unavailable")

    if _transcode_requested(query):
        obj = _lookup_object(ctx, folder, file_key)
        input_query = {"folder": folder, "file_key": file_key}
        if ctx.token:
            input_query["token"] = ctx.token
        return TranscodeResponse(
            input_path="/api/media",
            input_query=input_query,
            filename=str(getattr(obj, "orig_name", "") or "file"),
        )
    parts = ctx.repo.get_parts_for_object(folder_path=folder, file_key=file_key)
    if not parts:
        # Мелкие файлы хранятся как batch-member (в общем blob, без частей в
        # msg_index) — у них нет «частей», поэтому стрим невозможен. Не 404-им,
        # а собираем файл целиком из blob и отдаём (Path B).
        obj = _lookup_object(ctx, folder, file_key)
        if obj is None:
            raise ApiError(404, "Object not found")
        member_name = str(getattr(obj, "orig_name", "") or "file")
        member_pseudo = {
            "folder_path": folder,
            "file_key": file_key,
            "orig_name": member_name,
            "total_size": getattr(obj, "total_size", None),
        }
        member_ctype = (
            mimetypes.guess_type(member_name)[0] or "application/octet-stream"
        )
        member_path = _ensure_share_file(ctx, member_pseudo)
        if not member_path:
            raise ApiError(503, "File could not be assembled from storage")
        return FileResponse(
            path=member_path, filename=member_name, content_type=member_ctype
        )
    orig_name = str(getattr(parts[0], "orig_name", "") or "file")
    pseudo = {
        "folder_path": folder,
        "file_key": file_key,
        "orig_name": orig_name,
        "total_size": None,
    }
    content_type = mimetypes.guess_type(orig_name)[0] or "application/octet-stream"

    # Путь A — стрим: качаем только перекрытые Range части. Контейнеры с индексом
    # в хвосте файла (AVI/ASF/WMV/FLV/MPEG-PS, не-faststart MP4) сюда не попадают —
    # _build_stream_layout уже отсеивает их через _layout_is_streamable, возвращая
    # None, и раздача откатывается на Путь B ниже.
    layout = _build_stream_layout(ctx, pseudo)
    if layout is not None:
        from pathlib import Path

        cache_dir = str(Path(ctx.share_dir) / ".stream" / file_key)
        return StreamResponse(
            token="",
            folder=folder,
            file_key=file_key,
            filename=orig_name,
            content_type=content_type,
            layout=layout,
            cache_dir=cache_dir,
        )

    # Путь B — фолбэк: собрать файл целиком (мелкий/неполный) и отдать с Range.
    path = _ensure_share_file(ctx, pseudo)
    if not path:
        raise ApiError(503, "File could not be assembled from storage")
    return FileResponse(path=path, filename=orig_name, content_type=content_type)
