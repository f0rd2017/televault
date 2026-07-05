from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QIcon, QImage
from PySide6.QtWidgets import QApplication

import subprocess

import pytest

from app.core.types import ObjectEntry
from app.core.utils import extract_video_poster_png, ffmpeg_available
from app.ui.models_qt import (
    ExplorerFileItem,
    ExplorerGridModel,
    is_image_name,
    is_video_name,
    make_thumbnail_icon,
)
from PySide6.QtCore import Qt


def _write_test_video(path, *, duration: int = 2, size: str = "160x120") -> str:
    """Generate a short test video via ffmpeg (lavfi testsrc)."""
    subprocess.run(  # noqa: S603
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size={size}:rate=10",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    return str(path)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _write_png(path, w: int = 120, h: int = 80) -> str:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("#3366cc"))
    assert img.save(str(path), "PNG")
    return str(path)


def _entry(name: str = "pic.png", *, key: str = "k1", size: int = 1234) -> ObjectEntry:
    return ObjectEntry(
        file_key=key,
        folder_path="Photos",
        orig_name=name,
        parts_total=1,
        have_parts=1,
        status="complete",
        total_size=size,
        last_seen_ts=0,
    )


def test_is_image_name():
    assert is_image_name("a.JPG")
    assert is_image_name("b.png")
    assert is_image_name("c.webp")
    assert not is_image_name("d.txt")
    assert not is_image_name("e.mp4")
    assert not is_image_name("noext")


def test_make_thumbnail_icon(tmp_path):
    _app()
    png = _write_png(tmp_path / "x.png")
    icon = make_thumbnail_icon(png, size=58)
    assert isinstance(icon, QIcon)
    assert not icon.isNull()
    # A broken/nonexistent path → None.
    assert make_thumbnail_icon(str(tmp_path / "nope.png")) is None
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    assert make_thumbnail_icon(str(bad)) is None


def test_refresh_thumbnails_step_builds_and_caches(tmp_path):
    _app()
    png = _write_png(tmp_path / "pic.png")
    cache = tmp_path / ".thumb_cache"
    model = ExplorerGridModel(thumb_cache_dir=str(cache))
    item = ExplorerFileItem(entry=_entry(), local_path=png, local_exists=True)
    model.set_items([item])

    assert model.refresh_thumbnails_step(max_items=8) is True
    # The thumbnail is set and returned in DecorationRole.
    idx = model.index(0, 0)
    deco = model.data(idx, Qt.ItemDataRole.DecorationRole)
    assert isinstance(deco, QIcon) and not deco.isNull()
    refreshed = model.item_for_index(idx)
    assert refreshed.thumbnail is not None
    # The disk cache is written → survives a restart.
    assert any(cache.glob("*.png"))


def test_non_image_gets_no_thumbnail(tmp_path):
    _app()
    txt = tmp_path / "note.txt"
    txt.write_text("hello", encoding="utf-8")
    model = ExplorerGridModel(thumb_cache_dir=str(tmp_path / ".thumb_cache"))
    item = ExplorerFileItem(
        entry=_entry("note.txt", key="k2"), local_path=str(txt), local_exists=True
    )
    model.set_items([item])
    # Not an image → the step builds nothing.
    assert model.refresh_thumbnails_step(max_items=8) is False
    assert model.item_for_index(model.index(0, 0)).thumbnail is None


def test_set_thumbnail_from_path(tmp_path):
    _app()
    png = _write_png(tmp_path / "remote.png")
    model = ExplorerGridModel(thumb_cache_dir=str(tmp_path / ".thumb_cache"))
    # A not-downloaded image (local_exists=False) — set the thumbnail from a temp file.
    item = ExplorerFileItem(
        entry=_entry("remote.png", key="k3"), local_path=None, local_exists=False
    )
    model.set_items([item])
    assert model.set_thumbnail_from_path("Photos", "k3", png) is True
    assert model.item_for_index(model.index(0, 0)).thumbnail is not None


def test_set_items_preserves_thumbnail_across_reload(tmp_path):
    # Regression: downloading a file triggers a reload, which used to rebuild
    # items with thumbnail=None → an already-shown preview disappeared. set_items
    # must carry the ready thumbnail over to the new item of the same object.
    _app()
    png = _write_png(tmp_path / "remote.png")
    model = ExplorerGridModel(thumb_cache_dir=str(tmp_path / ".thumb_cache"))
    model.set_items(
        [ExplorerFileItem(entry=_entry("remote.png", key="k9"), local_exists=False)]
    )
    assert model.set_thumbnail_from_path("Photos", "k9", png) is True
    assert model.item_for_index(model.index(0, 0)).thumbnail is not None

    # Simulate a reload after download: a fresh item (thumbnail=None) of the same
    # object, now local. The preview must persist, not vanish.
    fresh = ExplorerFileItem(
        entry=_entry("remote.png", key="k9"),
        local_path=str(tmp_path / "dl" / "remote.png"),
        local_exists=True,
    )
    model.set_items([fresh])
    carried = model.item_for_index(model.index(0, 0))
    assert carried.thumbnail is not None, (
        "preview must survive the post-download reload"
    )
    assert carried.local_exists is True


def test_folder_download_mark_and_carry_over(tmp_path):
    # A downloaded folder (all files inside are local) is marked with the same
    # "downloaded" badge as files; the mark survives reload and is idempotent.
    _app()
    from app.ui.models_qt import ExplorerFolderItem

    model = ExplorerGridModel(thumb_cache_dir=str(tmp_path / ".thumb_cache"))
    model.set_items([ExplorerFolderItem(name="Sub", path="Photos/Sub")])
    idx = model.index(0, 0)
    plain = model.data(idx, Qt.ItemDataRole.DecorationRole)
    assert isinstance(plain, QIcon) and not plain.isNull()
    assert model.folder_paths() == ["Photos/Sub"]

    assert model.set_folder_downloaded("Photos/Sub", True) is True
    assert model.item_for_index(idx).downloaded is True
    assert "downloaded" in model.data(idx, Qt.ItemDataRole.ToolTipRole)
    # Idempotent — setting the same value again changes nothing.
    assert model.set_folder_downloaded("Photos/Sub", True) is False

    # The mark survives a reload (like file previews).
    model.set_items([ExplorerFolderItem(name="Sub", path="Photos/Sub")])
    assert model.item_for_index(model.index(0, 0)).downloaded is True

    # Clearing the mark works.
    assert model.set_folder_downloaded("Photos/Sub", False) is True
    assert model.item_for_index(model.index(0, 0)).downloaded is False


def test_set_icon_size_drops_stale_size_thumbnail(tmp_path):
    # After an icon-size change, old (different-size) thumbnails must not
    # be carried over — otherwise they'd block the rebuild for the new size.
    _app()
    png = _write_png(tmp_path / "pic.png")
    model = ExplorerGridModel(thumb_cache_dir=str(tmp_path / ".thumb_cache"))
    model.set_items(
        [ExplorerFileItem(entry=_entry(key="ksz"), local_path=png, local_exists=True)]
    )
    assert model.refresh_thumbnails_step(max_items=8) is True
    assert model.item_for_index(model.index(0, 0)).thumbnail is not None

    model.set_icon_size(128)  # resizing clears thumbnails off the items
    model.set_items(
        [ExplorerFileItem(entry=_entry(key="ksz"), local_path=png, local_exists=True)]
    )
    # Nothing carried over — the thumbnail is rebuilt for the new size in a separate step.
    assert model.item_for_index(model.index(0, 0)).thumbnail is None


def test_clear_dir_files(tmp_path):
    from app.core.utils import clear_dir_files

    d = tmp_path / "fetch"
    d.mkdir()
    (d / "a.bin").write_bytes(b"x")
    (d / "b.bin").write_bytes(b"y")
    assert clear_dir_files(d) == 2
    assert list(d.iterdir()) == []
    # A nonexistent folder — 0, no error.
    assert clear_dir_files(tmp_path / "nope") == 0


def test_evict_dir_to_limit(tmp_path):
    import os
    import time

    from app.core.utils import evict_dir_to_limit

    d = tmp_path / "cache"
    d.mkdir()
    for i in range(5):
        f = d / f"{i}.png"
        f.write_bytes(b"x")
        # Spread out mtime so ordering is deterministic (older = smaller i).
        os.utime(f, (time.time() + i, time.time() + i))
    # Keep the 2 newest → delete the 3 oldest.
    assert evict_dir_to_limit(d, max_files=2) == 3
    remaining = sorted(p.name for p in d.iterdir())
    assert remaining == ["3.png", "4.png"]
    # Under the limit — nothing is touched.
    assert evict_dir_to_limit(d, max_files=10) == 0


def test_image_rows_needing_fetch(tmp_path):
    _app()
    model = ExplorerGridModel(thumb_cache_dir=str(tmp_path / ".thumb_cache"))
    img_item = ExplorerFileItem(
        entry=_entry("a.png", key="ka"), local_path=None, local_exists=False
    )
    have_item = ExplorerFileItem(
        entry=_entry("b.png", key="kb"), local_path="x", local_exists=True
    )
    txt_item = ExplorerFileItem(
        entry=_entry("c.txt", key="kc"), local_path=None, local_exists=False
    )
    model.set_items([img_item, have_item, txt_item])
    need = model.image_rows_needing_fetch(max_items=8)
    keys = [it.entry.file_key for it in need]
    # Only a not-downloaded image with no cache.
    assert keys == ["ka"]


# ── Video posters (increment 4) ──────────────────────────────────────────────


def test_is_video_name():
    assert is_video_name("clip.MP4")
    assert is_video_name("movie.mkv")
    assert is_video_name("a.webm")
    # Formats beyond mp4 must also count as video so they get a poster frame
    # in the grid (regression: user reported avi had no preview).
    assert is_video_name("old.avi")
    assert is_video_name("phone.mov")
    assert is_video_name("cam.flv")
    assert is_video_name("rec.wmv")
    assert is_video_name("clip.mpeg")
    assert is_video_name("clip.mpg")
    assert is_video_name("clip.m4v")
    assert is_video_name("clip.3gp")
    assert not is_video_name("pic.png")
    assert not is_video_name("note.txt")
    assert not is_video_name("noext")


def test_video_rows_needing_poster(tmp_path):
    _app()
    model = ExplorerGridModel(thumb_cache_dir=str(tmp_path / ".thumb_cache"))
    # A downloaded video with no poster → a candidate.
    local_vid = ExplorerFileItem(
        entry=_entry("v.mp4", key="kv"), local_path="x", local_exists=True
    )
    # A not-downloaded video → NOT a candidate (pulling it just for a frame is expensive).
    remote_vid = ExplorerFileItem(
        entry=_entry("r.mp4", key="kr"), local_path=None, local_exists=False
    )
    # A downloaded image → NOT a video candidate.
    img = ExplorerFileItem(
        entry=_entry("p.png", key="kp"), local_path="y", local_exists=True
    )
    model.set_items([local_vid, remote_vid, img])
    need = model.video_rows_needing_poster(max_items=8)
    assert [it.entry.file_key for it in need] == ["kv"]


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")
def test_extract_video_poster_png(tmp_path):
    vid = _write_test_video(tmp_path / "clip.mp4")
    out = tmp_path / "poster.png"
    assert extract_video_poster_png(vid, out, box=128) is True
    assert out.is_file() and out.stat().st_size > 0
    # The built poster is a valid image → a thumbnail is built from it.
    icon = make_thumbnail_icon(str(out), size=58)
    assert isinstance(icon, QIcon) and not icon.isNull()


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")
def test_extract_video_poster_png_avi(tmp_path):
    # Regression: avi must get a poster just like mp4. ffmpeg is
    # format-agnostic, so this verifies the whole pipeline treats avi equally.
    _app()  # make_thumbnail_icon builds a QPixmap → needs a QApplication
    vid = _write_test_video(tmp_path / "clip.avi")
    out = tmp_path / "poster_avi.png"
    assert extract_video_poster_png(vid, out, box=128) is True
    assert out.is_file() and out.stat().st_size > 0
    icon = make_thumbnail_icon(str(out), size=58)
    assert isinstance(icon, QIcon) and not icon.isNull()


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")
def test_extract_video_poster_from_prefix_avi_and_mp4(tmp_path):
    # The remote-poster path only pulls the file's FIRST part (a prefix) and
    # runs ffmpeg on it. avi keeps frames inline from the start (index at the
    # end is not needed for a single frame), so a prefix must still decode.
    for ext in ("avi", "mp4"):
        vid = _write_test_video(tmp_path / f"clip.{ext}", duration=3, size="320x240")
        prefix = tmp_path / f"prefix.{ext}.bin"
        prefix.write_bytes(pathlib_read_prefix(vid, 128 * 1024))
        out = tmp_path / f"poster_prefix_{ext}.png"
        # seek_sec=0.0 mirrors _run_video_poster_remote.
        assert extract_video_poster_png(prefix, out, box=128, seek_sec=0.0) is True, ext
        assert out.is_file() and out.stat().st_size > 0


def pathlib_read_prefix(path: str, n: int) -> bytes:
    with open(path, "rb") as f:
        return f.read(n)


def _write_test_video_codec(path, *, codec: str, faststart: bool, duration=4) -> str:
    """Generate an mp4 with the moov atom at the front (faststart) or end."""
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration={duration}:size=640x480:rate=25",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        codec,
    ]
    if faststart:
        cmd += ["-movflags", "+faststart"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True)  # noqa: S603
    return str(path)


def test_write_sparse_head_tail_basic(tmp_path):
    from app.core.utils import write_sparse_head_tail

    head = tmp_path / "head.bin"
    tail = tmp_path / "tail.bin"
    head.write_bytes(b"HEAD")
    tail.write_bytes(b"TAIL")
    out = tmp_path / "sparse.bin"
    # total 20 bytes: HEAD at 0..4, hole 4..16, TAIL at 16..20.
    assert (
        write_sparse_head_tail(out, head, tail, tail_offset=16, total_size=20) is True
    )
    data = out.read_bytes()
    assert len(data) == 20
    assert data[0:4] == b"HEAD"
    assert data[16:20] == b"TAIL"
    assert data[4:16] == b"\x00" * 12  # the gap is a hole (zeros)


def test_write_sparse_head_tail_rejects_bad_geometry(tmp_path):
    from app.core.utils import write_sparse_head_tail

    head = tmp_path / "h.bin"
    tail = tmp_path / "t.bin"
    head.write_bytes(b"HEADHEAD")  # 8 bytes
    tail.write_bytes(b"TAIL")
    # tail_offset (4) < len(head) (8) → overlap → refuse.
    assert (
        write_sparse_head_tail(
            tmp_path / "o1.bin", head, tail, tail_offset=4, total_size=100
        )
        is False
    )
    # tail spilling past total_size → refuse.
    assert (
        write_sparse_head_tail(
            tmp_path / "o2.bin", head, tail, tail_offset=10, total_size=12
        )
        is False
    )
    # Missing input → False, no exception.
    assert (
        write_sparse_head_tail(
            tmp_path / "o3.bin",
            tmp_path / "nope.bin",
            tail,
            tail_offset=10,
            total_size=100,
        )
        is False
    )


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")
def test_remote_poster_fallback_reconstructs_nonfaststart(tmp_path):
    # The real bug: a non-faststart MP4 (often what a ".avi" actually is) keeps
    # moov at the END, so a prefix-only poster fails. Reconstructing a sparse
    # head+tail file (as the worker fallback does) must let ffmpeg decode it.
    from app.core.utils import write_sparse_head_tail

    vid = _write_test_video_codec(
        tmp_path / "movie.mp4", codec="mpeg4", faststart=False
    )
    with open(vid, "rb") as f:
        data = f.read()
    total = len(data)
    # Real parts are ~32 MB, so moov (a few KB at the tail) always lands wholly
    # inside the last part. Pick a part size here that preserves that property
    # while still yielding several parts.
    part = 100 * 1024
    nparts = (total + part - 1) // part

    head_path = tmp_path / "part0.bin"
    head_path.write_bytes(data[0:part])
    last_off = (nparts - 1) * part
    tail_path = tmp_path / "partlast.bin"
    tail_path.write_bytes(data[last_off:])

    # Prefix-only must FAIL for non-faststart (no moov in the head).
    assert (
        extract_video_poster_png(head_path, tmp_path / "pfx.png", seek_sec=0.0) is False
    )

    # Head+tail sparse reconstruction must SUCCEED, including a forward seek
    # (the ~1s frame's samples live in the head), so the poster isn't limited
    # to the black first frame.
    sparse = tmp_path / "sparse.bin"
    assert write_sparse_head_tail(sparse, head_path, tail_path, last_off, total) is True
    out = tmp_path / "poster.png"
    assert extract_video_poster_png(sparse, out, seek_sec=1.0) is True
    assert out.is_file() and out.stat().st_size > 0


def _avg_brightness(png_path) -> float:
    _app()
    from PySide6.QtGui import QImage

    img = QImage(str(png_path))
    assert not img.isNull()
    img = img.convertToFormat(QImage.Format.Format_Grayscale8)
    total = 0
    count = 0
    for y in range(0, img.height(), 6):
        for x in range(0, img.width(), 6):
            total += img.pixelColor(x, y).red()
            count += 1
    return total / max(1, count)


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")
def test_remote_poster_skips_black_intro_frame(tmp_path):
    # Regression: previews were coming out black because the remote poster used
    # the very first frame (a black fade-in). A ~1s seek must land on real
    # content — even through the sparse head+tail reconstruction.
    from app.core.utils import write_sparse_head_tail

    vid = tmp_path / "blackstart.mp4"
    subprocess.run(  # noqa: S603
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:d=1.5:r=25",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=3:size=320x240:rate=25",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "mpeg4",
            str(vid),
        ],
        check=True,
    )
    data = vid.read_bytes()
    total = len(data)
    part = 100 * 1024
    nparts = (total + part - 1) // part
    head_path = tmp_path / "h.bin"
    head_path.write_bytes(data[0:part])
    last_off = (nparts - 1) * part
    tail_path = tmp_path / "t.bin"
    tail_path.write_bytes(data[last_off:])
    sparse = tmp_path / "s.bin"
    assert write_sparse_head_tail(sparse, head_path, tail_path, last_off, total) is True

    black = tmp_path / "black.png"
    assert extract_video_poster_png(sparse, black, seek_sec=0.0) is True
    good = tmp_path / "good.png"
    assert extract_video_poster_png(sparse, good, seek_sec=1.8) is True

    # Frame 0 is (near) black; the 1.8s frame is bright content.
    assert _avg_brightness(black) < 16
    assert _avg_brightness(good) > 60


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")
def test_extract_video_poster_seek_past_end_falls_back(tmp_path):
    # The video is shorter than seek_sec → fall back to the first frame, still a success.
    vid = _write_test_video(tmp_path / "short.mp4", duration=1)
    out = tmp_path / "p.png"
    assert extract_video_poster_png(vid, out, box=96, seek_sec=10.0) is True
    assert out.is_file() and out.stat().st_size > 0


def test_extract_video_poster_bad_input(tmp_path):
    # Non-video / missing file → False, no exceptions.
    assert extract_video_poster_png(tmp_path / "nope.mp4", tmp_path / "o.png") is False
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    assert extract_video_poster_png(bad, tmp_path / "o2.png") is False


def test_video_poster_loads_from_disk_cache(tmp_path):
    _app()
    cache = tmp_path / ".thumb_cache"
    model = ExplorerGridModel(thumb_cache_dir=str(cache))
    entry = _entry("v.mp4", key="kvd")
    item = ExplorerFileItem(entry=entry, local_path="x", local_exists=True)
    model.set_items([item])
    # The poster isn't built yet → the step builds nothing synchronously (ffmpeg in the background).
    assert model.refresh_thumbnails_step(max_items=8) is False
    assert model.item_for_index(model.index(0, 0)).thumbnail is None
    # Simulate a ready poster from the background: set_thumbnail_from_path puts it in the cache.
    png = _write_png(tmp_path / "frame.png")
    assert model.set_thumbnail_from_path("Photos", "kvd", png) is True
    assert any(cache.glob("*.png"))
