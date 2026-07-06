"""Local REST API on top of the core (roadmap increment 5).

A thin wrapper around the stdlib ``http.server`` (no new dependencies): reads go
through ``DbRepo`` (SQLite in WAL mode allows concurrent readers), writes go
through ``worker.submit_job`` (which is picked up by the worker's asyncio loop,
the same path used from the GUI). The server runs in a daemon thread; disabled
by default (see ``ApiConfig``).

Routing is factored out into the pure function :func:`dispatch` so it can be
called directly in tests without sockets. The HTTP handler only reads the
body/headers and calls dispatch.

Endpoints:
  GET  /api/health                        — liveness check (no auth)
  GET  /api/folders                       — list folders
  GET  /api/files?folder=&search=&recursive=&status=  — list objects
  GET  /api/jobs?limit=                   — recent jobs
  GET  /api/jobs/{id}                     — a single job (for polling progress)
  POST /api/upload    {paths:[...], folder:"..."}  — enqueue an upload
  POST /api/download  {folder, file_key, allow_incomplete?}  — enqueue a download
  POST /api/delete    {folder, file_key}  — enqueue a delete from cloud storage
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.core.types import JobType

from app.api.common import (
    ApiContext,
    ApiError,
    FileResponse,
    MAX_BODY_BYTES,
    StreamResponse,
    TranscodeResponse,
    _first,
    _parse_json_body,
    _require_token,
    _str_list,
)
from app.api.shares import (
    _handle_create_share,
    _handle_list_shares,
    _handle_local_media,
    _handle_serve_share,
)

logger = logging.getLogger(__name__)


# ── Route handlers ───────────────────────────────────────────────────────────


def _handle_folders(ctx: ApiContext) -> dict[str, Any]:
    return {"folders": [asdict(f) for f in ctx.repo.list_folders()]}


def _handle_files(ctx: ApiContext, query: dict) -> dict[str, Any]:
    folder = _first(query, "folder").strip() or None
    search = _first(query, "search").strip() or None
    status = _first(query, "status").strip() or None
    recursive = _first(query, "recursive").strip().lower() in {"1", "true", "yes"}
    try:
        objects = ctx.repo.list_objects_unified(
            folder, search, status, recursive=recursive
        )
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    return {"files": [asdict(o) for o in objects]}


def _handle_jobs(ctx: ApiContext, query: dict) -> dict[str, Any]:
    try:
        limit = int(_first(query, "limit", "100"))
    except ValueError:
        limit = 100
    return {"jobs": ctx.repo.list_jobs(limit=limit)}


def _handle_job_by_id(ctx: ApiContext, job_id_raw: str) -> dict[str, Any]:
    try:
        job_id = int(job_id_raw)
    except ValueError as exc:
        raise ApiError(400, "job id must be an integer") from exc
    job = ctx.repo.get_job(job_id)
    if job is None:
        raise ApiError(404, f"Job {job_id} not found")
    return {"job": job}


def _submit(ctx: ApiContext, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    accepted = bool(ctx.worker.submit_job(job_type, payload))
    if not accepted:
        raise ApiError(503, "Worker is not ready to accept jobs")
    # submit_job is fire-and-forget via the worker's loop; the id is assigned later
    # at insert_job time. The client polls progress via GET /api/jobs.
    return {"accepted": True, "job_type": job_type}


def _handle_upload(ctx: ApiContext, body: dict[str, Any]) -> dict[str, Any]:
    paths = _str_list(body.get("paths") or body.get("file_paths"))
    if not paths:
        raise ApiError(400, "'paths' must be a non-empty list of file paths")
    from pathlib import Path

    missing = [p for p in paths if not Path(p).expanduser().is_file()]
    if missing:
        raise ApiError(400, f"Not a file or does not exist: {missing[0]}")
    folder = str(body.get("folder") or body.get("folder_path") or "").strip()
    payload: dict[str, Any] = {
        "file_paths": [str(Path(p).expanduser()) for p in paths],
        "folder_path": folder,
    }
    return _submit(ctx, JobType.UPLOAD.value, payload)


def _handle_download(ctx: ApiContext, body: dict[str, Any]) -> dict[str, Any]:
    folder = str(body.get("folder") or body.get("folder_path") or "").strip()
    file_key = str(body.get("file_key") or "").strip()
    if not folder or not file_key:
        raise ApiError(400, "'folder' and 'file_key' are required")
    payload: dict[str, Any] = {
        "folder_path": folder,
        "file_key": file_key,
        "allow_incomplete": bool(body.get("allow_incomplete", False)),
    }
    return _submit(ctx, JobType.DOWNLOAD.value, payload)


def _handle_delete(ctx: ApiContext, body: dict[str, Any]) -> dict[str, Any]:
    folder = str(body.get("folder") or body.get("folder_path") or "").strip()
    file_key = str(body.get("file_key") or "").strip()
    if not folder or not file_key:
        raise ApiError(400, "'folder' and 'file_key' are required")
    return _submit(
        ctx, JobType.DELETE.value, {"folder_path": folder, "file_key": file_key}
    )


def dispatch(
    ctx: ApiContext,
    method: str,
    path: str,
    query: dict[str, list[str]],
    headers: dict[str, str],
    body: bytes | None,
) -> tuple[int, dict[str, Any]] | FileResponse | StreamResponse:
    """Pure routing: method+path → (HTTP code, JSON) or :class:`FileResponse`.

    Doesn't touch sockets, so it can be tested directly. An :class:`ApiError`
    is converted here into an ``{"error": ...}`` response with its status code,
    so the function never raises on expected errors — it only returns.
    """
    try:
        return _route(ctx, method, path, query, headers, body)
    except ApiError as exc:
        return exc.status, {"error": exc.message}


def _route(
    ctx: ApiContext,
    method: str,
    path: str,
    query: dict[str, list[str]],
    headers: dict[str, str],
    body: bytes | None,
) -> tuple[int, dict[str, Any]] | FileResponse | StreamResponse:
    path = path.rstrip("/") or "/"

    # Liveness check — no auth required.
    if method == "GET" and path == "/api/health":
        return 200, {"status": "ok", "service": "tg_bd"}

    # Public serving of share links (no API token — the token itself is the secret).
    if method == "GET" and path.startswith("/share/"):
        token = path[len("/share/") :].strip("/")
        if not token:
            raise ApiError(404, "Share token missing")
        return _handle_serve_share(ctx, token, query)

    _require_token(ctx, headers, query)

    if method == "GET":
        if path == "/api/folders":
            return 200, _handle_folders(ctx)
        if path == "/api/files":
            return 200, _handle_files(ctx, query)
        if path == "/api/jobs":
            return 200, _handle_jobs(ctx, query)
        if path.startswith("/api/jobs/"):
            return 200, _handle_job_by_id(ctx, path[len("/api/jobs/") :])
        if path == "/api/shares":
            return 200, _handle_list_shares(ctx)
        if path == "/api/media":
            return _handle_local_media(ctx, query)
        raise ApiError(404, "Unknown endpoint")

    if method == "POST":
        data = _parse_json_body(body)
        if path == "/api/upload":
            return 202, _handle_upload(ctx, data)
        if path == "/api/download":
            return 202, _handle_download(ctx, data)
        if path == "/api/delete":
            return 202, _handle_delete(ctx, data)
        if path == "/api/shares":
            return 201, _handle_create_share(ctx, data)
        if path.startswith("/api/shares/") and path.endswith("/revoke"):
            token = path[len("/api/shares/") : -len("/revoke")].strip("/")
            removed = ctx.repo.revoke_share(token)
            if not removed:
                raise ApiError(404, "Share not found")
            return 200, {"revoked": True, "token": token}
        raise ApiError(404, "Unknown endpoint")

    if method == "DELETE":
        if path.startswith("/api/shares/"):
            token = path[len("/api/shares/") :].strip("/")
            removed = ctx.repo.delete_share(token)
            if not removed:
                raise ApiError(404, "Share not found")
            return 200, {"deleted": True, "token": token}
        raise ApiError(404, "Unknown endpoint")

    raise ApiError(405, f"Method {method} not allowed")


# ── HTTP glue ────────────────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    server_version = "TeleVault-API/1"
    protocol_version = "HTTP/1.1"
    # Never serve more than this amount of data in a single Range response —
    # otherwise an open-ended `Range: bytes=0-` from the player expands to
    # `0..size-1` and pulls a huge (sometimes the entire) chunk at once. The
    # player will request the next windows as playback progresses. See
    # _serve_stream/_prefetch_next.
    _STREAM_WINDOW_BYTES = 12 * 1024 * 1024

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        logger.debug("api %s - %s", self.address_string(), fmt % args)

    @property
    def _ctx(self) -> ApiContext:
        return self.server.ctx  # type: ignore[attr-defined]

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "Request body too large")
        return self.rfile.read(length)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            body = self._read_body() if method == "POST" else b""
            headers = {k: v for k, v in self.headers.items()}
            result = dispatch(self._ctx, method, parsed.path, query, headers, body)
        except ApiError as exc:
            self._write_json(exc.status, {"error": exc.message})
            return
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled API error for %s %s", method, self.path)
            self._write_json(500, {"error": "internal server error"})
            return
        if isinstance(result, TranscodeResponse):
            self._serve_transcode(result)
            return
        if isinstance(result, StreamResponse):
            self._serve_stream(result)
            return
        if isinstance(result, FileResponse):
            self._serve_file(result)
            return
        status, payload = result
        self._write_json(status, payload)

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # The client (FFmpeg/QMediaPlayer) already closed the connection, e.g.
            # while seeking or closing the player. Not an error — just return.
            return

    def _resolve_range(self, size: int) -> tuple[int, int, bool] | None:
        """Parse the ``Range`` header against ``size``.

        Returns ``(start, end, is_range)`` (end inclusive), or ``None`` if the
        range can't be satisfied (the caller must then respond with 416)."""
        range_header = self.headers.get("Range", "")
        if not range_header.startswith("bytes="):
            return 0, size - 1, False
        try:
            spec = range_header[len("bytes=") :].split(",")[0].strip()
            lo, _, hi = spec.partition("-")
            if lo == "":  # suffix range bytes=-N
                length = int(hi)
                start = max(0, size - length)
                end = size - 1
            else:
                start = int(lo)
                end = int(hi) if hi else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                return None
            return start, end, True
        except (ValueError, IndexError):
            return 0, size - 1, False

    def _send_416(self, size: int) -> None:
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.end_headers()

    def _send_media_headers(
        self,
        *,
        status: int,
        content_type: str,
        length: int,
        filename: str,
        content_range: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if content_range is not None:
            self.send_header("Content-Range", content_range)
        # The filename may contain Cyrillic/emoji; HTTP headers are encoded as
        # latin-1, so a raw non-ASCII name crashes send_header (UnicodeEncodeError)
        # and aborts the response — the player gets neither data nor a thumbnail.
        # Provide an ASCII fallback plus an RFC 5987 filename* (UTF-8, percent-encoded).
        from urllib.parse import quote

        safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "")
        ascii_name = safe_name.encode("ascii", "ignore").decode("ascii") or "file"
        if ascii_name == safe_name:
            disposition = f'inline; filename="{ascii_name}"'
        else:
            disposition = (
                f'inline; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(safe_name, safe='')}"
            )
        self.send_header("Content-Disposition", disposition)
        self.end_headers()

    def _serve_file(self, fr: FileResponse) -> None:
        """Serve an already-assembled file with HTTP Range support (streaming/seeking)."""
        import os

        try:
            size = os.path.getsize(fr.path)
        except OSError:
            self._write_json(404, {"error": "file unavailable"})
            return

        resolved = self._resolve_range(size)
        if resolved is None:
            self._send_416(size)
            return
        start, end, is_range = resolved
        length = end - start + 1
        self._send_media_headers(
            status=206 if is_range else 200,
            content_type=fr.content_type,
            length=length,
            filename=fr.filename,
            content_range=f"bytes {start}-{end}/{size}" if is_range else None,
        )
        if self.command == "HEAD":
            return
        with open(fr.path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(1024 * 256, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def _serve_stream(self, sr: StreamResponse) -> None:
        """Serve a file by ASSEMBLING it from chunks on the fly for the requested
        Range — only the parts overlapping the range are downloaded (increment 9/10)."""
        from app.core.stream import iter_range_bytes

        layout = sr.layout
        size = int(layout.total_size)

        resolved = self._resolve_range(size)
        if resolved is None:
            self._send_416(size)
            return
        start, end, is_range = resolved

        # Download (or take from cache) ONLY the parts overlapping the range.
        # Stream window: never serve more than ~_STREAM_WINDOW_BYTES in a single
        # Range response. Otherwise an open-ended `Range: bytes=0-` from the player
        # expands to `0..size-1` and pulls the ENTIRE file at once — a flood of
        # getFile calls → FloodWait, and playback only starts once everything has
        # downloaded. The player will request the next windows as playback
        # progresses, and after seeking it sends a Range from the new position —
        # parts are downloaded from the current timeline point to the end.
        # The window is intentionally small (not 48+ MB): the time to first byte
        # for the player is the time to download the parts covering the window,
        # not the whole window, so a small window means a fast playback start.
        # The rest of the window is fetched as playback progresses, and
        # _prefetch_next warms the next part ahead of time.
        needed = layout.select_parts(start, end)
        if needed and (end - start + 1) > self._STREAM_WINDOW_BYTES:
            capped = []
            for _part in needed:
                capped.append(_part)
                if _part.plain_end - start >= self._STREAM_WINDOW_BYTES:
                    break
            if len(capped) < len(needed):
                needed = capped
                end = needed[-1].plain_end - 1
                is_range = True  # response became partial → 206 + Content-Range
        part_indices = [p.part_index for p in needed]
        # Parts can be huge (hundreds of MB) — without this, the player would wait
        # for the ENTIRE part to download, even though the current window only
        # needs a small chunk at the start of it. This only works for unencrypted
        # objects (see TgDownloader.fetch_parts_decrypted) — for encrypted ones
        # the downloader ignores the hint and downloads the whole part anyway.
        prefix_bytes = {
            p.part_index: (min(end, p.plain_end - 1) - p.plain_start) + 1
            for p in needed
        }
        part_paths: dict[int, str] = {}
        if part_indices:
            part_paths = self._ctx.worker.fetch_stream_parts_blocking(
                sr.folder,
                sr.file_key,
                part_indices,
                sr.cache_dir,
                prefix_bytes=prefix_bytes,
            )
        missing = [p.part_index for p in needed if p.part_index not in part_paths]
        if missing:
            self._write_json(503, {"error": "stream parts could not be fetched"})
            return

        # Read-ahead prefetch: while the current window streams to the player, warm
        # the next part into the same cache in the background — by the time the
        # player reaches it, it's already on disk, so its Range request doesn't
        # stall on a download (no pause).
        if needed:
            self._prefetch_next(sr, layout, needed[-1].part_index)

        # Count a "download" only once — on a request starting from the beginning.
        if start == 0:
            try:
                self._ctx.repo.increment_share_downloads(sr.token)
            except Exception:  # noqa: BLE001
                logger.debug("increment_share_downloads failed", exc_info=True)

        length = end - start + 1
        self._send_media_headers(
            status=206 if is_range else 200,
            content_type=sr.content_type,
            length=length,
            filename=sr.filename,
            content_range=f"bytes {start}-{end}/{size}" if is_range else None,
        )
        if self.command == "HEAD":
            return
        try:
            for chunk in iter_range_bytes(layout, part_paths, start, end):
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
        except FileNotFoundError:
            logger.warning("Stream part vanished mid-serve for token=%s", sr.token)

    def _prefetch_next(self, sr: StreamResponse, layout, last_index: int) -> None:
        """Proactively download the NEXT part in the background while the current
        one is streaming, so it's already cached by the time the player reaches
        it (no pause)."""
        import threading

        next_index = last_index + 1
        if next_index >= len(layout.parts):
            return  # this was the last part — nothing ahead to warm
        inflight = globals().setdefault("_STREAM_PREFETCH_INFLIGHT", set())
        lock = globals().setdefault("_STREAM_PREFETCH_LOCK", threading.Lock())
        key = (sr.file_key, next_index)
        with lock:
            if key in inflight:
                return
            inflight.add(key)

        def _warm() -> None:
            try:
                # Warm only one window ahead, not the whole (possibly huge) next
                # part — same idea as prefix_bytes in _serve_stream: the rest is
                # fetched as playback progresses.
                self._ctx.worker.fetch_stream_parts_blocking(
                    sr.folder,
                    sr.file_key,
                    [next_index],
                    sr.cache_dir,
                    prefix_bytes={next_index: self._STREAM_WINDOW_BYTES},
                )
            finally:
                with lock:
                    inflight.discard(key)

        threading.Thread(target=_warm, daemon=True).start()

    def _serve_transcode(self, tr: TranscodeResponse) -> None:
        """Serve the source repackaged by ffmpeg into fragmented MP4 on the fly.

        ffmpeg's input is this same server (a normal Range-based serve: ffmpeg
        seeks on its own using the container index); the output is a stream of
        unknown length, so no Content-Length and no Range: playback starts from
        the first byte, no seeking in v1. See app.core.transcode."""
        import subprocess
        from urllib.parse import urlencode

        from app.core.transcode import (
            build_ffmpeg_args,
            plan_from_probe,
            probe_media,
            transcode_available,
        )

        if not transcode_available():
            self._write_json(501, {"error": "ffmpeg/ffprobe not available on server"})
            return

        host, port = self.server.server_address[:2]
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        query = urlencode(tr.input_query)
        input_url = f"http://{host}:{int(port)}{tr.input_path}" + (
            f"?{query}" if query else ""
        )

        probe = probe_media(input_url)
        if probe is None:
            self._write_json(502, {"error": "source is not readable as media"})
            return
        plan = plan_from_probe(probe)
        if plan.video_codec is None and plan.audio_codec is None:
            self._write_json(415, {"error": "no audio/video streams in source"})
            return
        logger.info(
            "Transcode start: %s → fMP4 (%s)",
            tr.filename,
            "remux" if plan.is_remux_only else "transcode",
        )

        try:
            proc = subprocess.Popen(  # noqa: S603 — fixed binary resolved from PATH
                build_ffmpeg_args(input_url, plan),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self._write_json(500, {"error": f"failed to start ffmpeg: {exc}"})
            return

        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            # Length is unknown (live pipe) — end of stream = connection close.
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if self.command == "HEAD":
                return
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(256 * 1024)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break  # player closed the connection — ffmpeg is killed in finally
        finally:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                logger.debug("ffmpeg did not exit cleanly after kill")

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")


class ApiServer:
    """Starts/stops the REST API in a background daemon thread."""

    def __init__(self, config: Any, repo: Any, worker: Any) -> None:
        self._config = config
        share_dir = ""
        try:
            from pathlib import Path

            share_dir = str(Path(config.cache_dir).expanduser() / ".share_cache")
        except Exception:  # noqa: BLE001
            share_dir = ""
        self._ctx = ApiContext(
            repo=repo,
            worker=worker,
            token=str(config.api.token or "").strip(),
            config=config,
            share_dir=share_dir,
        )
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def address(self) -> tuple[str, int] | None:
        """The actual (host, port) after startup — the port is real even if the
        config had 0 (ephemeral). None if the server isn't running."""
        if self._httpd is None:
            return None
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    def start(self) -> bool:
        api = self._config.api
        if not api.enabled:
            return False
        try:
            httpd = ThreadingHTTPServer((api.host, int(api.port)), _Handler)
        except OSError as exc:
            logger.error("REST API failed to bind %s:%s — %s", api.host, api.port, exc)
            return False
        httpd.daemon_threads = True
        httpd.ctx = self._ctx  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="api-server", daemon=True
        )
        self._thread.start()
        if not self._ctx.token:
            logger.warning(
                "REST API enabled WITHOUT a token — auth is OFF "
                "(relying on host binding %s). Set api.token to require Bearer auth.",
                api.host,
            )
        logger.info("REST API listening on http://%s:%s", api.host, api.port)
        return True

    def ensure_media_server(self) -> tuple[str, str] | None:
        """Ensure a local server is running for viewing without downloading.
        If the REST API is already running (api.enabled), reuse it. Otherwise
        spin up a loopback server on 127.0.0.1 with an ephemeral port and a
        random token. Returns (base_url, token) or None."""
        if self._httpd is None:
            if not self._ctx.token:
                self._ctx.token = secrets.token_urlsafe(24)
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            except OSError as exc:
                logger.error("Media server failed to bind 127.0.0.1 — %s", exc)
                return None
            httpd.daemon_threads = True
            httpd.ctx = self._ctx  # type: ignore[attr-defined]
            self._httpd = httpd
            self._thread = threading.Thread(
                target=httpd.serve_forever, name="api-media-server", daemon=True
            )
            self._thread.start()
            logger.info(
                "Local media server listening on http://127.0.0.1:%s",
                httpd.server_address[1],
            )
        addr = self.address
        if addr is None:
            return None
        host, port = addr
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"http://{host}:{port}", str(self._ctx.token or "")

    def stop(self) -> None:
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:  # noqa: BLE001
                logger.debug("Error while stopping REST API", exc_info=True)
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=3.0)
