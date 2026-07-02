"""Буфер пакетной записи частей в индекс (общий для multi-part веток).

`chunked_upload` (in-memory) и `_multipart_upload_from_disk` независимо держали
идентичные замыкания `flush_records`/`add_record` с горой `nonlocal`. Здесь это
один объект: копит PartRecord, пишет пачками по `batch_size`, троттлит пересборку
агрегата объекта и сам считает потраченное на БД время.
"""

from __future__ import annotations

import asyncio
import time

from app.core.types import PartRecord
from app.db.repo import DbRepo


class _UploadRecordBuffer:
    def __init__(
        self,
        *,
        repo: DbRepo,
        chat_id: str,
        folder_path: str,
        file_key: str,
        batch_size: int,
        rebuild_throttle_sec: float,
    ) -> None:
        self._repo = repo
        self._chat_id = chat_id
        self._folder_path = folder_path
        self._file_key = file_key
        self._batch_size = max(1, int(batch_size))
        self._rebuild_throttle_sec = float(rebuild_throttle_sec)
        self._pending: list[PartRecord] = []
        self._lock = asyncio.Lock()
        self._last_rebuild_ts = 0.0
        self.db_upsert_seconds = 0.0
        self.db_rebuild_seconds = 0.0

    async def add(self, record: PartRecord) -> None:
        async with self._lock:
            self._pending.append(record)
        await self.flush(force=False)

    async def flush(self, force: bool = False) -> None:
        batch: list[PartRecord] = []
        async with self._lock:
            if force and self._pending:
                batch = list(self._pending)
                self._pending.clear()
            elif len(self._pending) >= self._batch_size:
                batch = self._pending[: self._batch_size]
                del self._pending[: self._batch_size]
        if not batch:
            return
        # DB work runs outside the lock (preserves prior behavior: SQLite serializes).
        db_started = time.monotonic()
        self._repo.upsert_msg_parts_bulk(batch)
        self.db_upsert_seconds += max(0.0, time.monotonic() - db_started)
        now = time.monotonic()
        if force or (now - self._last_rebuild_ts) >= self._rebuild_throttle_sec:
            rebuild_started = time.monotonic()
            self._repo.rebuild_object_aggregate(
                self._chat_id, self._folder_path, self._file_key
            )
            self.db_rebuild_seconds += max(0.0, time.monotonic() - rebuild_started)
            self._last_rebuild_ts = now
