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
    """Сгенерировать короткое тестовое видео через ffmpeg (lavfi testsrc)."""
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
    # Битый/несуществующий путь → None.
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
    # Миниатюра проставлена и отдаётся в DecorationRole.
    idx = model.index(0, 0)
    deco = model.data(idx, Qt.ItemDataRole.DecorationRole)
    assert isinstance(deco, QIcon) and not deco.isNull()
    refreshed = model.item_for_index(idx)
    assert refreshed.thumbnail is not None
    # Дисковый кэш записан → переживёт рестарт.
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
    # Не-картинка → шаг ничего не строит.
    assert model.refresh_thumbnails_step(max_items=8) is False
    assert model.item_for_index(model.index(0, 0)).thumbnail is None


def test_set_thumbnail_from_path(tmp_path):
    _app()
    png = _write_png(tmp_path / "remote.png")
    model = ExplorerGridModel(thumb_cache_dir=str(tmp_path / ".thumb_cache"))
    # Нескачанная картинка (local_exists=False) — миниатюру ставим из временного файла.
    item = ExplorerFileItem(
        entry=_entry("remote.png", key="k3"), local_path=None, local_exists=False
    )
    model.set_items([item])
    assert model.set_thumbnail_from_path("Photos", "k3", png) is True
    assert model.item_for_index(model.index(0, 0)).thumbnail is not None


def test_clear_dir_files(tmp_path):
    from app.core.utils import clear_dir_files

    d = tmp_path / "fetch"
    d.mkdir()
    (d / "a.bin").write_bytes(b"x")
    (d / "b.bin").write_bytes(b"y")
    assert clear_dir_files(d) == 2
    assert list(d.iterdir()) == []
    # Несуществующая папка — 0, без ошибки.
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
        # Разводим mtime, чтобы порядок был детерминирован (старые — меньший i).
        os.utime(f, (time.time() + i, time.time() + i))
    # Оставляем 2 новейших → удаляем 3 старейших.
    assert evict_dir_to_limit(d, max_files=2) == 3
    remaining = sorted(p.name for p in d.iterdir())
    assert remaining == ["3.png", "4.png"]
    # Под лимитом — ничего не трогаем.
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
    # Только нескачанная картинка без кэша.
    assert keys == ["ka"]


# ── Видео-постеры (инкремент 4) ──────────────────────────────────────────────


def test_is_video_name():
    assert is_video_name("clip.MP4")
    assert is_video_name("movie.mkv")
    assert is_video_name("a.webm")
    assert not is_video_name("pic.png")
    assert not is_video_name("note.txt")
    assert not is_video_name("noext")


def test_video_rows_needing_poster(tmp_path):
    _app()
    model = ExplorerGridModel(thumb_cache_dir=str(tmp_path / ".thumb_cache"))
    # Скачанное видео без постера → кандидат.
    local_vid = ExplorerFileItem(
        entry=_entry("v.mp4", key="kv"), local_path="x", local_exists=True
    )
    # Нескачанное видео → НЕ кандидат (тянуть ради кадра дорого).
    remote_vid = ExplorerFileItem(
        entry=_entry("r.mp4", key="kr"), local_path=None, local_exists=False
    )
    # Скачанная картинка → НЕ видео-кандидат.
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
    # Построенный постер — валидная картинка → из него строится миниатюра.
    icon = make_thumbnail_icon(str(out), size=58)
    assert isinstance(icon, QIcon) and not icon.isNull()


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")
def test_extract_video_poster_seek_past_end_falls_back(tmp_path):
    # Видео короче seek_sec → фолбэк на первый кадр, всё равно успех.
    vid = _write_test_video(tmp_path / "short.mp4", duration=1)
    out = tmp_path / "p.png"
    assert extract_video_poster_png(vid, out, box=96, seek_sec=10.0) is True
    assert out.is_file() and out.stat().st_size > 0


def test_extract_video_poster_bad_input(tmp_path):
    # Не-видео / отсутствующий файл → False, без исключений.
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
    # Постер ещё не построен → шаг ничего не строит синхронно (ffmpeg в фоне).
    assert model.refresh_thumbnails_step(max_items=8) is False
    assert model.item_for_index(model.index(0, 0)).thumbnail is None
    # Имитируем готовый постер из фона: set_thumbnail_from_path кладёт в кэш.
    png = _write_png(tmp_path / "frame.png")
    assert model.set_thumbnail_from_path("Photos", "kvd", png) is True
    assert any(cache.glob("*.png"))
