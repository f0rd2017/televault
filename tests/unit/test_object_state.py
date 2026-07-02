from __future__ import annotations

from app.core.object_state import (
    STATE_COMPLETE,
    STATE_DAMAGED,
    STATE_INCOMPLETE,
    STATE_OFFLINE,
    classify_object_state,
    display_state,
)
from app.core.types import PartRecord


def _part(part_index: int, *, chat_id: str, lost_ts: int | None = None) -> PartRecord:
    return PartRecord(
        msg_id=100 + part_index,
        chat_id=chat_id,
        folder_path="/f",
        file_key="k",
        part_index=part_index,
        parts_total=2,
        orig_name="movie.bin",
        file_size=10,
        caption_raw="",
        date_ts=1,
        lost_ts=lost_ts,
    )


def test_complete_when_all_parts_present_and_chats_connected():
    parts = [_part(0, chat_id="-100a"), _part(1, chat_id="-100a")]
    assert (
        classify_object_state(parts, parts_total=2, connected_chat_ids={"-100a"})
        == STATE_COMPLETE
    )


def test_incomplete_when_part_missing_from_index():
    parts = [_part(0, chat_id="-100a")]
    assert (
        classify_object_state(parts, parts_total=2, connected_chat_ids={"-100a"})
        == STATE_INCOMPLETE
    )


def test_offline_when_part_chat_not_connected():
    parts = [_part(0, chat_id="-100a"), _part(1, chat_id="-100b")]
    # Account hosting chat -100b is not connected this session.
    assert (
        classify_object_state(parts, parts_total=2, connected_chat_ids={"-100a"})
        == STATE_OFFLINE
    )


def test_damaged_when_lost_part_chat_is_connected():
    parts = [_part(0, chat_id="-100a"), _part(1, chat_id="-100a", lost_ts=123)]
    # Account is online but the message is gone => permanent damage.
    assert (
        classify_object_state(parts, parts_total=2, connected_chat_ids={"-100a"})
        == STATE_DAMAGED
    )


def test_lost_part_on_offline_account_is_offline_not_damaged():
    parts = [_part(0, chat_id="-100a"), _part(1, chat_id="-100b", lost_ts=123)]
    # Lost flag but its account is offline => can't conclude damage; show offline.
    assert (
        classify_object_state(parts, parts_total=2, connected_chat_ids={"-100a"})
        == STATE_OFFLINE
    )


def test_no_connected_accounts_skips_offline_check():
    parts = [_part(0, chat_id="-100a"), _part(1, chat_id="-100b")]
    # Unknown network state => fall back to count-based status.
    assert (
        classify_object_state(parts, parts_total=2, connected_chat_ids=set())
        == STATE_COMPLETE
    )


# --- display_state (lightweight grid overlay) --------------------------------


def test_display_state_keeps_stored_status_when_all_online():
    assert (
        display_state(
            stored_status="complete",
            part_chat_ids={"-100a"},
            has_lost_part=False,
            connected_chat_ids={"-100a"},
        )
        == STATE_COMPLETE
    )


def test_display_state_offline_when_chat_not_connected():
    assert (
        display_state(
            stored_status="complete",
            part_chat_ids={"-100a", "-100b"},
            has_lost_part=False,
            connected_chat_ids={"-100a"},
        )
        == STATE_OFFLINE
    )


def test_display_state_damaged_overrides_when_lost_and_online():
    assert (
        display_state(
            stored_status="complete",
            part_chat_ids={"-100a"},
            has_lost_part=True,
            connected_chat_ids={"-100a"},
        )
        == STATE_DAMAGED
    )


def test_display_state_passthrough_without_connections():
    assert (
        display_state(
            stored_status="incomplete",
            part_chat_ids={"-100a"},
            has_lost_part=False,
            connected_chat_ids=set(),
        )
        == STATE_INCOMPLETE
    )
