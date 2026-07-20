from __future__ import annotations

from televault.core.types import PartRecord
from televault.db.database import connect_db
from televault.db.repo import DbRepo


def test_mark_messages_deleted_is_scoped_by_chat_id(tmp_path) -> None:
    repo = DbRepo(connect_db(tmp_path / "index.sqlite3"))

    repo.upsert_msg_part(
        PartRecord(
            msg_id=10,
            chat_id="chat-a",
            folder_path="A/B",
            file_key="ka",
            part_index=0,
            parts_total=1,
            orig_name="a.bin",
            file_size=1,
            caption_raw="",
            date_ts=100,
        )
    )
    repo.upsert_msg_part(
        PartRecord(
            msg_id=11,
            chat_id="chat-b",
            folder_path="A/B",
            file_key="kb",
            part_index=0,
            parts_total=1,
            orig_name="b.bin",
            file_size=1,
            caption_raw="",
            date_ts=101,
        )
    )

    deleted = repo.mark_messages_deleted([10, 11], chat_id="chat-a")
    assert deleted == 1
    assert repo.list_msg_ids("chat-a") == []
    assert repo.list_msg_ids("chat-b") == [11]
