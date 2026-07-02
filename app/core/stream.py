"""Поблочный стрим без полного скачивания (инкремент 9/10 roadmap).

Наш файл — это набор частей (чанков), каждая лежит отдельным telegram-сообщением
и (опционально) зашифрована AES-GCM. Чтобы отдать **диапазон** исходного файла
(HTTP Range от плеера), нужно знать, какие части его покрывают, и скачать ТОЛЬКО
их — а не весь файл.

Ключевое наблюдение: размер расшифрованной (plaintext) части выводится из БД без
скачивания. Зашифрованная часть хранит ``len(payload)`` байт, где
``payload = b"ENC1" + nonce(12) + ciphertext+tag``. Накладные расходы фиксированы:
``ENC1``(4) + nonce(12) + GCM tag(16) = :data:`GCM_OVERHEAD` байт. Значит
``plain_size = file_size - GCM_OVERHEAD`` для зашифрованных частей и
``plain_size = file_size`` для незашифрованных (диск-слайсы, ``enc:false``).

Этот модуль — чистая логика (без сети/сокетов): построение раскладки частей с
кумулятивными plaintext-смещениями, выбор частей по диапазону и нарезка байтов из
уже расшифрованных part-файлов. Скачивание делает downloader, раздачу — api-сервер.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from app.core.types import PartRecord
from app.tg.parser import parse_caption

# ENC1(4) + nonce(12) + GCM tag(16) — байты, которые шифрование добавляет к части
# поверх её plaintext (см. app.core.utils.encrypt_bytes).
GCM_OVERHEAD = 32


class LayoutError(ValueError):
    """Раскладку построить нельзя (части неполны/неизвестен размер/несовместимы).

    Вызывающий ловит и откатывается на полную сборку файла (``_ensure_share_file``).
    """


@dataclass(frozen=True)
class PartSlice:
    """Одна часть с её положением в plaintext-файле."""

    part_index: int
    msg_id: int
    chat_id: str
    stored_size: int  # байт хранится в telegram (зашифровано, если enc)
    enc: bool
    plain_size: int  # байт в отдаваемом (расшифрованном) файле
    plain_start: int  # включительное смещение в plaintext-файле
    plain_end: int  # исключающее смещение (start + plain_size)


@dataclass(frozen=True)
class StreamLayout:
    """Упорядоченные части + полный размер plaintext-файла."""

    parts: list[PartSlice]
    total_size: int

    def select_parts(self, start: int, end: int) -> list[PartSlice]:
        """Части, чей plaintext-диапазон пересекает ``[start, end]`` (end включительно)."""
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
    """Построить раскладку из частей объекта. Бросает :class:`LayoutError`, если
    объект неполон/несвязен или у части неизвестен размер — тогда стрим невозможен
    и вызывающий откатывается на полную сборку."""
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
    """Выдавать plaintext-байты ``[start, end]`` (включительно) из расшифрованных
    part-файлов ``part_paths`` (part_index → путь). Бросает ``FileNotFoundError``,
    если нужная часть не скачана."""
    if end < start:
        return
    for part in layout.select_parts(start, end):
        path = part_paths.get(part.part_index)
        if not path:
            raise FileNotFoundError(f"decrypted part {part.part_index} not available")
        local_lo = max(start, part.plain_start) - part.plain_start
        local_hi = min(end, part.plain_end - 1) - part.plain_start  # включительно
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
