"""Chunked streaming without a full download (increment 9/10 roadmap).

Our file is a set of parts (chunks), each stored as a separate Telegram
message and (optionally) encrypted with AES-GCM. To serve a **range** of the
original file (an HTTP Range request from a player), we need to know which
parts cover it, and download ONLY those — not the whole file.

Key insight: the size of the decrypted (plaintext) part can be derived from
the DB without downloading. An encrypted part stores ``len(payload)`` bytes,
where ``payload = b"ENC1" + nonce(12) + ciphertext+tag``. The overhead is
fixed: ``ENC1``(4) + nonce(12) + GCM tag(16) = :data:`GCM_OVERHEAD` bytes.
So ``plain_size = file_size - GCM_OVERHEAD`` for encrypted parts, and
``plain_size = file_size`` for unencrypted ones (disk slices, ``enc:false``).

This module is pure logic (no network/sockets): building the part layout
with cumulative plaintext offsets, selecting parts for a range, and slicing
bytes out of already-decrypted part files. The downloader does the actual
fetching, and the api-server does the actual serving.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from app.core.types import PartRecord
from app.tg.parser import parse_caption

# ENC1(4) + nonce(12) + GCM tag(16) — the bytes encryption adds to a part
# on top of its plaintext (see app.core.utils.encrypt_bytes).
GCM_OVERHEAD = 32


class LayoutError(ValueError):
    """The layout can't be built (parts are incomplete/have unknown size/are incompatible).

    The caller catches this and falls back to assembling the full file
    (``_ensure_share_file``).
    """


@dataclass(frozen=True)
class PartSlice:
    """A single part with its position within the plaintext file."""

    part_index: int
    msg_id: int
    chat_id: str
    stored_size: int  # bytes stored in telegram (encrypted, if enc)
    enc: bool
    plain_size: int  # bytes in the served (decrypted) file
    plain_start: int  # inclusive offset within the plaintext file
    plain_end: int  # exclusive offset (start + plain_size)


@dataclass(frozen=True)
class StreamLayout:
    """Ordered parts plus the total size of the plaintext file."""

    parts: list[PartSlice]
    total_size: int

    def select_parts(self, start: int, end: int) -> list[PartSlice]:
        """Parts whose plaintext range overlaps ``[start, end]`` (end inclusive)."""
        if end < start:
            return []
        out: list[PartSlice] = []
        for part in self.parts:
            if part.plain_end <= start:
                continue
            if part.plain_start > end:
                break
            out.append(part)
        return out


def _part_is_encrypted(part: PartRecord, caption_prefix: str) -> bool:
    caption = (part.caption_raw or "").strip()
    if not caption:
        return False
    meta = parse_caption(caption, prefix=caption_prefix)
    return bool(meta is not None and meta.enc)


def build_layout(parts: list[PartRecord], *, caption_prefix: str) -> StreamLayout:
    """Build the layout from an object's parts. Raises :class:`LayoutError` if
    the object is incomplete/inconsistent, or a part has an unknown size — in
    which case streaming isn't possible and the caller falls back to a full
    assembly."""
    if not parts:
        raise LayoutError("no parts for object")
    ordered = sorted(parts, key=lambda p: int(p.part_index))

    parts_total = max(int(p.parts_total) for p in ordered)
    if len(ordered) != parts_total:
        raise LayoutError(
            f"object is incomplete: {len(ordered)} of {parts_total} parts"
        )
    if [int(p.part_index) for p in ordered] != list(range(parts_total)):
        raise LayoutError("part indices are not contiguous from 0")

    slices: list[PartSlice] = []
    offset = 0
    for part in ordered:
        if part.file_size is None:
            raise LayoutError(f"part {part.part_index} has unknown size")
        stored = int(part.file_size)
        enc = _part_is_encrypted(part, caption_prefix)
        plain = stored - GCM_OVERHEAD if enc else stored
        if plain < 0:
            raise LayoutError(
                f"part {part.part_index} size {stored} too small for enc overhead"
            )
        slices.append(
            PartSlice(
                part_index=int(part.part_index),
                msg_id=int(part.msg_id),
                chat_id=str(part.chat_id),
                stored_size=stored,
                enc=enc,
                plain_size=plain,
                plain_start=offset,
                plain_end=offset + plain,
            )
        )
        offset += plain
    return StreamLayout(parts=slices, total_size=offset)


def iter_range_bytes(
    layout: StreamLayout,
    part_paths: dict[int, str],
    start: int,
    end: int,
    *,
    buffer_size: int = 256 * 1024,
) -> Iterator[bytes]:
    """Yield plaintext bytes ``[start, end]`` (inclusive) from the decrypted
    part files in ``part_paths`` (part_index → path). Raises
    ``FileNotFoundError`` if a needed part hasn't been downloaded."""
    if end < start:
        return
    for part in layout.select_parts(start, end):
        path = part_paths.get(part.part_index)
        if not path:
            raise FileNotFoundError(f"decrypted part {part.part_index} not available")
        local_lo = max(start, part.plain_start) - part.plain_start
        local_hi = min(end, part.plain_end - 1) - part.plain_start  # inclusive
        remaining = local_hi - local_lo + 1
        if remaining <= 0:
            continue
        with open(path, "rb") as handle:
            handle.seek(local_lo)
            while remaining > 0:
                chunk = handle.read(min(buffer_size, remaining))
                if not chunk:
                    break
                yield chunk
                remaining -= len(chunk)
