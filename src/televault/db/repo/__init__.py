from __future__ import annotations

import sqlite3

from televault.db.database import init_schema
from televault.db.repo._batch import _BatchMixin
from televault.db.repo._index import _IndexMixin
from televault.db.repo._jobs import _JobsMixin
from televault.db.repo._objects import _ObjectsMixin
from televault.db.repo._tail import _TailMixin
from televault.db.repo._trash import _TrashShareSyncMixin


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
