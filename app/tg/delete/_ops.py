from __future__ import annotations

import logging
import shutil
from pathlib import Path


from app.core.jobs import CancelToken, JobCancelledError
from app.core.types import PartMeta
from app.core.utils import normalize_folder_path
from app.tg.parser import build_caption, parse_caption

logger = logging.getLogger(__name__)


class _OpsMixin:
    async def delete_remote(
        self,
        folder_path: str,
        file_key: str,
        cancel_token: CancelToken | None = None,
    ) -> dict[str, int]:
        storage_kind = self.repo.resolve_object_storage(folder_path, file_key)
        if storage_kind == "batch_member":
            return await self._delete_batch_member_remote(
                folder_path=folder_path,
                file_key=file_key,
                cancel_token=cancel_token,
            )

        refs = self.repo.get_all_msg_index_refs_for_object(
            folder_path=folder_path, file_key=file_key
        )
        if not refs:
            return {"deleted": 0, "failed": 0}

        refs_by_chat: dict[str, list[int]] = {}
        for chat_id, msg_id in refs:
            refs_by_chat.setdefault(chat_id, []).append(msg_id)

        deleted_refs: list[tuple[str, int]] = []
        failed_refs: list[tuple[str, int]] = []
        orphaned_refs: list[tuple[str, int]] = []
        clients_used: set[str] = set()
        first_error: str | None = None
        for part_chat_id, msg_ids in refs_by_chat.items():
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            unique_ids = list(dict.fromkeys(int(msg_id) for msg_id in msg_ids))
            try:
                (
                    deleted_ids,
                    failed_ids,
                    route_label,
                    reason,
                ) = await self._delete_with_retry(
                    part_chat_id, unique_ids, cancel_token=cancel_token
                )
                deleted_refs.extend((part_chat_id, msg_id) for msg_id in deleted_ids)
                clients_used.add(str(route_label))
                if failed_ids:
                    # Недоступный канал (нет маршрута) — помечаем как orphaned,
                    # реальные отказы (forbidden/невалидная сущность) — как ошибку
                    # с понятной причиной для пользователя.
                    if reason and "No delete route available" in reason:
                        logger.warning(
                            "No route for chat_id=%s — marking %d parts as orphaned (legacy channel)",
                            part_chat_id,
                            len(failed_ids),
                        )
                        orphaned_refs.extend(
                            (part_chat_id, msg_id) for msg_id in failed_ids
                        )
                    else:
                        failed_refs.extend(
                            (part_chat_id, msg_id) for msg_id in failed_ids
                        )
                        if first_error is None and reason:
                            first_error = reason
            except JobCancelledError:
                raise
            except RuntimeError as exc:
                err_msg = str(exc)
                # Detect orphaned legacy channel — no route available
                if "No delete route available" in err_msg:
                    logger.warning(
                        "No route for chat_id=%s — marking %d parts as orphaned (legacy channel)",
                        part_chat_id,
                        len(unique_ids),
                    )
                    orphaned_refs.extend(
                        (part_chat_id, msg_id) for msg_id in unique_ids
                    )
                else:
                    failed_refs.extend((part_chat_id, msg_id) for msg_id in unique_ids)
                    if first_error is None:
                        first_error = err_msg
            except Exception as exc:
                failed_refs.extend((part_chat_id, msg_id) for msg_id in unique_ids)
                if first_error is None:
                    first_error = str(exc)

        # Помечаем удалённые + orphaned (legacy каналы, недоступные для удаления)
        deleted = self.repo.mark_messages_deleted_refs(deleted_refs)
        if orphaned_refs:
            orphaned_deleted = self.repo.mark_messages_deleted_refs(orphaned_refs)
            logger.info(
                "Marked %d orphaned parts as deleted (legacy channels inaccessible)",
                orphaned_deleted,
            )

        # Always rebuild aggregate to ensure database consistency
        self.repo.rebuild_object_aggregate(self.chat_id, folder_path, file_key)

        if failed_refs:
            error_suffix = f": {first_error}" if first_error else ""
            logger.error(
                "Failed to delete %d message part(s) in Telegram%s. Chat references may be invalid.",
                len(failed_refs),
                error_suffix,
            )
            # ВАЖНО: Мы не помечаем failed_refs как orphaned автоматически здесь,
            # чтобы не удалять из базы то, что не удалилось в реальности.
            # Тесты ожидают, что при ошибке объект остается в базе.

            channels_used = sorted(refs_by_chat.keys())
            raise RuntimeError(
                f"Failed to delete {len(failed_refs)} part(s) in Telegram{error_suffix}"
            )

        channels_used = sorted(refs_by_chat.keys())
        return {
            "deleted": deleted + len(orphaned_refs),
            "failed": 0,
            "orphaned": len(orphaned_refs),
            "channels_used": channels_used,
            "clients_used": sorted(clients_used),
            "cross_channel_parts": bool(len(channels_used) > 1),
        }

    async def _delete_batch_member_remote(
        self,
        *,
        folder_path: str,
        file_key: str,
        cancel_token: CancelToken | None = None,
    ) -> dict[str, int]:
        member = self.repo.get_batch_member(folder_path, file_key)
        if member is None or member.deleted_ts is not None:
            return {"deleted": 0, "failed": 0, "logical_only": True}
        marked = self.repo.mark_batch_member_deleted(folder_path, file_key)
        if marked <= 0:
            return {"deleted": 0, "failed": 0, "logical_only": True}

        active_count = self.repo.count_active_batch_members(member.blob_key)
        if active_count > 0:
            return {
                "deleted": 1,
                "failed": 0,
                "logical_only": True,
                "blob_gc": False,
                "blob_key": member.blob_key,
            }

        blob_parts = self.repo.get_parts_for_blob(member.blob_key)
        if not blob_parts:
            self.repo.mark_batch_blob_deleted(member.blob_key)
            return {
                "deleted": 1,
                "failed": 0,
                "logical_only": True,
                "blob_gc": True,
                "blob_key": member.blob_key,
                "channels_used": [],
                "clients_used": [],
                "cross_channel_parts": False,
            }

        refs_by_chat: dict[str, list[int]] = {}
        for part in blob_parts:
            chat_id = str(part.chat_id or self.chat_id)
            refs_by_chat.setdefault(chat_id, []).append(int(part.msg_id))

        deleted_refs: list[tuple[str, int]] = []
        failed_refs: list[tuple[str, int]] = []
        orphaned_refs: list[tuple[str, int]] = []
        clients_used: set[str] = set()
        first_error: str | None = None
        for part_chat_id, msg_ids in refs_by_chat.items():
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            unique_ids = list(dict.fromkeys(int(msg_id) for msg_id in msg_ids))
            try:
                (
                    deleted_ids,
                    failed_ids,
                    route_label,
                    reason,
                ) = await self._delete_with_retry(
                    part_chat_id,
                    unique_ids,
                    cancel_token=cancel_token,
                )
                deleted_refs.extend((part_chat_id, msg_id) for msg_id in deleted_ids)
                clients_used.add(str(route_label))
                if failed_ids:
                    if reason and "No delete route available" in reason:
                        logger.warning(
                            "No route for chat_id=%s (batch blob) — marking %d parts as orphaned",
                            part_chat_id,
                            len(failed_ids),
                        )
                        orphaned_refs.extend(
                            (part_chat_id, msg_id) for msg_id in failed_ids
                        )
                    else:
                        failed_refs.extend(
                            (part_chat_id, msg_id) for msg_id in failed_ids
                        )
                        if first_error is None and reason:
                            first_error = reason
            except JobCancelledError:
                raise
            except RuntimeError as exc:
                err_msg = str(exc)
                if "No delete route available" in err_msg:
                    logger.warning(
                        "No route for chat_id=%s (batch blob) — marking %d parts as orphaned",
                        part_chat_id,
                        len(unique_ids),
                    )
                    orphaned_refs.extend(
                        (part_chat_id, msg_id) for msg_id in unique_ids
                    )
                else:
                    failed_refs.extend((part_chat_id, msg_id) for msg_id in unique_ids)
                    if first_error is None:
                        first_error = err_msg
            except Exception as exc:
                failed_refs.extend((part_chat_id, msg_id) for msg_id in unique_ids)
                if first_error is None:
                    first_error = str(exc)

        deleted = self.repo.mark_messages_deleted_refs(deleted_refs)
        if orphaned_refs:
            orphaned_deleted = self.repo.mark_messages_deleted_refs(orphaned_refs)
            logger.info(
                "Marked %d orphaned batch blob parts as deleted (legacy channels)",
                orphaned_deleted,
            )
        self.repo.mark_batch_blob_deleted(member.blob_key)
        if failed_refs:
            error_suffix = f": {first_error}" if first_error else ""
            # Логируем детали о неудачных попытках удаления
            logger.error(
                "Failed to delete blob message part(s) in Telegram%s. Chat references may be invalid.",
                error_suffix,
            )
            # Помечаем неудачные попытки как удаленные, чтобы не пытаться снова
            for chat_id, msg_id in failed_refs:
                try:
                    # Проверим, существует ли сообщение вообще
                    route_client, route_chat, route_label = await self._pick_route(
                        chat_id
                    )
                    validated_chat = await self._validate_or_resolve_chat(
                        route_chat, route_client
                    )
                    if validated_chat is None:
                        # Если не можем получить действительный чат, помечаем как удаленный
                        orphaned_deleted = self.repo.mark_messages_deleted_refs(
                            [(chat_id, msg_id)]
                        )
                        logger.info(
                            "Marked blob message %s in chat %s as orphaned due to invalid chat reference",
                            msg_id,
                            chat_id,
                        )
                except Exception:
                    # Если вообще не можем обработать чат, помечаем как удаленный
                    orphaned_deleted = self.repo.mark_messages_deleted_refs(
                        [(chat_id, msg_id)]
                    )
                    logger.info(
                        "Marked blob message %s in chat %s as orphaned due to chat resolution failure",
                        msg_id,
                        chat_id,
                    )

            # Возвращаем результат с информацией о фактически удаленных сообщениях
            channels_used = sorted(refs_by_chat.keys())
            return {
                "deleted": max(1, int(deleted) + len(orphaned_refs)),
                "failed": len(failed_refs),
                "orphaned": len(orphaned_refs),
                "logical_only": True,
                "blob_gc": True,
                "blob_key": member.blob_key,
                "channels_used": channels_used,
                "clients_used": sorted(clients_used),
                "cross_channel_parts": bool(len(channels_used) > 1),
            }
        channels_used = sorted(refs_by_chat.keys())
        return {
            "deleted": max(1, int(deleted) + len(orphaned_refs)),
            "failed": 0,
            "orphaned": len(orphaned_refs),
            "logical_only": True,
            "blob_gc": True,
            "blob_key": member.blob_key,
            "channels_used": channels_used,
            "clients_used": sorted(clients_used),
            "cross_channel_parts": bool(len(channels_used) > 1),
        }

    async def delete_folder(
        self,
        folder_path: str,
        progress_cb=None,
        cancel_token: CancelToken | None = None,
    ) -> dict[str, int]:
        objects = self.repo.list_objects_recursive(folder_path)

        # Собираем ВСЕ живые сообщения папки напрямую из msg_index. Это покрывает
        # и обычные файлы, и batch-blob'ы (зип-архивы мелких файлов), сообщения
        # которых не попадают в objects, когда участники уже логически удалены.
        # Источник — msg_index (is_deleted=0), т.е. то, что reconcile реально
        # видел в канале: повторное удаление дотирает то, что прошлая неудачная
        # попытка оставила в канале (флаг batch_blobs.is_deleted может разойтись
        # с реальностью и тогда блобы пропускались навсегда).
        refs_by_chat: dict[str, list[int]] = {}
        for chat_id, msg_id in self.repo.get_live_msg_refs_for_folder(
            folder_path, recursive=True
        ):
            refs_by_chat.setdefault(chat_id, []).append(msg_id)

        if not refs_by_chat:
            # Удалять в канале нечего — чистим локальные записи (включая blob'ы).
            self.repo.mark_folder_batch_blobs_deleted(folder_path)
            self.repo.delete_folder(folder_path)
            local_dir = Path(
                self.config.cache_dir
            ).expanduser().resolve() / normalize_folder_path(folder_path)
            if local_dir.exists():
                shutil.rmtree(local_dir, ignore_errors=True)
            return {"deleted": 0, "files": len(objects)}

        # Удаляем сообщения пакетно — один delete_messages на каждый chat_id
        total_deleted = 0
        total_failed = 0
        total_refs = sum(len(ids) for ids in refs_by_chat.values())
        processed_refs = 0

        for chat_id, msg_ids in refs_by_chat.items():
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()

            unique_ids = list(dict.fromkeys(msg_ids))
            try:
                (
                    deleted_ids,
                    failed_ids,
                    route_label,
                    reason,
                ) = await self._delete_with_retry(
                    chat_id, unique_ids, cancel_token=cancel_token
                )
                total_deleted += len(deleted_ids)
                total_failed += len(failed_ids)
                if failed_ids and reason:
                    logger.warning(
                        "Folder delete: %d part(s) in chat_id=%s could not be deleted: %s",
                        len(failed_ids),
                        chat_id,
                        reason,
                    )
                # Mark both successfully deleted and failed IDs to prevent resurrection
                all_to_mark = deleted_ids + failed_ids
                if all_to_mark:
                    self.repo.mark_messages_deleted_refs(
                        [(chat_id, mid) for mid in all_to_mark]
                    )
            except RuntimeError as exc:
                if "No delete route available" in str(exc):
                    logger.warning(
                        "No route for chat_id=%s in folder %s — marking %d refs as orphaned",
                        chat_id,
                        folder_path,
                        len(unique_ids),
                    )
                    total_deleted += len(unique_ids)
                    self.repo.mark_messages_deleted_refs(
                        [(chat_id, mid) for mid in unique_ids]
                    )
                else:
                    total_failed += len(unique_ids)
                    logger.error(
                        "Failed to delete folder parts in chat_id=%s: %s", chat_id, exc
                    )
                    # Mark as deleted anyway to prevent resurrection
                    self.repo.mark_messages_deleted_refs(
                        [(chat_id, mid) for mid in unique_ids]
                    )
            except Exception as exc:
                total_failed += len(unique_ids)
                logger.error(
                    "Failed to delete folder parts in chat_id=%s: %s", chat_id, exc
                )
                # Mark as deleted anyway to prevent resurrection
                self.repo.mark_messages_deleted_refs(
                    [(chat_id, mid) for mid in unique_ids]
                )

            processed_refs += len(unique_ids)
            if progress_cb and total_refs > 0:
                pct = (processed_refs / total_refs) * 100
                await progress_cb(
                    pct, f"Deleted {processed_refs}/{total_refs} message parts"
                )

        # Помечаем blob'ы папки удалёнными, чтобы они не воскресали из batch-таблиц.
        self.repo.mark_folder_batch_blobs_deleted(folder_path)

        # Удаляем записи папки из БД
        self.repo.delete_folder(folder_path)

        # Удаляем локальные кэшированные файлы
        local_dir = Path(
            self.config.cache_dir
        ).expanduser().resolve() / normalize_folder_path(folder_path)
        if local_dir.exists():
            shutil.rmtree(local_dir, ignore_errors=True)

        return {
            "deleted": total_deleted,
            "failed": total_failed,
            "files": len(objects),
        }

    async def rename_file(
        self,
        folder_path: str,
        file_key: str,
        new_name: str,
        progress_cb=None,
        cancel_token: CancelToken | None = None,
    ) -> dict[str, int]:
        normalized_folder = normalize_folder_path(folder_path)
        storage_kind = self.repo.resolve_object_storage(normalized_folder, file_key)
        if storage_kind == "batch_member":
            updated = self.repo.rename_batch_member(
                normalized_folder, file_key, new_name
            )
            return {
                "edited": 0,
                "failed": 0,
                "total": int(updated),
                "logical_only": True,
            }

        parts = self.repo.get_parts_for_object(
            folder_path=normalized_folder, file_key=file_key
        )
        edited = 0
        failed = 0
        channels_used: set[str] = set()
        clients_used: set[str] = set()
        for i, part in enumerate(parts):
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            parsed = parse_caption(
                part.caption_raw or "", prefix=self.config.caption_prefix
            )
            if parsed is not None:
                meta = parsed
                extra: dict[str, object] = {}
                if meta.sha256:
                    extra["sha256"] = meta.sha256
                if meta.orig_size is not None:
                    extra["orig_size"] = int(meta.orig_size)
                if meta.part_size is not None:
                    extra["part_size"] = int(meta.part_size)
                if meta.enc is not None:
                    extra["enc"] = bool(meta.enc)
            else:
                meta = PartMeta(
                    folder_path=part.folder_path,
                    file_key=part.file_key,
                    part_index=part.part_index,
                    parts_total=part.parts_total,
                    orig_name=part.orig_name,
                )
                extra = {}

            new_meta = PartMeta(
                folder_path=meta.folder_path,
                file_key=meta.file_key,
                part_index=meta.part_index,
                parts_total=meta.parts_total,
                orig_name=new_name,
                sha256=meta.sha256,
                orig_size=meta.orig_size,
                part_size=meta.part_size,
                enc=meta.enc,
            )
            new_caption = build_caption(
                new_meta,
                prefix=self.config.caption_prefix,
                extra=extra or None,
            )
            try:
                part_chat_id = str(part.chat_id or self.chat_id)
                route_client, route_chat, route_label = await self._pick_route(
                    part_chat_id
                )
                await route_client.edit_message(
                    route_chat, part.msg_id, text=new_caption
                )
                self.repo.update_caption_raw(
                    part.msg_id, new_caption, chat_id=str(part.chat_id)
                )
                edited += 1
                channels_used.add(part_chat_id)
                clients_used.add(str(route_label))
            except JobCancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Rename part failed: msg_id=%s chat_id=%s error=%s",
                    part.msg_id,
                    part.chat_id,
                    exc,
                )
                failed += 1
            if progress_cb and parts:
                await progress_cb(
                    (i + 1) / len(parts) * 100, f"Renamed part {i + 1}/{len(parts)}"
                )
        if edited > 0 and failed == 0:
            self.repo.rename_object(normalized_folder, file_key, new_name)
        return {
            "edited": edited,
            "failed": failed,
            "total": len(parts),
            "channels_used": sorted(channels_used),
            "clients_used": sorted(clients_used),
            "cross_channel_parts": bool(len(channels_used) > 1),
        }
