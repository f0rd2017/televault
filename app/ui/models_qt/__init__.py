from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import time

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractListModel,
    QModelIndex,
    QSize,
    Qt,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QStyle

from app.core.types import ObjectEntry
from app.core.utils import to_human_size


from app.ui.models_qt._icons import (
    _build_file_icon_with_badge,
    _build_folder_icon,
    _build_typed_file_icon,
    _file_extension_token,
    is_image_name,
    is_pdf_name as is_pdf_name,
    is_text_editable_name as is_text_editable_name,
    is_video_name,
    make_thumbnail_icon,
)


@dataclass(frozen=True)
class ExplorerFolderItem:
    name: str
    path: str


@dataclass(frozen=True)
class ExplorerFileItem:
    entry: ObjectEntry
    local_path: str | None = None
    local_exists: bool = False
    # Состояние с учётом живых аккаунтов: complete/incomplete/offline/damaged.
    display_state: str | None = None
    # Пользовательская заметка (минипометка).
    note: str = ""
    # Миниатюра для картинок (если уже построена); иначе типовая иконка.
    thumbnail: QIcon | None = None


# Авто-подпись (минипометка) под именем для проблемных состояний.
_DISPLAY_STATE_LABELS = {
    "incomplete": "⚠ не дозалит",
    "offline": "☁ аккаунт оффлайн",
    "damaged": "✖ повреждён",
}


@dataclass
class FolderNode:
    name: str
    path: str
    parent: "FolderNode | None" = None
    children: list["FolderNode"] = field(default_factory=list)

    def child_by_name(self, name: str) -> "FolderNode | None":
        for child in self.children:
            if child.name == name:
                return child
        return None


class FolderTreeModel(QAbstractItemModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._root = FolderNode(name="", path="")
        self._folder_icon = _build_folder_icon(22)
        self._folder_signature: tuple[str, ...] = ()

    def set_folders(self, folders: list[str]) -> None:
        # Структура папок при скачивании/выгрузке файлов не меняется, а
        # reload_all() дёргает set_folders на каждом завершении джобы. Полный
        # beginResetModel схлопывает дерево и сбрасывает выделение → визуально
        # папки слева «прыгают» (мигают). Если набор папок тот же — выходим без
        # сброса модели, сохраняя раскрытие/выделение.
        signature = tuple(sorted(set(folders), key=lambda x: x.lower()))
        if signature == self._folder_signature:
            return
        self._folder_signature = signature

        self.beginResetModel()
        self._root = FolderNode(name="", path="")

        for folder in signature:
            node = self._root
            current = ""
            for part in folder.split("/"):
                current = part if not current else f"{current}/{part}"
                child = node.child_by_name(part)
                if child is None:
                    child = FolderNode(name=part, path=current, parent=node)
                    node.children.append(child)
                node = child

        self.endResetModel()

    def find_index_by_path(self, folder_path: str | None) -> QModelIndex:
        if not folder_path:
            return QModelIndex()

        node = self._root
        index = QModelIndex()
        for part in folder_path.split("/"):
            found_row = -1
            found_node: FolderNode | None = None
            for row, child in enumerate(node.children):
                if child.name == part:
                    found_row = row
                    found_node = child
                    break
            if found_row < 0 or found_node is None:
                return QModelIndex()
            index = self.createIndex(found_row, 0, found_node)
            node = found_node
        return index

    def path_from_index(self, index: QModelIndex) -> str | None:
        if not index.isValid():
            return None
        node: FolderNode = index.internalPointer()
        return node.path or None

    def index(
        self, row: int, column: int, parent: QModelIndex = QModelIndex()
    ) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        parent_node = self._node_for_index(parent)
        try:
            child = parent_node.children[row]
        except IndexError:
            return QModelIndex()
        return self.createIndex(row, column, child)

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()

        node: FolderNode = index.internalPointer()
        parent = node.parent
        if parent is None or parent is self._root:
            return QModelIndex()

        grand = parent.parent if parent.parent is not None else self._root
        row = grand.children.index(parent)
        return self.createIndex(row, 0, parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        node = self._node_for_index(parent)
        return len(node.children)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        node: FolderNode = index.internalPointer()
        if role == Qt.ItemDataRole.DisplayRole:
            return node.name
        if role == Qt.ItemDataRole.DecorationRole:
            return self._folder_icon
        if role == Qt.ItemDataRole.SizeHintRole:
            return QSize(0, 34)
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def _node_for_index(self, index: QModelIndex) -> FolderNode:
        if index.isValid():
            return index.internalPointer()
        return self._root


class ExplorerGridModel(QAbstractListModel):
    def __init__(self, parent=None, thumb_cache_dir: str | None = None) -> None:
        super().__init__(parent)
        self._items: list[ExplorerFolderItem | ExplorerFileItem] = []
        self._file_rows: list[int] = []
        self._object_rows: dict[tuple[str, str], list[int]] = {}
        self._local_presence_cursor = 0

        style = QApplication.style()
        self._file_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self._incomplete_icon = style.standardIcon(
            QStyle.StandardPixmap.SP_MessageBoxWarning
        )
        self._folder_icon = _build_folder_icon(58)
        self._badged_file_icons: dict[tuple[str, str, str, bool, int], QIcon] = {}
        self._recent_exports: dict[tuple[str, str], float] = {}
        self._transfer_states: dict[tuple[str, str], str] = {}
        self._loading_phase = 0

        # Размер иконок: 58пк по умолчанию, может быть изменён через set_icon_size()
        self._icon_size = 58

        # Превью картинок: размер, дисковый кэш и in-memory поверх него, курсор
        # ленивого обхода (как у local-presence). thumb_cache_dir может быть None
        # в тестах/без конфига — тогда только in-memory.
        self._thumb_size = 58
        self._thumb_cache_dir = thumb_cache_dir
        self._thumb_mem: dict[str, QIcon] = {}
        self._thumb_cursor = 0

    def set_icon_size(self, size: int) -> None:
        """Update icon/grid size and trigger a full model refresh."""
        size = max(32, min(256, int(size)))
        if self._icon_size == size:
            return
        self._icon_size = size
        self._thumb_size = size
        self._folder_icon = _build_folder_icon(size)
        # Invalidate the badged-icon cache — sizes are baked in
        self._badged_file_icons.clear()
        self._thumb_mem.clear()
        self.beginResetModel()
        self.endResetModel()

    def set_items(self, items: list[ExplorerFolderItem | ExplorerFileItem]) -> None:
        self.beginResetModel()
        self._items = items
        self._rebuild_row_index()
        self.endResetModel()

    def refresh_local_presence(self) -> bool:
        return self._refresh_local_presence_rows(self._file_rows)

    def refresh_local_presence_step(self, max_items: int = 24) -> bool:
        if not self._file_rows:
            self._local_presence_cursor = 0
            return False

        limit = max(1, int(max_items))
        total = len(self._file_rows)
        scan_count = min(limit, total)

        rows: list[int] = []
        for _ in range(scan_count):
            pos = self._local_presence_cursor % total
            rows.append(self._file_rows[pos])
            self._local_presence_cursor = (self._local_presence_cursor + 1) % total

        return self._refresh_local_presence_rows(rows)

    def refresh_local_presence_for_object(
        self, folder_path: str, file_key: str
    ) -> bool:
        rows = self._object_rows.get((folder_path, file_key)) or []
        if not rows:
            return False
        return self._refresh_local_presence_rows(rows)

    def _refresh_local_presence_rows(self, rows: list[int]) -> bool:
        changed_rows: list[int] = []
        for row in sorted(set(rows)):
            if row < 0 or row >= len(self._items):
                continue
            item = self._items[row]
            if not isinstance(item, ExplorerFileItem):
                continue

            local_exists = self._check_local_exists(item.local_path)

            if local_exists == item.local_exists:
                continue

            self._items[row] = ExplorerFileItem(
                entry=item.entry,
                local_path=item.local_path,
                local_exists=local_exists,
                display_state=item.display_state,
                note=item.note,
                thumbnail=item.thumbnail,
            )
            changed_rows.append(row)

        if not changed_rows:
            return False

        roles = [
            Qt.ItemDataRole.DecorationRole,
            Qt.ItemDataRole.ToolTipRole,
            Qt.ItemDataRole.ForegroundRole,
            Qt.ItemDataRole.UserRole,
        ]
        self._emit_data_changed_rows(changed_rows, roles)
        return True

    @staticmethod
    def _check_local_exists(local_path: str | None) -> bool:
        if not local_path:
            return False
        try:
            return Path(local_path).exists()
        except Exception:
            return False

    # ── Превью картинок ──────────────────────────────────────────────────
    def _thumb_key(self, entry: ObjectEntry) -> str:
        import hashlib

        raw = f"{entry.file_key}|{int(entry.total_size or 0)}|{self._thumb_size}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()  # noqa: S324

    def _thumb_disk_path(self, entry: ObjectEntry) -> Path | None:
        if not self._thumb_cache_dir:
            return None
        return Path(self._thumb_cache_dir) / f"{self._thumb_key(entry)}.png"

    def _load_or_build_thumbnail(self, item: ExplorerFileItem) -> QIcon | None:
        """Достать миниатюру: память → диск → построить из локального файла.
        Возвращает None, если построить не удалось (фолбэк на типовую иконку)."""
        entry = item.entry
        key = self._thumb_key(entry)
        cached = self._thumb_mem.get(key)
        if cached is not None:
            return cached

        disk = self._thumb_disk_path(entry)
        if disk is not None and disk.exists():
            pix = QPixmap(str(disk))
            if not pix.isNull():
                icon = QIcon(pix)
                self._thumb_mem[key] = icon
                return icon

        if not item.local_path or not self._check_local_exists(item.local_path):
            return None
        # Видео-постер строится в фоне через ffmpeg (см. video_rows_needing_poster),
        # синхронно здесь — только дешёвый декод картинок.
        if not is_image_name(entry.orig_name):
            return None
        icon = make_thumbnail_icon(item.local_path, self._thumb_size)
        if icon is None:
            return None
        self._thumb_mem[key] = icon
        # Сохраняем на диск (best-effort), чтобы пережить рестарт.
        if disk is not None:
            try:
                disk.parent.mkdir(parents=True, exist_ok=True)
                pm = icon.pixmap(self._thumb_size, self._thumb_size)
                if not pm.isNull():
                    pm.save(str(disk), "PNG")
            except Exception:
                pass
        return icon

    def set_thumbnail_from_path(
        self, folder_path: str, file_key: str, image_path: str
    ) -> bool:
        """Построить миниатюру из произвольного файла (напр. временно скачанного
        для нескачанной картинки) и проставить её всем строкам объекта."""
        rows = self._object_rows.get((folder_path, file_key)) or []
        if not rows:
            return False
        icon = make_thumbnail_icon(image_path, self._thumb_size)
        if icon is None:
            return False
        applied = False
        for row in rows:
            if 0 <= row < len(self._items):
                item = self._items[row]
                if isinstance(item, ExplorerFileItem):
                    key = self._thumb_key(item.entry)
                    self._thumb_mem[key] = icon
                    disk = self._thumb_disk_path(item.entry)
                    if disk is not None:
                        try:
                            disk.parent.mkdir(parents=True, exist_ok=True)
                            pm = icon.pixmap(self._thumb_size, self._thumb_size)
                            if not pm.isNull():
                                pm.save(str(disk), "PNG")
                        except Exception:
                            pass
                    self._apply_thumbnail(row, icon)
                    applied = True
        return applied

    def _apply_thumbnail(self, row: int, icon: QIcon) -> None:
        item = self._items[row]
        if not isinstance(item, ExplorerFileItem):
            return
        self._items[row] = ExplorerFileItem(
            entry=item.entry,
            local_path=item.local_path,
            local_exists=item.local_exists,
            display_state=item.display_state,
            note=item.note,
            thumbnail=icon,
        )
        self._emit_data_changed_rows([row], [Qt.ItemDataRole.DecorationRole])

    def image_rows_needing_fetch(self, max_items: int = 8) -> list[ExplorerFileItem]:
        """Картинки без миниатюры, которых нет локально и нет в кэше — кандидаты
        на фоновую дозагрузку ради превью (инкремент 1b)."""
        out: list[ExplorerFileItem] = []
        for row in self._file_rows:
            if row < 0 or row >= len(self._items):
                continue
            item = self._items[row]
            if not isinstance(item, ExplorerFileItem):
                continue
            if item.thumbnail is not None or item.local_exists:
                continue
            if not is_image_name(item.entry.orig_name):
                continue
            disk = self._thumb_disk_path(item.entry)
            if self._thumb_mem.get(self._thumb_key(item.entry)) is not None:
                continue
            if disk is not None and disk.exists():
                continue
            out.append(item)
            if len(out) >= max(1, int(max_items)):
                break
        return out

    def video_rows_needing_poster(self, max_items: int = 4) -> list[ExplorerFileItem]:
        """Локальные видео без постера и без кэша — кандидаты на фоновое
        построение кадра через ffmpeg (инкремент 4). В отличие от картинок,
        строим только из СКАЧАННЫХ файлов (тянуть видео ради кадра дорого)."""
        out: list[ExplorerFileItem] = []
        for row in self._file_rows:
            if row < 0 or row >= len(self._items):
                continue
            item = self._items[row]
            if not isinstance(item, ExplorerFileItem):
                continue
            if item.thumbnail is not None or not item.local_exists:
                continue
            if not is_video_name(item.entry.orig_name):
                continue
            if self._thumb_mem.get(self._thumb_key(item.entry)) is not None:
                continue
            disk = self._thumb_disk_path(item.entry)
            if disk is not None and disk.exists():
                continue
            out.append(item)
            if len(out) >= max(1, int(max_items)):
                break
        return out

    def video_rows_needing_remote_poster(
        self, max_items: int = 2
    ) -> list[ExplorerFileItem]:
        """Видео БЕЗ постера, которых нет локально и нет в кэше — кандидаты на
        фоновое построение кадра по ПРЕФИКСУ (тянем только первую часть).
        В отличие от video_rows_needing_poster — именно для НЕскачанных файлов."""
        out: list[ExplorerFileItem] = []
        for row in self._file_rows:
            if row < 0 or row >= len(self._items):
                continue
            item = self._items[row]
            if not isinstance(item, ExplorerFileItem):
                continue
            if item.thumbnail is not None or item.local_exists:
                continue
            if not is_video_name(item.entry.orig_name):
                continue
            if self._thumb_mem.get(self._thumb_key(item.entry)) is not None:
                continue
            disk = self._thumb_disk_path(item.entry)
            if disk is not None and disk.exists():
                continue
            out.append(item)
            if len(out) >= max(1, int(max_items)):
                break
        return out

    def refresh_thumbnails_step(self, max_items: int = 12) -> bool:
        """Ленивый шаг построения миниатюр для видимых картинок (зеркало
        refresh_local_presence_step). Строит только из локальных файлов/кэша."""
        if not self._file_rows:
            self._thumb_cursor = 0
            return False
        total = len(self._file_rows)
        scan = min(max(1, int(max_items)), total)
        changed = False
        for _ in range(scan):
            pos = self._thumb_cursor % total
            self._thumb_cursor = (self._thumb_cursor + 1) % total
            row = self._file_rows[pos]
            if row < 0 or row >= len(self._items):
                continue
            item = self._items[row]
            if not isinstance(item, ExplorerFileItem):
                continue
            if item.thumbnail is not None:
                continue
            name = item.entry.orig_name
            if not (is_image_name(name) or is_video_name(name)):
                continue
            icon = self._load_or_build_thumbnail(item)
            if icon is not None:
                self._apply_thumbnail(row, icon)
                changed = True
        return changed

    def item_for_index(
        self, index: QModelIndex
    ) -> ExplorerFolderItem | ExplorerFileItem | None:
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self._items):
            return None
        return self._items[row]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._items)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        item = self.item_for_index(index)
        if item is None:
            return None

        if isinstance(item, ExplorerFolderItem):
            if role == Qt.ItemDataRole.DisplayRole:
                return item.name
            if role == Qt.ItemDataRole.DecorationRole:
                return self._folder_icon
            if role == Qt.ItemDataRole.ToolTipRole:
                return f"Folder: {item.path}"
            if role == Qt.ItemDataRole.ForegroundRole:
                return QBrush(QColor("#d7c5ff"))
            if role == Qt.ItemDataRole.UserRole:
                return item
            if role == Qt.ItemDataRole.UserRole + 1:
                return "folder"
            if role == Qt.ItemDataRole.SizeHintRole:
                sz = self._icon_size + 44
                return QSize(sz, self._icon_size + 54)
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            return None

        entry = item.entry
        recently_exported = self._is_recent_export(entry.folder_path, entry.file_key)
        transfer_state = self._transfer_state_for(entry.folder_path, entry.file_key)
        if transfer_state == "downloading":
            badge_kind = "loading"
        elif item.local_exists:
            badge_kind = "downloaded"
        else:
            badge_kind = "not_downloaded"

        state = item.display_state or entry.status
        auto_label = _DISPLAY_STATE_LABELS.get(state, "")
        minimark = (item.note or "").strip() or auto_label

        if role == Qt.ItemDataRole.DisplayRole:
            return f"{entry.orig_name}\n{minimark}" if minimark else entry.orig_name

        if role == Qt.ItemDataRole.DecorationRole:
            if item.thumbnail is not None:
                return item.thumbnail
            return self._file_icon_for_state(
                state,
                file_name=entry.orig_name,
                badge_kind=badge_kind,
                recently_exported=recently_exported,
                loading_phase=self._loading_phase,
            )

        if role == Qt.ItemDataRole.ToolTipRole:
            progress = 0
            if entry.parts_total > 0:
                progress = int((entry.have_parts / entry.parts_total) * 100)
            note_line = f"Заметка: {item.note}\n" if (item.note or "").strip() else ""
            return (
                f"Name: {entry.orig_name}\n"
                f"Folder: {entry.folder_path}\n"
                f"Status: {state}\n"
                f"{note_line}"
                f"Parts: {entry.have_parts}/{entry.parts_total}\n"
                f"Progress: {max(0, min(100, progress))}%\n"
                f"Size: {to_human_size(entry.total_size)}\n"
                f"Last seen: {datetime.fromtimestamp(entry.last_seen_ts).strftime('%Y-%m-%d %H:%M')}\n"
                f"Cached locally: {'yes' if item.local_exists else 'no'}\n"
                f"Transfer: {transfer_state or 'idle'}\n"
                f"Recently exported: {'yes' if recently_exported else 'no'}\n"
                f"Local path: {item.local_path or '-'}"
            )

        if role == Qt.ItemDataRole.SizeHintRole:
            sz = self._icon_size + 44
            return QSize(sz, self._icon_size + 54)

        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

        if role == Qt.ItemDataRole.ForegroundRole:
            if transfer_state == "downloading":
                return QBrush(QColor("#dfcbff"))
            if recently_exported:
                return QBrush(QColor("#ffe7ff"))
            if state == "damaged":
                return QBrush(QColor("#ff7b72"))
            if state == "offline":
                return QBrush(QColor("#8fb8ff"))
            if item.local_exists:
                return QBrush(
                    QColor("#f5f7fa") if state == "complete" else QColor("#ffcf66")
                )
            return QBrush(
                QColor("#c8d1dc") if state == "complete" else QColor("#ffcf66")
            )

        if role == Qt.ItemDataRole.BackgroundRole:
            if transfer_state == "downloading":
                return QBrush(QColor(126, 85, 255, 44))
            if recently_exported:
                return QBrush(QColor(201, 87, 255, 38))
            return None

        if role == Qt.ItemDataRole.UserRole:
            return item

        if role == Qt.ItemDataRole.UserRole + 1:
            return "file"

        return None

    def _file_icon_for_state(
        self,
        status: str,
        file_name: str,
        badge_kind: str,
        recently_exported: bool = False,
        loading_phase: int = 0,
    ) -> QIcon:
        extension_token = _file_extension_token(file_name)
        phase_key = int(loading_phase) % 8 if badge_kind == "loading" else 0
        key = (status, extension_token, badge_kind, recently_exported, phase_key)
        cached = self._badged_file_icons.get(key)
        if cached is not None:
            return cached

        base_icon = _build_typed_file_icon(file_name=file_name, status=status, size=58)
        icon = _build_file_icon_with_badge(
            base_icon,
            badge_kind=badge_kind,
            recently_exported=recently_exported,
            loading_phase=loading_phase,
            size=58,
        )
        self._badged_file_icons[key] = icon
        return icon

    def mark_recent_export(
        self, folder_path: str, file_key: str, ttl_sec: float = 3.0
    ) -> None:
        ttl = max(0.5, float(ttl_sec))
        key = (folder_path, file_key)
        self._recent_exports[key] = time.monotonic() + ttl
        self._emit_changed_for_object(folder_path, file_key)

    def cleanup_recent_export_marks(self) -> bool:
        if not self._recent_exports:
            return False

        now = time.monotonic()
        expired = [
            key for key, expire_at in self._recent_exports.items() if expire_at <= now
        ]
        if not expired:
            return False

        for key in expired:
            self._recent_exports.pop(key, None)
        self._emit_changed_for_keys(expired)
        return True

    def set_transfer_state(
        self, folder_path: str, file_key: str, state: str | None
    ) -> None:
        key = (folder_path, file_key)
        normalized_state = (state or "").strip().lower()

        changed = False
        if not normalized_state:
            if key in self._transfer_states:
                self._transfer_states.pop(key, None)
                changed = True
        else:
            prev = self._transfer_states.get(key)
            if prev != normalized_state:
                self._transfer_states[key] = normalized_state
                changed = True

        if changed:
            self._emit_changed_for_object(folder_path, file_key)

    def has_active_loading_transfers(self) -> bool:
        for key, state in self._transfer_states.items():
            if state == "downloading" and self._object_rows.get(key):
                return True
        return False

    def advance_loading_animation(self) -> bool:
        loading_rows: set[int] = set()
        for key, state in self._transfer_states.items():
            if state != "downloading":
                continue
            rows = self._object_rows.get(key)
            if rows:
                loading_rows.update(rows)

        if not loading_rows:
            return False

        self._loading_phase = (self._loading_phase + 1) % 8
        self._emit_data_changed_rows(
            list(loading_rows),
            [Qt.ItemDataRole.DecorationRole],
        )
        return True

    def _is_recent_export(self, folder_path: str, file_key: str) -> bool:
        key = (folder_path, file_key)
        expire_at = self._recent_exports.get(key)
        if expire_at is None:
            return False
        if expire_at > time.monotonic():
            return True
        self._recent_exports.pop(key, None)
        return False

    def _transfer_state_for(self, folder_path: str, file_key: str) -> str | None:
        return self._transfer_states.get((folder_path, file_key))

    def _emit_changed_for_object(self, folder_path: str, file_key: str) -> None:
        self._emit_changed_for_keys([(folder_path, file_key)])

    def _emit_changed_for_keys(self, keys: list[tuple[str, str]]) -> None:
        if not keys:
            return

        roles = [
            Qt.ItemDataRole.DecorationRole,
            Qt.ItemDataRole.ToolTipRole,
            Qt.ItemDataRole.ForegroundRole,
            Qt.ItemDataRole.BackgroundRole,
            Qt.ItemDataRole.UserRole,
        ]
        rows: list[int] = []
        for key in keys:
            rows.extend(self._object_rows.get(key, []))
        self._emit_data_changed_rows(rows, roles)

    def _rebuild_row_index(self) -> None:
        self._file_rows = []
        self._object_rows = {}
        for row, item in enumerate(self._items):
            if not isinstance(item, ExplorerFileItem):
                continue
            self._file_rows.append(row)
            key = (item.entry.folder_path, item.entry.file_key)
            self._object_rows.setdefault(key, []).append(row)
        self._local_presence_cursor = 0
        self._thumb_cursor = 0

    def _emit_data_changed_rows(self, rows: list[int], roles: list[int]) -> None:
        if not rows:
            return
        normalized = sorted(set(row for row in rows if 0 <= row < len(self._items)))
        if not normalized:
            return

        start = normalized[0]
        prev = normalized[0]
        for row in normalized[1:]:
            if row == prev + 1:
                prev = row
                continue
            top = self.index(start, 0)
            bottom = self.index(prev, 0)
            self.dataChanged.emit(top, bottom, roles)
            start = row
            prev = row

        top = self.index(start, 0)
        bottom = self.index(prev, 0)
        self.dataChanged.emit(top, bottom, roles)

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags

        item = self.item_for_index(index)
        if item is None:
            return Qt.ItemFlag.NoItemFlags

        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if isinstance(item, ExplorerFileItem):
            # Required for external drag start (to Explorer/Desktop).
            return base | Qt.ItemFlag.ItemIsDragEnabled
        return base


# Backward-compatible alias (if referenced elsewhere).
ObjectsIconModel = ExplorerGridModel
