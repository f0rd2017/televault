from __future__ import annotations

import logging

from telethon.tl.custom.message import Message

from televault.core.jobs import CancelToken
from televault.core.types import AppConfig, PartRecord, ScanStats
from televault.core.utils import now_ts, normalize_folder_path, sanitize_filename
from televault.db.repo import DbRepo
from televault.tg.parser import parse_batch_blob_caption, parse_caption

logger = logging.getLogger(__name__)


class TgScanner:
    _SCAN_BATCH_SIZE = 500
    _INCREMENTAL_OVERLAP_IDS = 500
    _UNMANAGED_FOLDER = "Imported"

    def __init__(
        self,
        config: AppConfig,
        repo: DbRepo,
        client=None,
        chat=None,
        chat_id: str | None = None,
        *,
        chats: list[object] | None = None,
        chat_ids: list[str] | None = None,
        client_by_chat_id: dict[str, object] | None = None,
    ) -> None:
        self.config = config
        self.repo = repo

        # Support multi-client mode via client_by_chat_id mapping
        resolved_chats = list(chats or [])
        resolved_chat_ids = [str(item) for item in (chat_ids or [])]

        if resolved_chats or resolved_chat_ids:
            if len(resolved_chats) != len(resolved_chat_ids):
                raise ValueError("TgScanner: 'chats' and 'chat_ids' length mismatch")
            if not resolved_chats:
                raise ValueError(
                    "TgScanner: multi-channel mode requires at least one chat"
                )
            self._channels = list(zip(resolved_chats, resolved_chat_ids))
        else:
            if chat is None or not str(chat_id or "").strip():
                raise ValueError(
                    "TgScanner: single-channel mode requires 'chat' and 'chat_id'"
                )
            resolved_chat_id = str(chat_id)
            self._channels = [(chat, resolved_chat_id)]

        # Build client mapping for multi-client support
        self._client_by_chat_id = {}

        if client_by_chat_id:
            # Multi-client mode: use provided mapping
            self._client_by_chat_id = dict(client_by_chat_id)
            # Ensure fallback to single client for backward compatibility
            if not self._client_by_chat_id and client is not None:
                self._client_by_chat_id = {chat_id: client} if chat_id else {}

        self.client = client  # Fallback client for single-channel or legacy mode

    def _get_client_for_chat(self, chat_id: str) -> object:
        """Get the appropriate client for scanning a specific chat."""
        # Try exact match first
        if chat_id in self._client_by_chat_id:
            return self._client_by_chat_id[chat_id]

        # Try without the Telegram channel prefix (e.g., "-1001234" vs "1234").
        # NOTE: must strip the literal "-100" prefix, NOT lstrip("100") which
        # greedily removes any leading '1'/'0' characters and mangles real ids.
        cleaned = self._normalize_chat_id_for_match(chat_id)
        for cid, client in self._client_by_chat_id.items():
            if self._normalize_chat_id_for_match(str(cid)) == cleaned:
                return client

        # Fallback to main client if available
        if self.client is not None:
            return self.client

        # Last resort: first client in mapping
        if self._client_by_chat_id:
            return next(iter(self._client_by_chat_id.values()))

        raise ValueError(f"No client available for chat {chat_id}")

    @staticmethod
    def _normalize_chat_id_for_match(chat_id: str) -> str:
        """Normalize a chat id for fuzzy client matching.

        Telegram supergroup/channel ids appear both as the bare id and in the
        ``-100<id>`` peer form. Strip only that exact prefix so ids are compared
        on equal footing (unlike ``lstrip`` which eats any leading '1'/'0').
        """
        s = str(chat_id).strip()
        if s.startswith("-100"):
            return s[4:]
        if s.startswith("-"):
            return s[1:]
        return s

    async def refresh_incremental(
        self, cancel_token: CancelToken | None = None
    ) -> ScanStats:
        return await self._scan_all_channels(
            mode="incremental", cancel_token=cancel_token
        )

    async def refresh_full(self, cancel_token: CancelToken | None = None) -> ScanStats:
        return await self._scan_all_channels(mode="full", cancel_token=cancel_token)

    async def reconcile(self, cancel_token: CancelToken | None = None) -> ScanStats:
        return await self._scan_all_channels(
            mode="reconcile", cancel_token=cancel_token
        )

    async def _scan_all_channels(
        self,
        *,
        mode: str,
        cancel_token: CancelToken | None = None,
    ) -> ScanStats:
        total_processed = 0
        total_indexed = 0
        total_skipped = 0
        total_deleted_marked = 0
        max_seen_global = 0

        scanned_chat_ids = {cid for _, cid in self._channels}

        # In reconcile mode, check whether the database has any "orphaned" chats
        if mode == "reconcile":
            all_db_chat_ids = set(self.repo.list_all_indexed_chat_ids())
            orphaned = all_db_chat_ids - scanned_chat_ids
            if orphaned:
                logger.warning(
                    "⚠️ Found %d chats in the database that are not scanned by the current accounts: %s. "
                    "Files from these chats may be stale and will not be removed automatically during reconcile.",
                    len(orphaned),
                    ", ".join(list(orphaned)[:5])
                    + ("..." if len(orphaned) > 5 else ""),
                )

        for chat, chat_id in self._channels:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()

            if mode == "full":
                self.repo.clear_index(chat_id)

            (
                processed,
                indexed,
                skipped,
                deleted_marked,
                max_seen,
            ) = await self._scan_single_channel(
                chat, chat_id, mode=mode, cancel_token=cancel_token
            )

            total_processed += processed
            total_indexed += indexed
            total_skipped += skipped
            total_deleted_marked += deleted_marked
            max_seen_global = max(max_seen_global, max_seen)

        if total_indexed > 0 or total_deleted_marked > 0:
            self.repo.rebuild_objects_aggregates()

        logger.info(
            "📊 SCAN SUMMARY (%s): processed=%d indexed=%d deleted=%d skipped=%d max_id=%d",
            mode,
            total_processed,
            total_indexed,
            total_deleted_marked,
            total_skipped,
            max_seen_global,
        )

        return ScanStats(
            processed_messages=total_processed,
            indexed_parts=total_indexed,
            max_msg_id=max_seen_global,
            deleted_marked=total_deleted_marked,
            parse_skipped=total_skipped,
        )

    async def _scan_single_channel(
        self,
        chat: object,
        chat_id: str,
        *,
        mode: str,
        cancel_token: CancelToken | None = None,
    ) -> tuple[int, int, int, int, int]:
        state = self.repo.get_state(chat_id)
        last_max = int(state["last_max_msg_id"] or 0)

        min_id = 0
        if mode == "incremental":
            min_id = max(0, last_max - self._INCREMENTAL_OVERLAP_IDS)

        max_seen = last_max if mode != "full" else 0
        processed = 0
        indexed = 0
        skipped = 0
        seen_msg_ids: set[int] = set()
        batch_parts: list[PartRecord] = []
        batch_folders: set[str] = set()

        client = self._get_client_for_chat(chat_id)

        async for message in client.iter_messages(chat, min_id=min_id):
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()

            processed += 1
            msg_id = int(message.id)
            max_seen = max(max_seen, msg_id)

            record = self._parse_message_to_record(message, chat_id=chat_id)
            if record is None:
                skipped += 1
                continue

            # Mark as seen only if it's a valid file message
            if mode == "reconcile":
                seen_msg_ids.add(msg_id)

            indexed += 1
            batch_parts.append(record)
            batch_folders.add(record.folder_path)

            if len(batch_parts) >= self._SCAN_BATCH_SIZE:
                self._flush_batch(batch_parts, batch_folders)
                batch_parts.clear()
                batch_folders.clear()

        self._flush_batch(batch_parts, batch_folders)

        deleted_marked = 0
        if mode == "reconcile":
            local_msg_ids = set(self.repo.list_msg_ids(chat_id))
            missing = sorted(local_msg_ids - seen_msg_ids)
            deleted_marked = self.repo.mark_messages_deleted(missing, chat_id=chat_id)

        self.repo.update_state_last_max_id(chat_id, max_seen, now_ts())

        logger.info(
            "Scan %s telemetry: chat=%s processed=%d indexed=%d skipped=%d deleted=%d max_id=%d",
            mode,
            chat_id,
            processed,
            indexed,
            skipped,
            deleted_marked,
            max_seen,
        )

        return processed, indexed, skipped, deleted_marked, max_seen

    def _parse_message_to_record(
        self, message: Message, *, chat_id: str
    ) -> PartRecord | None:
        caption = (message.message or "").strip()
        blob_meta = parse_batch_blob_caption(caption, prefix=self.config.caption_prefix)
        if blob_meta is not None:
            if not self._is_file_message(message):
                logger.debug(
                    "Skipping ghost batch blob message (no media): msg_id=%s",
                    message.id,
                )
                return None
            folder_path = normalize_folder_path(blob_meta.folder_path)
            file_key = blob_meta.blob_key
            part_index = 0
            parts_total = 1
            orig_name = sanitize_filename(blob_meta.orig_name)
        else:
            meta = parse_caption(caption, prefix=self.config.caption_prefix)
            if meta is not None:
                if not self._is_file_message(message):
                    logger.debug(
                        "Skipping ghost part message (no media): msg_id=%s", message.id
                    )
                    return None
                folder_path = normalize_folder_path(meta.folder_path)
                file_key = meta.file_key
                part_index = meta.part_index
                parts_total = meta.parts_total
                orig_name = sanitize_filename(meta.orig_name)
            else:
                if not self._is_file_message(message):
                    return None
                msg_id = int(message.id)
                folder_path = normalize_folder_path(self._UNMANAGED_FOLDER)
                file_key = self._unmanaged_file_key(msg_id, chat_id=chat_id)
                part_index = 0
                parts_total = 1
                orig_name = sanitize_filename(self._extract_file_name(message, msg_id))

        file_size = self._extract_file_size(message)
        if message.date:
            try:
                date_ts = int(message.date.timestamp())
            except (OSError, OverflowError, ValueError):
                date_ts = now_ts()
        else:
            date_ts = now_ts()

        return PartRecord(
            msg_id=int(message.id),
            chat_id=str(chat_id),
            folder_path=folder_path,
            file_key=file_key,
            part_index=part_index,
            parts_total=parts_total,
            orig_name=orig_name,
            file_size=file_size,
            caption_raw=caption,
            date_ts=date_ts,
        )

    @staticmethod
    def _is_file_message(message: Message) -> bool:
        return bool(
            getattr(message, "file", None) is not None
            or getattr(message, "document", None) is not None
        )

    @staticmethod
    def _extract_file_name(message: Message, msg_id: int) -> str:
        file_obj = getattr(message, "file", None)
        file_name = getattr(file_obj, "name", None)
        if isinstance(file_name, str) and file_name.strip():
            return file_name.strip()

        document = getattr(message, "document", None)
        attributes = getattr(document, "attributes", None) or []
        for attr in attributes:
            attr_name = getattr(attr, "file_name", None)
            if isinstance(attr_name, str) and attr_name.strip():
                return attr_name.strip()

        return f"message_{int(msg_id)}.bin"

    def _unmanaged_file_key(self, msg_id: int, *, chat_id: str) -> str:
        if len(self._channels) <= 1:
            return f"msg_{int(msg_id):016x}"
        chat_token = str(chat_id or "").strip().replace("-", "m")
        return f"msg_{chat_token}_{int(msg_id):016x}"

    @staticmethod
    def _extract_file_size(message: Message) -> int | None:
        if (
            getattr(message, "file", None) is not None
            and getattr(message.file, "size", None) is not None
        ):
            return int(message.file.size)
        if (
            getattr(message, "document", None) is not None
            and getattr(message.document, "size", None) is not None
        ):
            return int(message.document.size)
        return None

    def _flush_batch(self, parts: list[PartRecord], folders: set[str]) -> None:
        if not parts:
            return
        self.repo.upsert_folders_bulk(list(folders))
        self.repo.upsert_msg_parts_bulk(parts)
