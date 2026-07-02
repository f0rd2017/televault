"""Локальный REST API поверх ядра (инкремент 5 roadmap).

Тонкая обёртка на stdlib ``http.server`` (без новых зависимостей): чтение — через
``DbRepo`` (SQLite в WAL допускает конкурентных читателей), запись — через
``worker.submit_job`` (уходит в asyncio-loop воркера, тот же путь, что и из GUI).
Сервер крутится в демон-потоке; выключен по умолчанию (см. ``ApiConfig``).

Маршрутизация вынесена в чистую функцию :func:`dispatch` — её можно дёргать в
тестах без сокетов. HTTP-обработчик лишь читает тело/заголовки и зовёт dispatch.

Эндпоинты:
  GET  /api/health                        — живость (без авторизации)
  GET  /api/folders                       — список папок
  GET  /api/files?folder=&search=&recursive=&status=  — список объектов
  GET  /api/jobs?limit=                   — последние джобы
  GET  /api/jobs/{id}                     — одна джоба (для опроса прогресса)
  POST /api/upload    {paths:[...], folder:"..."}  — поставить загрузку
  POST /api/download  {folder, file_key, allow_incomplete?}  — поставить скачивание
  POST /api/delete    {folder, file_key}  — поставить удаление из облака
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


# ── Обработчики маршрутов ────────────────────────────────────────────────────


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
    # submit_job — fire-and-forget через loop воркера; id присваивается позже при
    # insert_job. Клиент опрашивает прогресс через GET /api/jobs.
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
    """Чистая маршрутизация: метод+путь → (HTTP-код, JSON) или :class:`FileResponse`.

    Не трогает сокеты — поэтому тестируется напрямую. :class:`ApiError`
    превращается здесь же в ответ ``{"error": ...}`` с её кодом, так что функция
    никогда не бросает на ожидаемых ошибках (только возвращает).
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

    # Живость — без авторизации.
    if method == "GET" and path == "/api/health":
        return 200, {"status": "ok", "service": "tg_bd"}

    # Публичная раздача шар-ссылок (без API-токена — секрет это сам token).
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


# ── HTTP-обвязка ─────────────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    server_version = "TGBD-API/1"
    protocol_version = "HTTP/1.1"
    # За один Range-ответ отдаём не больше этого объёма — иначе открытый
    # `Range: bytes=0-` от плеера разворачивается в `0..size-1` и тянет
    # огромную (иногда весь файл) часть разом. Плеер до-запросит следующие
    # окна по мере проигрывания. См. _serve_stream/_prefetch_next.
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
            # Клиент (FFmpeg/QMediaPlayer) уже закрыл соединение, например при
            # перемотке или закрытии плеера. Это не ошибка — просто выходим.
            return

    def _resolve_range(self, size: int) -> tuple[int, int, bool] | None:
        """Разобрать заголовок ``Range`` против размера ``size``.

        Возвращает ``(start, end, is_range)`` (end включительно) или ``None``,
        если диапазон не удовлетворить (caller обязан ответить 416)."""
        range_header = self.headers.get("Range", "")
        if not range_header.startswith("bytes="):
            return 0, size - 1, False
        try:
            spec = range_header[len("bytes=") :].split(",")[0].strip()
            lo, _, hi = spec.partition("-")
            if lo == "":  # суффиксный диапазон bytes=-N
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
        # Имя файла может содержать кириллицу/эмодзи; HTTP-заголовки кодируются
        # latin-1, поэтому сырое не-ASCII имя роняет send_header (UnicodeEncodeError)
        # и обрывает ответ → плеер не получает ни данных, ни картинки. Даём
        # ASCII-фолбэк + RFC 5987 filename* (UTF-8, percent-encoded).
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
        """Отдать уже собранный файл с поддержкой HTTP Range (стрим/перемотка)."""
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
        """Отдать файл, СОБИРАЯ его из чанков на лету по запрошенному Range —
        скачиваются только перекрытые диапазоном части (инкремент 9/10)."""
        from app.core.stream import iter_range_bytes

        layout = sr.layout
        size = int(layout.total_size)

        resolved = self._resolve_range(size)
        if resolved is None:
            self._send_416(size)
            return
        start, end, is_range = resolved

        # Скачать (или взять из кэша) ТОЛЬКО части, перекрытые диапазоном.
        # Окно стрима: за один Range-ответ отдаём не больше ~_STREAM_WINDOW_BYTES
        # байт. Иначе открытый `Range: bytes=0-` от плеера разворачивается в
        # `0..size-1` и тянет ВЕСЬ файл разом — шквал getFile → FloodWait, а
        # воспроизведение стартует только после полной загрузки. Плеер до-запросит
        # следующие окна по мере проигрывания, а после перемотки пришлёт Range от
        # новой позиции — части качаются от текущей точки таймлайна и до конца.
        # Окно небольшое (не 48+ МБ) намеренно: время до первого байта плееру —
        # это время скачивания частей, покрывающих окно, а не всего окна целиком,
        # так что маленькое окно = быстрый старт воспроизведения. Догрузка
        # следующего окна идёт по мере проигрывания + _prefetch_next греет вперёд.
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
                is_range = True  # ответ стал частичным → 206 + Content-Range
        part_indices = [p.part_index for p in needed]
        # Части бывают огромными (сотни МБ) — без этого плеер ждал бы, пока
        # скачается ВСЯ часть целиком, хотя из неё для текущего окна нужен лишь
        # небольшой отрезок в начале. Работает только для незашифрованных
        # объектов (см. TgDownloader.fetch_parts_decrypted) — для зашифрованных
        # downloader сам проигнорирует подсказку и скачает часть целиком.
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

        # Упреждающая подкачка: пока текущее окно стримится плееру, в фоне греем
        # следующую часть в тот же кэш — к моменту, когда плеер до неё доиграет,
        # она уже на диске, и его Range-запрос не упирается в скачивание (нет паузы).
        if needed:
            self._prefetch_next(sr, layout, needed[-1].part_index)

        # Считаем «скачивание» один раз — на запросе с начала файла.
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
        """Упреждающе скачать СЛЕДУЮЩУЮ часть в фоне, пока текущая стримится, —
        чтобы при достижении её плеером она уже лежала в кэше (без паузы)."""
        import threading

        next_index = last_index + 1
        if next_index >= len(layout.parts):
            return  # последняя часть — впереди греть нечего
        inflight = globals().setdefault("_STREAM_PREFETCH_INFLIGHT", set())
        lock = globals().setdefault("_STREAM_PREFETCH_LOCK", threading.Lock())
        key = (sr.file_key, next_index)
        with lock:
            if key in inflight:
                return
            inflight.add(key)

        def _warm() -> None:
            try:
                # Греем только одно окно вперёд, а не всю (возможно, огромную)
                # следующую часть — тот же смысл, что и prefix_bytes в
                # _serve_stream: докачается остальное по мере проигрывания.
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

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")


class ApiServer:
    """Запускает/останавливает REST API в фоновом демон-потоке."""

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
        """Фактический (host, port) после старта — порт реальный, даже если в
        конфиге был 0 (эфемерный). None, если сервер не запущен."""
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
        """Гарантировать запущенный локальный сервер для просмотра без скачивания.
        Если REST API уже запущен (api.enabled) — переиспользуем его. Иначе
        поднимаем петлевой сервер на 127.0.0.1 с эфемерным портом и случайным
        токеном. Возвращает (base_url, token) или None."""
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
