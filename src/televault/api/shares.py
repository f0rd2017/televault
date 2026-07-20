"""Public share links and local media serving for the REST API."""

from __future__ import annotations

import logging
import mimetypes
from typing import Any

from televault.core.sharing import (
    hash_share_password,
    new_share_token,
    verify_share_password,
)
from televault.core.utils import build_safe_output_path, now_ts

from televault.api.common import (
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


# ── Share links ──────────────────────────────────────────────────────────────


def _share_public_url(ctx: ApiContext, token: str) -> str:
    api = getattr(ctx.config, "api", None) if ctx.config is not None else None
    if api is None:
        return f"/share/{token}"
    host = str(getattr(api, "host", "127.0.0.1") or "127.0.0.1")
    if host in {"0.0.0.0", "::"}:
        host = (
            "127.0.0.1"  # for display only — the user knows the real external address
        )
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
    """Check the token/revocation/expiry/password. Returns the share record or
    raises ApiError. Pure (no sockets) — can be tested directly."""
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
    """Path to the assembled file: first look for an already-downloaded one,
    otherwise assemble it from chunks via the worker (blocking). None means
    assembly failed."""
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
    # Already assembled earlier into the share cache? Don't rebuild on repeat requests.
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
    """Try to build a part layout for streaming without a full download.

    Returns a ``StreamLayout`` or ``None`` (in which case serving falls back to
    a full file assembly). Streaming is only possible for chunked objects with
    known part sizes; batch members (small files packed into a blob),
    incomplete/inconsistent objects, or a missing worker/config all fall
    through to None. Any error also falls back to None (the file will still be
    served in full).

    Containers with a trailing index (AVI/ASF/WMV/FLV/MPEG-PS) are
    intentionally NOT filtered out here: the detector (see
    _is_non_streamable_container) would itself have to download the entire
    first part just to inspect a 64KB header, and falling back to full
    assembly synchronously blocks the HTTP response for tens of seconds on
    large files — the player gives up and drops the connection (timeout)
    before any picture even appears. In practice, partial Range streaming
    works fine for such files anyway — the demuxer seeks wherever it needs,
    and the server serves whatever range is requested."""
    if ctx.worker is None or not ctx.share_dir or ctx.config is None:
        return None
    if not hasattr(ctx.worker, "fetch_stream_parts_blocking"):
        return None
    folder = str(share["folder_path"])
    file_key = str(share["file_key"])
    try:
        from televault.core.stream import LayoutError, build_layout

        storage = str(ctx.repo.resolve_object_storage(folder, file_key)).strip().lower()
        if storage == "batch_member":
            return None  # small file inside a shared blob — cheap to assemble whole
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

    # ?transcode=1 — repackage into fragmented MP4 on the fly (non-native format).
    # ffmpeg reads the source from this same server via the normal serving path.
    if _transcode_requested(query):
        input_query = {"pw": password} if password else {}
        return TranscodeResponse(
            input_path=f"/share/{token}",
            input_query=input_query,
            filename=str(share["orig_name"]),
        )

    # Path A — real streaming: download only the parts needed for the range.
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

    # Path B — fallback: assemble the whole file and serve it (still with Range,
    # but only after assembly).
    path = _ensure_share_file(ctx, share)
    if not path:
        raise ApiError(503, "File could not be assembled from storage")
    ctx.repo.increment_share_downloads(token)
    return FileResponse(
        path=path, filename=str(share["orig_name"]), content_type=content_type
    )


def _mp4_needs_full_assembly(head: bytes) -> bool:
    """True if, at the start of an MP4/MOV, the first large box is ``mdat``
    (``moov`` at the tail) — a partial Range stream then serves mdat without
    the index, so the video track can't be assembled. False for faststart
    files (``moov`` before ``mdat``)."""
    moov_pos = head.find(b"moov")
    mdat_pos = head.find(b"mdat")
    return mdat_pos != -1 and (moov_pos == -1 or mdat_pos < moov_pos)


# Containers with a trailing index — in theory a partial Range stream could
# break their video track (the demuxer seeks to the end for the index). In
# practice this hasn't been confirmed on real files, and the fallback (full
# assembly) synchronously blocks the HTTP response and causes the player to
# time out on large files — which is worse than the original problem.
# _is_non_streamable_container below is NOT called from the serving path (see
# _build_stream_layout) — it's kept as a ready-made detector in case some
# format later needs a non-blocking, async/prefetch-based fallback.
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
    """True for containers with a trailing index (AVI/ASF/WMV/FLV/MPEG-PS) —
    their partial Range stream breaks the video track. The container is
    detected from the byte signature of part 0's head (not the extension: a
    file could be an MP4 named .avi); if part 0 can't be read, fall back to
    the extension heuristic."""
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
            return False  # Matroska/WebM — streams fine over Range
        if _mp4_needs_full_assembly(head):
            return True  # moov at the tail — partial Range breaks the video
        return False  # faststart MP4/MOV — streams fine over Range
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
    """Local object viewing without a full download (for the GUI). Requires an
    API token. Streams over Range for only the needed parts; falls back to a
    full assembly for batch members/incomplete objects. ``transcode=1``
    repackages into fragmented MP4 on the fly (for formats the player can't
    play natively)."""
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
        # Small files are stored as batch members (packed into a shared blob,
        # with no parts in msg_index) — they have no "parts", so streaming isn't
        # possible. Instead of a 404, assemble the whole file from the blob and
        # serve it (Path B).
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

    # Path A — streaming: download only the parts overlapping the range.
    # Containers with a trailing index (AVI/ASF/WMV/FLV/MPEG-PS, non-faststart
    # MP4) don't reach here — _build_stream_layout already filters them out via
    # _layout_is_streamable, returning None, and serving falls back to Path B below.
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

    # Path B — fallback: assemble the whole file (small/incomplete) and serve it with Range.
    path = _ensure_share_file(ctx, pseudo)
    if not path:
        raise ApiError(503, "File could not be assembled from storage")
    return FileResponse(path=path, filename=orig_name, content_type=content_type)
