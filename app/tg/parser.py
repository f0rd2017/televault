from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote

from app.core.types import BatchBlobCaption, PartMeta

_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def parse_caption(caption: str, prefix: str = "FC1|") -> PartMeta | None:
    if not caption:
        return None

    body = caption
    if prefix and caption.startswith(prefix):
        body = caption[len(prefix) :]

    body = body.strip()
    if not body:
        return None

    if body.startswith("{"):
        return _parse_json_meta(body)

    if prefix and not caption.startswith(prefix):
        return None

    return _parse_legacy_meta(body)


def _parse_json_meta(body: str) -> PartMeta | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None
    kind = _pick_str(payload, ("kind", "t"))
    if kind and kind not in {"tgccm_part"}:
        return None

    folder = _pick_str(payload, ("folder_path", "f"))
    file_key = _pick_str(payload, ("file_key", "k"))
    name = _pick_str(payload, ("orig_name", "nm"))
    part_index = _pick_int(payload, ("part_index", "i"))
    parts_total = _pick_int(payload, ("parts_total", "n"))
    sha256 = _pick_sha256(payload, ("sha256", "sha", "s"))
    orig_size = _pick_int(payload, ("orig_size", "os"))
    part_size = _pick_int(payload, ("part_size", "ps"))
    enc = _pick_bool(payload, ("enc", "e"))

    if folder is None or file_key is None or name is None:
        return None
    if part_index is None or parts_total is None:
        return None
    if parts_total <= 0 or part_index < 0 or part_index >= parts_total:
        return None
    if orig_size is not None and orig_size < 0:
        return None
    if part_size is not None and part_size < 0:
        return None

    return PartMeta(
        folder_path=folder,
        file_key=file_key,
        part_index=part_index,
        parts_total=parts_total,
        orig_name=name,
        sha256=sha256,
        orig_size=orig_size,
        part_size=part_size,
        enc=enc,
    )


def parse_batch_blob_caption(
    caption: str, prefix: str = "FC1|"
) -> BatchBlobCaption | None:
    if not caption:
        return None

    body = caption
    if prefix and caption.startswith(prefix):
        body = caption[len(prefix) :]

    body = body.strip()
    if not body or not body.startswith("{"):
        return None

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    kind = _pick_str(payload, ("kind", "t"))
    if kind != "tgccm_batch_blob":
        return None

    version = _pick_int(payload, ("version", "v"))
    folder_path = _pick_str(payload, ("folder_path", "f"))
    blob_key = _pick_str(payload, ("blob_key", "k"))
    orig_name = _pick_str(payload, ("orig_name", "nm"))
    members_count = _pick_int(payload, ("members_count", "mc"))
    manifest_sha256 = _pick_sha256(payload, ("manifest_sha256", "msha"))

    if version is None or version <= 0:
        return None
    if folder_path is None or blob_key is None or orig_name is None:
        return None
    if members_count is None or members_count <= 0:
        return None

    return BatchBlobCaption(
        version=int(version),
        kind="tgccm_batch_blob",
        folder_path=folder_path,
        blob_key=blob_key,
        orig_name=orig_name,
        members_count=int(members_count),
        manifest_sha256=manifest_sha256,
    )


def _parse_legacy_meta(body: str) -> PartMeta | None:
    parts = body.split("|")
    values: dict[str, str] = {}
    for item in parts:
        if "=" not in item:
            continue
        key, raw_value = item.split("=", 1)
        values[key.strip()] = raw_value.strip()

    required = {"f", "k", "i", "n", "nm"}
    if not required.issubset(values):
        return None

    try:
        part_index = int(values["i"])
        parts_total = int(values["n"])
    except ValueError:
        return None

    if parts_total <= 0 or part_index < 0 or part_index >= parts_total:
        return None

    folder = unquote(values["f"])
    file_key = unquote(values["k"])
    name = unquote(values["nm"])
    if not folder or not file_key or not name:
        return None

    return PartMeta(
        folder_path=folder,
        file_key=file_key,
        part_index=part_index,
        parts_total=parts_total,
        orig_name=name,
    )


def build_caption(
    meta: PartMeta,
    prefix: str = "FC1|",
    max_len: int = 1024,
    extra: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "version": 1,
        "kind": "tgccm_part",
        "folder_path": meta.folder_path,
        "file_key": meta.file_key,
        "part_index": meta.part_index,
        "parts_total": meta.parts_total,
        "orig_name": meta.orig_name,
    }
    if extra:
        payload.update(extra)

    caption = (
        f"{prefix}{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )
    if len(caption) <= max_len:
        return caption

    # Keep JSON format, trimming only name first.
    name = str(payload.get("orig_name", "file.bin"))
    overflow = len(caption) - max_len
    trimmed_len = max(8, len(name) - overflow)
    payload["orig_name"] = name[:trimmed_len]
    caption = (
        f"{prefix}{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )
    if len(caption) <= max_len:
        return caption

    # Fallback: use compact keys if still too long.
    compact_payload = {
        "v": 1,
        "t": "tgccm_part",
        "f": str(payload["folder_path"]),
        "k": str(payload["file_key"]),
        "i": int(payload["part_index"]),
        "n": int(payload["parts_total"]),
        "nm": str(payload["orig_name"]),
    }
    for key in ("sha256", "orig_size", "part_size", "enc"):
        if key in payload:
            compact_payload[key] = payload[key]
    caption = f"{prefix}{json.dumps(compact_payload, ensure_ascii=False, separators=(',', ':'))}"
    if len(caption) > max_len:
        raise ValueError("Caption is too long even after compaction")
    return caption


def build_batch_blob_caption(
    meta: BatchBlobCaption,
    *,
    prefix: str = "FC1|",
    max_len: int = 1024,
) -> str:
    payload: dict[str, Any] = {
        "version": int(meta.version),
        "kind": "tgccm_batch_blob",
        "folder_path": str(meta.folder_path),
        "blob_key": str(meta.blob_key),
        "orig_name": str(meta.orig_name),
        "members_count": int(meta.members_count),
    }
    if meta.manifest_sha256:
        payload["manifest_sha256"] = str(meta.manifest_sha256).lower()

    caption = (
        f"{prefix}{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )
    if len(caption) <= max_len:
        return caption

    name = str(payload.get("orig_name", "batch.zip"))
    overflow = len(caption) - max_len
    trimmed_len = max(8, len(name) - overflow)
    payload["orig_name"] = name[:trimmed_len]
    caption = (
        f"{prefix}{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )
    if len(caption) <= max_len:
        return caption

    compact_payload = {
        "v": int(meta.version),
        "t": "tgccm_batch_blob",
        "f": str(meta.folder_path),
        "k": str(meta.blob_key),
        "nm": str(payload["orig_name"]),
        "mc": int(meta.members_count),
    }
    if meta.manifest_sha256:
        compact_payload["msha"] = str(meta.manifest_sha256).lower()
    caption = f"{prefix}{json.dumps(compact_payload, ensure_ascii=False, separators=(',', ':'))}"
    if len(caption) > max_len:
        raise ValueError("Batch blob caption is too long even after compaction")
    return caption


def _pick_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _pick_int(payload: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                continue
    return None


def _pick_bool(payload: dict[str, Any], keys: tuple[str, ...]) -> bool | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            if value in {0, 1}:
                return bool(value)
            continue
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes"}:
                return True
            if normalized in {"0", "false", "no"}:
                return False
    return None


def _pick_sha256(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        candidate = value.strip().lower()
        if _SHA256_HEX_RE.fullmatch(candidate):
            return candidate
    return None
