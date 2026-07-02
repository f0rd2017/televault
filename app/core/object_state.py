"""Классификация состояния объекта (файла) по его частям и живым аккаунтам.

Расширяет хранимый статус ``complete``/``incomplete`` двумя состояниями,
которые зависят от того, какие аккаунты/чаты сейчас подключены:

- ``complete``   — все части на месте и их чаты доступны;
- ``incomplete`` — часть(и) так и не дозалиты (нет в индексе);
- ``offline``    — все части в индексе есть, но чат хотя бы одной части не
  обслуживается ни одним подключённым аккаунтом → транзиентно, «ждать аккаунт»;
- ``damaged``    — у части стоит ``lost_ts`` И её чат подключён → сообщение
  реально пропало, «удалить или перезалить».

Чистая функция (без БД), легко тестируется.
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
    """Вернуть одно из ``complete|incomplete|offline|damaged``.

    ``parts`` — НЕ удалённые строки части (как из ``get_parts_for_object``).
    ``connected_chat_ids`` — chat_id подключённых сейчас аккаунтов.
    """
    connected = {str(c).strip() for c in connected_chat_ids if str(c or "").strip()}
    have_indices = {int(p.part_index) for p in parts}

    # damaged: часть помечена потерянной, но её аккаунт доступен — значит сообщение
    # действительно пропало (а не просто аккаунт отключён).
    for part in parts:
        if part.lost_ts and str(part.chat_id).strip() in connected:
            return STATE_DAMAGED

    # offline: чат хотя бы одной имеющейся части не обслуживается живым аккаунтом.
    # (Если аккаунтов не передали вовсе — пропускаем эту проверку, не зная сети.)
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
    """Лёгкий вариант для сетки: считает offline/damaged из агрегатов.

    Не требует полного списка ``PartRecord`` — достаточно набора chat_id частей,
    флага «есть потерянная часть» и сохранённого статуса. Накладывает
    damaged/offline поверх ``complete``/``incomplete``.
    """
    connected = {str(c).strip() for c in connected_chat_ids if str(c or "").strip()}
    chat_ids = {str(c).strip() for c in part_chat_ids if str(c or "").strip()}
    if has_lost_part and any(c in connected for c in chat_ids):
        return STATE_DAMAGED
    if connected and chat_ids and any(c not in connected for c in chat_ids):
        return STATE_OFFLINE
    return str(stored_status or STATE_INCOMPLETE)
