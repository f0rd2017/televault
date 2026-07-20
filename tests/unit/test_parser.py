from televault.core.types import PartMeta
from televault.tg.parser import (
    build_batch_blob_caption,
    build_caption,
    parse_batch_blob_caption,
    parse_caption,
)


def test_parse_caption_valid_roundtrip() -> None:
    meta = PartMeta(
        folder_path="Anime/Cache",
        file_key="abc123def456",
        part_index=3,
        parts_total=10,
        orig_name="shader.bin",
    )
    caption = build_caption(meta)
    parsed = parse_caption(caption)
    assert parsed == meta


def test_parse_caption_legacy_supported() -> None:
    caption = "FC1|f=Anime/Cache|k=abc123def456|i=0|n=2|nm=file.bin"
    parsed = parse_caption(caption)
    assert parsed == PartMeta(
        folder_path="Anime/Cache",
        file_key="abc123def456",
        part_index=0,
        parts_total=2,
        orig_name="file.bin",
    )


def test_parse_caption_json_without_prefix() -> None:
    caption = '{"folder_path":"A/B","file_key":"k1","part_index":1,"parts_total":2,"orig_name":"f.bin"}'
    parsed = parse_caption(caption, prefix="")
    assert parsed == PartMeta(
        folder_path="A/B",
        file_key="k1",
        part_index=1,
        parts_total=2,
        orig_name="f.bin",
    )


def test_parse_caption_json_with_integrity_meta() -> None:
    caption = (
        'FC1|{"folder_path":"A/B","file_key":"k1","part_index":1,"parts_total":2,'
        '"orig_name":"f.bin","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"orig_size":123,"part_size":64,"enc":true}'
    )
    parsed = parse_caption(caption)
    assert parsed is not None
    assert parsed.sha256 == "a" * 64
    assert parsed.orig_size == 123
    assert parsed.part_size == 64
    assert parsed.enc is True


def test_parse_caption_broken() -> None:
    assert parse_caption("FC1|f=a|k=b|n=10|nm=x") is None
    assert parse_caption("FC1|f=a|k=b|i=z|n=10|nm=x") is None
    assert parse_caption("FC1|{bad json}") is None
    assert parse_caption("other") is None


def test_parse_caption_invalid_integrity_values() -> None:
    broken_sha = (
        'FC1|{"folder_path":"A/B","file_key":"k1","part_index":0,"parts_total":1,'
        '"orig_name":"f.bin","sha256":"bad"}'
    )
    parsed = parse_caption(broken_sha)
    assert parsed is not None
    assert parsed.sha256 is None


def test_batch_blob_caption_roundtrip() -> None:
    from televault.core.types import BatchBlobCaption

    meta = BatchBlobCaption(
        version=2,
        kind="tgccm_batch_blob",
        folder_path="Anime/Cache",
        blob_key="blob123abc999",
        orig_name="batch.zip",
        members_count=7,
        manifest_sha256="b" * 64,
    )
    caption = build_batch_blob_caption(meta)
    parsed = parse_batch_blob_caption(caption, prefix="FC1|")
    assert parsed == meta


def test_parse_caption_ignores_batch_blob_kind() -> None:
    from televault.core.types import BatchBlobCaption

    caption = build_batch_blob_caption(
        BatchBlobCaption(
            version=2,
            kind="tgccm_batch_blob",
            folder_path="F",
            blob_key="k",
            orig_name="b.zip",
            members_count=2,
        )
    )
    assert parse_caption(caption) is None
