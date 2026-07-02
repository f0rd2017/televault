from __future__ import annotations

import sqlite3

from app.db.database import init_schema
from app.db.repo._batch import _BatchMixin
from app.db.repo._index import _IndexMixin
from app.db.repo._jobs import _JobsMixin
from app.db.repo._objects import _ObjectsMixin
from app.db.repo._tail import _TailMixin
from app.db.repo._trash import _TrashShareSyncMixin


class DbRepo(
    _IndexMixin,
    _ObjectsMixin,
    _TrashShareSyncMixin,
    _JobsMixin,
    _BatchMixin,
    _TailMixin,
):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def init_schema(self) -> None:
        init_schema(self.conn)
