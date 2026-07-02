"""Folder panel mixin: navigation, history, path bar, folder tree sync."""

from __future__ import annotations

from app.ui.models_qt import ExplorerFolderItem


class FolderPanelMixin:
    """Methods for folder navigation, history, and tree sync."""

    def _push_history(self, folder: str | None) -> None:
        current = self._history[self._history_index] if self._history else None
        if current == folder:
            return
        self._history = self._history[: self._history_index + 1]
        self._history.append(folder)
        self._history_index += 1

    def _set_current_folder(
        self, folder: str | None, push_history: bool = True, sync_tree: bool = True
    ) -> None:
        self.current_folder = folder
        if push_history:
            self._push_history(folder)
        if sync_tree:
            self._sync_folder_selection()
        self.reload_items()

    def _sync_folder_selection(self) -> None:
        index = self.folder_model.find_index_by_path(self.current_folder)
        if index.isValid():
            self.folder_tree.setCurrentIndex(index)
            self._expand_to_index(index)
        else:
            self.folder_tree.clearSelection()

    def _expand_to_index(self, index) -> None:
        current = index
        while current.isValid():
            self.folder_tree.expand(current)
            current = current.parent()

    def _list_child_folders(self, folder: str | None) -> list[str]:
        child_paths: set[str] = set()

        if folder:
            prefix = f"{folder}/"
            for path in self._all_folders:
                if not path.startswith(prefix):
                    continue
                rest = path[len(prefix) :]
                if not rest:
                    continue
                child_name = rest.split("/", 1)[0]
                child_paths.add(f"{prefix}{child_name}")
        else:
            for path in self._all_folders:
                child_name = path.split("/", 1)[0]
                child_paths.add(child_name)

        return sorted(child_paths, key=lambda x: x.lower())

    def _on_folder_clicked(self, index) -> None:
        folder = self.folder_model.path_from_index(index)
        self._set_current_folder(folder, push_history=True, sync_tree=False)

    def _on_nav_back(self) -> None:
        if self._history_index <= 0:
            return
        self._history_index -= 1
        target = self._history[self._history_index]
        self._set_current_folder(target, push_history=False, sync_tree=True)

    def _on_nav_forward(self) -> None:
        if self._history_index >= len(self._history) - 1:
            return
        self._history_index += 1
        target = self._history[self._history_index]
        self._set_current_folder(target, push_history=False, sync_tree=True)

    def _on_nav_up(self) -> None:
        if not self.current_folder:
            return
        if "/" in self.current_folder:
            parent = self.current_folder.rsplit("/", 1)[0]
        else:
            parent = None
        self._set_current_folder(parent, push_history=True, sync_tree=True)

    def _on_nav_up_shortcut(self) -> None:
        if self._is_text_input_focused():
            return
        self._on_nav_up()

    def _update_path_bar(self) -> None:
        suffix = self.current_folder if self.current_folder else ""
        self.path_bar.setText(f"Cloud:/{suffix}")

    def _on_item_activated(self, index) -> None:
        from app.ui.models_qt import (
            ExplorerFileItem,
            is_image_name,
            is_text_editable_name,
            is_video_name,
            is_pdf_name,
        )

        item = self.explorer_model.item_for_index(index)
        if isinstance(item, ExplorerFolderItem):
            self._set_current_folder(item.path, push_history=True, sync_tree=True)
            return
        if isinstance(item, ExplorerFileItem):
            # Double-click no longer auto-downloads (it caused surprise/mass
            # downloads). Download is explicit now: the D shortcut, the toolbar
            # button, or the right-click menu. Exception: images/videos open in
            # a preview WITHOUT downloading, streamed via the local media server
            # (same path as the "Открыть без скачивания" menu action).
            name = item.entry.orig_name
            if is_image_name(name) or is_video_name(name) or is_pdf_name(name):
                self._on_open_stream(item.entry)
            elif is_text_editable_name(name):
                self._on_edit_file(item.entry)
            return
