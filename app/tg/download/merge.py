from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
from typing import Any


from app.core.jobs import CancelToken
from app.core.types import PartRecord
from app.tg.parser import parse_caption
from app.tg.download._common import (
    _SHA_PREFIX_RE,
)

logger = logging.getLogger(__name__)


class _DownloadMergeMixin:
    @classmethod
    def _manifest_path(cls, temp_dir: Path) -> Path:
        return temp_dir / cls._MANIFEST_NAME

    @classmethod
    def _load_manifest(cls, temp_dir: Path) -> dict[str, Any] | None:
        manifest_path = cls._manifest_path(temp_dir)
        if not manifest_path.exists():
            return None
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (OSError, json.JSONDecodeError):
            return None
        return None

    @classmethod
    def _write_manifest(
        cls,
        temp_dir: Path,
        file_key: str,
        parts_total: int,
        completed_parts: set[int],
    ) -> None:
        temp_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = cls._manifest_path(temp_dir)
        payload = {
            "file_key": file_key,
            "parts_total": int(parts_total),
            "completed_parts": sorted(int(part_id) for part_id in completed_parts),
        }
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def _is_manifest_compatible(
        manifest: dict[str, Any] | None,
        *,
        file_key: str,
        parts_total: int,
    ) -> bool:
        if manifest is None:
            return False
        return manifest.get("file_key") == file_key and int(
            manifest.get("parts_total", -1)
        ) == int(parts_total)

    @staticmethod
    def _prepare_temp_dir(temp_dir: Path) -> None:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _merge_parts_with_hash_sync(
        output_path: Path,
        temp_dir: Path,
        part_ids: list[int],
        buffer_size: int,
        cancel_token: CancelToken,
        compute_hash: bool,
        expected_total_size: int | None = None,
    ) -> tuple[str | None, int]:
        digest = hashlib.sha256() if compute_hash else None
        total_written = 0
        expected_total: int | None = None
        if expected_total_size is not None:
            expected_total = int(expected_total_size)
            if expected_total < 0:
                raise ValueError(f"Invalid expected output size: {expected_total}")
        ignore_part_payload = expected_total == 0

        # Fast path: a single part needs no concatenation. Move it into place
        # instead of copying the whole payload — saves a full-size read+write in
        # fast mode and the full-size write in strict mode (the read is still
        # needed there to compute the digest). Output is byte-identical and
        # resume is unaffected: this only runs on the successful-merge step,
        # after which the temp dir is removed anyway.
        if len(part_ids) == 1 and not ignore_part_payload:
            cancel_token.raise_if_cancelled()
            part_path = temp_dir / f"part_{part_ids[0]:08d}.bin"
            if not part_path.exists():
                raise ValueError(f"Missing downloaded chunk file: {part_path.name}")
            single_total = int(part_path.stat().st_size)
            if digest is not None:
                with open(part_path, "rb") as part_file:
                    while True:
                        cancel_token.raise_if_cancelled()
                        chunk = part_file.read(buffer_size)
                        if not chunk:
                            break
                        digest.update(chunk)
            os.replace(part_path, output_path)
            return (
                digest.hexdigest() if digest is not None else None,
                single_total,
            )

        with open(output_path, "wb") as out:
            for part_id in part_ids:
                cancel_token.raise_if_cancelled()
                part_path = temp_dir / f"part_{part_id:08d}.bin"
                if not part_path.exists():
                    raise ValueError(f"Missing downloaded chunk file: {part_path.name}")
                with open(part_path, "rb") as part_file:
                    while True:
                        cancel_token.raise_if_cancelled()
                        chunk = part_file.read(buffer_size)
                        if not chunk:
                            break
                        if ignore_part_payload:
                            continue
                        out.write(chunk)
                        total_written += len(chunk)
                        if digest is not None:
                            digest.update(chunk)

        if expected_total is not None and ignore_part_payload:
            total_written = 0

        return (digest.hexdigest() if digest is not None else None, total_written)

    @staticmethod
    def _decrypt_file_to_file(
        enc_path: Path, out_path: Path, crypto_key: bytes
    ) -> None:
        from app.core.utils import decrypt_bytes

        payload = enc_path.read_bytes()
        clear = decrypt_bytes(payload, crypto_key)
        out_path.write_bytes(clear)

    @staticmethod
    def _extract_expected_sha256(
        parts: list[PartRecord], caption_prefix: str
    ) -> str | None:
        expected_sha256_values: set[str] = set()
        for part in parts:
            caption = (part.caption_raw or "").strip()
            if not caption:
                continue
            meta = parse_caption(caption, prefix=caption_prefix)
            if meta is None or not meta.sha256:
                continue
            expected_sha256_values.add(meta.sha256.lower())

        if len(expected_sha256_values) > 1:
            values_preview = ", ".join(sorted(expected_sha256_values))
            raise ValueError(
                f"Integrity metadata conflict: multiple sha256 values in parts ({values_preview})"
            )

        return next(iter(expected_sha256_values), None)

    @staticmethod
    def _extract_expected_orig_size(
        parts: list[PartRecord], caption_prefix: str
    ) -> int | None:
        expected_size_values: set[int] = set()
        for part in parts:
            caption = (part.caption_raw or "").strip()
            if not caption:
                continue
            meta = parse_caption(caption, prefix=caption_prefix)
            if meta is None or meta.orig_size is None:
                continue
            expected_size_values.add(int(meta.orig_size))

        if len(expected_size_values) > 1:
            values_preview = ", ".join(str(v) for v in sorted(expected_size_values))
            raise ValueError(
                f"Integrity metadata conflict: multiple orig_size values in parts ({values_preview})"
            )

        return next(iter(expected_size_values), None)

    @staticmethod
    def _looks_like_sha_prefix(file_key: str) -> bool:
        return bool(_SHA_PREFIX_RE.fullmatch(str(file_key).strip().lower()))
