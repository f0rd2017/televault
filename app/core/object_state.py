"""Classifies an object's (file's) state based on its parts and the live accounts.

Extends the stored ``complete``/``incomplete`` status with two more states
that depend on which accounts/chats are currently connected:

- ``complete``   — all parts are present and their chats are reachable;
- ``incomplete`` — some part(s) were never fully uploaded (missing from the index);
- ``offline``    — every part is present in the index, but at least one part's
  chat isn't served by any connected account → transient, "wait for an account";
- ``damaged``    — a part has ``lost_ts`` set AND its chat is connected → the
  message is really gone, "delete or re-upload".

A pure function (no DB access), easy to test.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.core.types import PartRecord

STATE_COMPLETE = "complete"
STATE_INCOMPLETE = "incomplete"
STATE_OFFLINE = "offline"
STATE_DAMAGED = "damaged"


def classify_object_state(
    parts: list[PartRecord],
    *,
    parts_total: int,
    connected_chat_ids: Iterable[str],
) -> str:
    """Return one of ``complete|incomplete|offline|damaged``.

    ``parts`` — the non-deleted part rows (as from ``get_parts_for_object``).
    ``connected_chat_ids`` — chat_id values of the currently connected accounts.
    """
    connected = {str(c).strip() for c in connected_chat_ids if str(c or "").strip()}
    have_indices = {int(p.part_index) for p in parts}

    # damaged: a part is marked as lost, but its account is reachable — meaning
    # the message really is gone (not just that the account is disconnected).
    for part in parts:
        if part.lost_ts and str(part.chat_id).strip() in connected:
            return STATE_DAMAGED

    # offline: at least one existing part's chat isn't served by a live account.
    # (If no accounts were passed in at all — skip this check, since we don't
    # know the network state.)
    if connected:
        for part in parts:
            if str(part.chat_id).strip() not in connected:
                return STATE_OFFLINE

    if len(have_indices) < int(parts_total):
        return STATE_INCOMPLETE
    return STATE_COMPLETE


def display_state(
    *,
    stored_status: str,
    part_chat_ids: Iterable[str],
    has_lost_part: bool,
    connected_chat_ids: Iterable[str],
) -> str:
    """A lightweight variant for the grid: derives offline/damaged from aggregates.

    Doesn't need the full list of ``PartRecord``s — just the set of part chat_ids,
    a "has a lost part" flag, and the stored status. Overlays damaged/offline on
    top of ``complete``/``incomplete``.
    """
    connected = {str(c).strip() for c in connected_chat_ids if str(c or "").strip()}
    chat_ids = {str(c).strip() for c in part_chat_ids if str(c or "").strip()}
    if has_lost_part and any(c in connected for c in chat_ids):
        return STATE_DAMAGED
    if connected and chat_ids and any(c not in connected for c in chat_ids):
        return STATE_OFFLINE
    return str(stored_status or STATE_INCOMPLETE)
