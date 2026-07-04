from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QThread, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog, QListView, QMessageBox

from app.core.types import (
    AppConfig,
    CryptoConfig,
    JobEvent,
    JobStatus,
    ObjectEntry,
    PartRecord,
    RetryConfig,
)
from app.db.database import connect_db
from app.db.repo import DbRepo
from app.ui.dialogs import ConfirmDialog
from app.ui.models_qt import ExplorerFileItem, ExplorerFolderItem
from app.ui.window_main import MainWindow


class MockWorker(QThread):
    job_event = Signal(object)
    ready = Signal()
    fatal_error = Signal(str)
    reconnect_attempt = Signal(int)
    account_pool_status = Signal(object)
    thumbnail_ready = Signal(str, str, str)
    thumbnail_failed = Signal(str, str)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.thumbnail_fetches: list[tuple[str, str, str]] = []
        self.video_poster_builds: list[tuple[str, str, str, str]] = []

    def submit_job(self, job_type, payload) -> bool:
        _ = (job_type, payload)
        return True

    def fetch_thumbnail(self, folder_path, file_key, dest_dir) -> bool:
        self.thumbnail_fetches.append((folder_path, file_key, dest_dir))
        return True

    def build_video_poster(self, folder_path, file_key, src_path, dest_dir) -> bool:
        self.video_poster_builds.append((folder_path, file_key, src_path, dest_dir))
        return True

    def cancel_job(self, job_id: int) -> None:
        pass

    def request_stop(self) -> None:
        pass

    def request_restart(self) -> None:
        pass

    def run(self) -> None:
        pass


def _build_window(
    tmp_path, monkeypatch, config_overrides: dict | None = None
) -> MainWindow:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr("app.ui.window_main.QTimer.singleShot", lambda *_args: None)
    # Suppress system tray in headless testing
    monkeypatch.setattr("app.ui.window_main.QSystemTrayIcon.show", lambda self: None)

    config_payload = {
        "tg_api_id": 1,
        "tg_api_hash": "x",
        "tg_session_path": str(tmp_path / "data" / "session.session"),
        "cache_dir": str(tmp_path / "cache"),
        "retry": RetryConfig(),
        "crypto": CryptoConfig(),
    }
    if config_overrides:
        config_payload.update(config_overrides)

    config = AppConfig(
        **config_payload,
    )
    repo = DbRepo(connect_db(tmp_path / "index.sqlite3"))
    worker = MockWorker()
    window = MainWindow(
        config=config,
        repo=repo,
        worker=worker,
        save_config_callback=lambda _: None,
    )
    # Suppress the close dialog in tests
    window.closeEvent = lambda event: event.accept()
    window.show()
    app.processEvents()
    return window


def test_upload_enabled_only_inside_folder(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        assert not window.action_upload.isEnabled()

        window.repo.upsert_folder("A/B")
        window.reload_all()
        window._set_current_folder("A/B", push_history=True, sync_tree=True)
        app.processEvents()

        assert window.action_upload.isEnabled()
    finally:
        window.close()


def test_drop_in_root_shows_warning(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    called = {"value": False}

    def _fake_warning(*_args, **_kwargs):
        called["value"] = True
        return QMessageBox.Ok

    monkeypatch.setattr(QMessageBox, "warning", _fake_warning)
    try:
        window._set_current_folder(None, push_history=True, sync_tree=True)
        window._on_files_dropped([str(tmp_path / "a.bin")])
        assert called["value"] is True
    finally:
        window.close()


def test_drag_out_not_cached_starts_download(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=1,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="abc123abc123",
                part_index=0,
                parts_total=1,
                orig_name="x.bin",
                file_size=10,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"abc123abc123","part_index":0,"parts_total":1,"orig_name":"x.bin"}',
                date_ts=100,
            )
        )
        window.repo.rebuild_objects_aggregates()

        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        assert window.explorer_model.rowCount() == 1
        window.explorer_view.setCurrentIndex(window.explorer_model.index(0, 0))

        called = {"for_export": False}

        def _fake_enqueue(entry, for_export: bool) -> None:
            called["for_export"] = for_export

        monkeypatch.setattr(window, "_enqueue_download_entry", _fake_enqueue)
        exported_paths = window._provide_export_paths_for_drag()

        assert exported_paths is None
        assert called["for_export"] is True
        assert "queued 1 download" in window.progress_widget.logs.toPlainText().lower()
    finally:
        window.close()


def test_running_events_are_coalesced_in_ui_flush(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    try:
        window._active_jobs.add(77)

        for value in (10.0, 18.0, 27.0):
            window._on_job_event(
                JobEvent(
                    job_id=77,
                    job_type="download",
                    status=JobStatus.RUNNING,
                    progress=value,
                    message=f"Downloading {int(value)}%",
                    payload={"mode": "x"},
                )
            )

        assert 77 in window._pending_running_events
        window._flush_pending_running_event(force=True)

        # Only the last (27%) event should be reflected in the UI
        assert window.progress_widget.progress.value() == 27
    finally:
        window.close()


def test_global_progress_is_weighted_by_transfer_bytes(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    try:
        payload_small = {"_ui_request_id": "req-small", "_ui_total_bytes": 1}
        payload_large = {"_ui_request_id": "req-large", "_ui_total_bytes": 99}

        window._on_job_event(
            JobEvent(
                job_id=501,
                job_type="upload",
                status=JobStatus.QUEUED,
                progress=0.0,
                payload=payload_small,
            )
        )
        window._on_job_event(
            JobEvent(
                job_id=502,
                job_type="upload",
                status=JobStatus.QUEUED,
                progress=0.0,
                payload=payload_large,
            )
        )
        window._on_job_event(
            JobEvent(
                job_id=501,
                job_type="upload",
                status=JobStatus.RUNNING,
                progress=100.0,
                message="Uploading 100%",
                payload=payload_small,
            )
        )
        window._flush_pending_running_event(force=True)

        # Weighted by bytes: (100*1 + 0*99) / (1+99) = 1%
        assert window.progress_widget.progress.value() == 1
    finally:
        window.close()


def test_global_progress_keeps_completed_transfer_weight(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    try:
        payload_a = {"_ui_request_id": "req-a", "_ui_total_bytes": 50}
        payload_b = {"_ui_request_id": "req-b", "_ui_total_bytes": 50}

        window._on_job_event(
            JobEvent(
                job_id=701,
                job_type="upload",
                status=JobStatus.QUEUED,
                progress=0.0,
                payload=payload_a,
            )
        )
        window._on_job_event(
            JobEvent(
                job_id=702,
                job_type="upload",
                status=JobStatus.QUEUED,
                progress=0.0,
                payload=payload_b,
            )
        )
        window._on_job_event(
            JobEvent(
                job_id=701,
                job_type="upload",
                status=JobStatus.RUNNING,
                progress=100.0,
                message="Uploading 100%",
                payload=payload_a,
            )
        )
        window._flush_pending_running_event(force=True)
        window._on_job_event(
            JobEvent(
                job_id=701,
                job_type="upload",
                status=JobStatus.DONE,
                progress=100.0,
                payload=payload_a,
                result={"ok": True},
            )
        )

        # One of two equal-sized transfer jobs is complete => 50% global progress.
        assert window.progress_widget.progress.value() == 50
    finally:
        window.close()


def test_global_progress_includes_pending_upload_bytes(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    try:
        window._pending_upload_jobs.append({"_ui_total_bytes": 90})
        payload_active = {"_ui_request_id": "req-active", "_ui_total_bytes": 10}

        window._on_job_event(
            JobEvent(
                job_id=703,
                job_type="upload",
                status=JobStatus.QUEUED,
                progress=100.0,
                payload=payload_active,
            )
        )

        # 10 bytes done out of 100 total bytes (10 active + 90 pending) => 10%.
        assert window.progress_widget.progress.value() == 10
    finally:
        window.close()


def test_global_progress_status_shows_eta_for_transfer_jobs(
    tmp_path, monkeypatch
) -> None:
    window = _build_window(tmp_path, monkeypatch)
    try:
        payload = {"_ui_request_id": "req-eta", "_ui_total_bytes": 100 * 1024 * 1024}

        window._on_job_event(
            JobEvent(
                job_id=601,
                job_type="upload",
                status=JobStatus.QUEUED,
                progress=0.0,
                payload=payload,
            )
        )
        window._on_job_event(
            JobEvent(
                job_id=601,
                job_type="upload",
                status=JobStatus.RUNNING,
                progress=35.0,
                message="Uploading 35%",
                payload=payload,
            )
        )
        window._flush_pending_running_event(force=True)

        fmt = window.progress_widget.progress.format()
        assert "ETA" in fmt
    finally:
        window.close()


def _parse_mbps(status: str) -> float | None:
    # "Downloading 42% | ETA 20s | 5.0 MB/s" -> 5.0
    if "MB/s" not in status:
        return None
    chunk = status.rsplit("|", 1)[-1].strip()
    return float(chunk.replace("MB/s", "").strip())


def test_eta_speed_is_stable_under_bursty_events(tmp_path, monkeypatch) -> None:
    """Скорость скачивания приходит рваными кусками (часть «падает» целиком за
    один опрос, между кусками тики без прогресса). Оконный оценщик должен
    держать показанную скорость у истинной, а не проваливаться к 0 между
    кусками и не взлетать на их приходе."""
    window = _build_window(tmp_path, monkeypatch)
    try:
        clock = {"t": 1000.0}
        monkeypatch.setattr(
            "app.ui.panels.transfer_ops.time.monotonic", lambda: clock["t"]
        )

        mb = 1024 * 1024
        total = 200.0 * mb
        rate = 5.0 * mb  # истинная скорость 5 MB/s
        state = {"done": 0.0}
        # Притворяемся, что идёт активная download-джоба (для метки активности).
        window._active_jobs = {601}
        window._job_type_by_id = {601: "download"}
        monkeypatch.setattr(
            window,
            "_global_transfer_progress_bytes",
            lambda: (state["done"], total),
        )

        # Неравномерные интервалы между событиями; прогресс «выдаётся» только на
        # части тиков (куски), иначе done не меняется.
        gaps = [0.05, 0.05, 0.7, 0.05, 0.9, 0.1, 0.05, 1.1, 0.3, 0.05, 0.8, 0.05]
        deliver = [1, 0, 1, 0, 1, 1, 0, 1, 0, 0, 1, 0]
        gaps = gaps * 4
        deliver = deliver * 4

        t_start = clock["t"]
        speeds: list[float] = []
        for gap, give in zip(gaps, deliver):
            clock["t"] += gap
            if give:
                # done выходит на истинную линию rate*elapsed (кусок целиком).
                state["done"] = min(total, rate * (clock["t"] - t_start))
            status = window._build_global_progress_status()
            elapsed = clock["t"] - t_start
            mbps = _parse_mbps(status or "")
            # Собираем только после прогрева окна (>= _ETA_WINDOW_SEC).
            if elapsed >= window._ETA_WINDOW_SEC and mbps is not None:
                speeds.append(mbps)

        assert len(speeds) >= 5
        # Все цифры в разумном коридоре вокруг истинных 5 MB/s (старый per-call
        # EMA проваливался к ~0 на тиках без прогресса).
        assert all(3.0 <= s <= 7.0 for s in speeds), speeds
        # Соседние показания не скачут резко.
        max_jump = max(abs(b - a) for a, b in zip(speeds, speeds[1:]))
        assert max_jump < 1.5, (max_jump, speeds)
    finally:
        window.close()


def test_startup_overlay_waits_for_initial_load(tmp_path, monkeypatch) -> None:
    """Стартовый экран загрузки должен прятаться не сразу при подключении к
    Telegram, а после завершения первичной сверки (джобы с _ui_initial_load)."""
    window = _build_window(tmp_path, monkeypatch)
    try:
        # Подключились к Telegram — оверлей НЕ должен прятаться немедленно.
        window._on_worker_ready()
        assert window._startup_overlay_done is False

        # Не-стартовая джоба завершилась — оверлей всё ещё держится.
        window._on_job_event(
            JobEvent(
                job_id=10,
                job_type="refresh",
                status=JobStatus.DONE,
                progress=100.0,
                payload={"_ui_request_id": "req-other"},
            )
        )
        assert window._startup_overlay_done is False

        # Первичная сверка завершилась — теперь экран загрузки прячется.
        window._on_job_event(
            JobEvent(
                job_id=11,
                job_type="refresh",
                status=JobStatus.DONE,
                progress=100.0,
                payload={"_ui_request_id": "req-init", "_ui_initial_load": True},
            )
        )
        assert window._startup_overlay_done is True
    finally:
        window.close()


def test_startup_overlay_finishes_on_initial_load_error(tmp_path, monkeypatch) -> None:
    """Даже если первичная сверка упала с ошибкой — экран загрузки прячется
    (подключение есть, не оставляем пользователя на сплэше)."""
    window = _build_window(tmp_path, monkeypatch)
    try:
        # Гасим модальный диалог ошибки: иначе его QTimer выстрелит позже и
        # заблокирует event loop полного прогона на QMessageBox.critical.
        monkeypatch.setattr(window, "_queue_error_dialog", lambda *a, **k: None)
        window._on_worker_ready()
        assert window._startup_overlay_done is False
        window._on_job_event(
            JobEvent(
                job_id=12,
                job_type="refresh",
                status=JobStatus.ERROR,
                progress=0.0,
                error="boom",
                payload={"_ui_request_id": "req-init", "_ui_initial_load": True},
            )
        )
        assert window._startup_overlay_done is True
    finally:
        window.close()


def _img_object_entry(name="pic.png", key="imgk"):
    return ObjectEntry(
        file_key=key,
        folder_path="Photos",
        orig_name=name,
        parts_total=1,
        have_parts=1,
        status="complete",
        total_size=2048,
        last_seen_ts=0,
    )


def test_thumbnail_fetch_enqueue_and_ready(tmp_path, monkeypatch) -> None:
    """Нескачанная картинка ставится в фоновую дозагрузку; по готовности
    миниатюра проставляется, временный файл удаляется, inflight очищается."""
    from PySide6.QtGui import QColor, QImage

    window = _build_window(tmp_path, monkeypatch)
    try:
        item = ExplorerFileItem(
            entry=_img_object_entry(), local_path=None, local_exists=False
        )
        window.explorer_model.set_items([item])

        # Очередь дозагрузки: воркер получил запрос на нашу картинку.
        window._enqueue_thumbnail_fetches()
        assert window.worker.thumbnail_fetches == [
            ("Photos", "imgk", window._thumb_fetch_dir)
        ]
        assert ("Photos", "imgk") in window._thumb_fetch_inflight

        # Готовим «скачанный» временный файл и эмитим готовность.
        temp = tmp_path / "tmp_pic.png"
        img = QImage(40, 30, QImage.Format.Format_RGB32)
        img.fill(QColor("#22aa55"))
        img.save(str(temp), "PNG")
        window.worker.thumbnail_ready.emit("Photos", "imgk", str(temp))

        # Миниатюра проставлена, временный файл удалён, inflight пуст.
        applied = window.explorer_model.item_for_index(
            window.explorer_model.index(0, 0)
        )
        assert applied.thumbnail is not None
        assert not temp.exists()
        assert ("Photos", "imgk") not in window._thumb_fetch_inflight
    finally:
        window.close()


def test_thumbnail_fetch_failed_not_retried(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    try:
        item = ExplorerFileItem(
            entry=_img_object_entry(key="bad"), local_path=None, local_exists=False
        )
        window.explorer_model.set_items([item])
        window._enqueue_thumbnail_fetches()
        assert len(window.worker.thumbnail_fetches) == 1

        window.worker.thumbnail_failed.emit("Photos", "bad")
        assert ("Photos", "bad") in window._thumb_fetch_failed

        # Повторная попытка не дёргает воркер снова.
        window._enqueue_thumbnail_fetches()
        assert len(window.worker.thumbnail_fetches) == 1
    finally:
        window.close()


def test_video_poster_enqueue_and_ready(tmp_path, monkeypatch) -> None:
    """Скачанное видео ставится в фоновое построение постера через ffmpeg;
    по готовности постер проставляется как миниатюра, inflight очищается.
    Нескачанное видео в очередь НЕ попадает."""
    from PySide6.QtGui import QColor, QImage

    window = _build_window(tmp_path, monkeypatch)
    try:
        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"fake-video")
        local = ExplorerFileItem(
            entry=_img_object_entry(name="clip.mp4", key="vidk"),
            local_path=str(vid),
            local_exists=True,
        )
        remote = ExplorerFileItem(
            entry=_img_object_entry(name="far.mp4", key="vidr"),
            local_path=None,
            local_exists=False,
        )
        window.explorer_model.set_items([local, remote])

        window._enqueue_video_posters()
        # Только скачанное видео ушло в построение.
        assert window.worker.video_poster_builds == [
            ("Photos", "vidk", str(vid), window._thumb_fetch_dir)
        ]
        assert ("Photos", "vidk") in window._thumb_fetch_inflight

        # Имитируем готовый кадр-постер из фона.
        poster = tmp_path / "vp_vidk.png"
        img = QImage(48, 36, QImage.Format.Format_RGB32)
        img.fill(QColor("#aa3322"))
        img.save(str(poster), "PNG")
        window.worker.thumbnail_ready.emit("Photos", "vidk", str(poster))

        applied = window.explorer_model.item_for_index(
            window.explorer_model.index(0, 0)
        )
        assert applied.thumbnail is not None
        assert not poster.exists()
        assert ("Photos", "vidk") not in window._thumb_fetch_inflight
    finally:
        window.close()


def test_trash_move_view_and_restore(tmp_path, monkeypatch) -> None:
    """В корзину → файл скрывается из папки и виден в режиме корзины →
    восстановление возвращает его в папку."""
    window = _build_window(tmp_path, monkeypatch)
    try:
        repo = window.repo
        repo.upsert_folder("Docs")
        repo.upsert_msg_part(
            PartRecord(
                msg_id=1,
                chat_id="c",
                folder_path="Docs",
                file_key="k1",
                part_index=0,
                parts_total=1,
                orig_name="f1.txt",
                file_size=10,
                caption_raw="",
                date_ts=1,
            )
        )
        repo.rebuild_objects_aggregates()

        window.current_folder = "Docs"
        window.reload_items()

        def _file_keys():
            keys = set()
            for r in range(window.explorer_model.rowCount()):
                idx = window.explorer_model.index(r, 0)
                if (
                    window.explorer_model.data(idx, Qt.ItemDataRole.UserRole + 1)
                    == "file"
                ):
                    keys.add(window.explorer_model.item_for_index(idx).entry.file_key)
            return keys

        assert _file_keys() == {"k1"}

        # Выделяем и кладём в корзину.
        window.explorer_view.setCurrentIndex(window.explorer_model.index(0, 0))
        window._on_move_to_trash()
        assert _file_keys() == set()  # исчез из папки
        assert repo.count_trash() == 1

        # Включаем режим корзины — файл там.
        window.trash_btn.setChecked(True)
        assert window._trash_view is True
        assert _file_keys() == {"k1"}

        # Восстанавливаем из корзины.
        window.explorer_view.setCurrentIndex(window.explorer_model.index(0, 0))
        window._on_restore_from_trash()
        assert repo.count_trash() == 0
        assert _file_keys() == set()  # корзина теперь пуста

        # Возврат в обычный режим — файл снова в папке.
        window.trash_btn.setChecked(False)
        assert window._trash_view is False
        assert _file_keys() == {"k1"}
    finally:
        window.close()


def test_recursive_search_toggle(tmp_path, monkeypatch) -> None:
    """«Везде» + запрос → поиск по всему поддереву (файлы из вложенных папок);
    выключено → только текущая папка."""
    window = _build_window(tmp_path, monkeypatch)
    try:
        repo = window.repo
        repo.upsert_folder("A")
        repo.upsert_folder("A/B")
        for msg_id, folder, key in ((1, "A", "ka"), (2, "A/B", "kb")):
            repo.upsert_msg_part(
                PartRecord(
                    msg_id=msg_id,
                    chat_id="c1",
                    folder_path=folder,
                    file_key=key,
                    part_index=0,
                    parts_total=1,
                    orig_name="report.txt",
                    file_size=10,
                    caption_raw="",
                    date_ts=msg_id,
                )
            )
        repo.rebuild_objects_aggregates()

        window.current_folder = "A"
        window.search_edit.setText("report")

        # «Везде» выключено → только файл из текущей папки A.
        window.search_everywhere_btn.setChecked(False)
        window.reload_items()
        keys_local = {
            window.explorer_model.item_for_index(
                window.explorer_model.index(r, 0)
            ).entry.file_key
            for r in range(window.explorer_model.rowCount())
            if window.explorer_model.data(
                window.explorer_model.index(r, 0), Qt.ItemDataRole.UserRole + 1
            )
            == "file"
        }
        assert keys_local == {"ka"}

        # «Везде» включено → файлы из A и A/B.
        window.search_everywhere_btn.setChecked(True)
        window.reload_items()
        keys_rec = {
            window.explorer_model.item_for_index(
                window.explorer_model.index(r, 0)
            ).entry.file_key
            for r in range(window.explorer_model.rowCount())
            if window.explorer_model.data(
                window.explorer_model.index(r, 0), Qt.ItemDataRole.UserRole + 1
            )
            == "file"
        }
        assert keys_rec == {"ka", "kb"}
    finally:
        window.close()


def test_terminal_event_flushes_pending_running_state(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    try:
        window._active_jobs.add(91)
        window._inflight_requests.add("req-91")

        window._on_job_event(
            JobEvent(
                job_id=91,
                job_type="download",
                status=JobStatus.RUNNING,
                progress=46.0,
                message="Downloading 46%",
                payload={"mode": "x", "_ui_request_id": "req-91"},
            )
        )
        assert 91 in window._pending_running_events

        window._on_job_event(
            JobEvent(
                job_id=91,
                job_type="download",
                status=JobStatus.DONE,
                progress=100.0,
                payload={"mode": "x", "_ui_request_id": "req-91"},
                result={"ok": True},
            )
        )
        # Terminal event should flush the pending running state
        assert 91 not in window._pending_running_events
        assert 91 not in window._active_jobs
    finally:
        window.close()


def test_done_event_does_not_override_status_when_other_jobs_pending(
    tmp_path, monkeypatch
) -> None:
    window = _build_window(tmp_path, monkeypatch)
    try:
        window._active_jobs.add(501)
        window._job_progress[501] = 67.0
        window._inflight_requests.add("req-501")
        window._inflight_request_meta["req-501"] = {
            "job_type": "upload",
            "small_upload": False,
        }
        window._inflight_requests.add("req-next")
        window._inflight_request_meta["req-next"] = {
            "job_type": "upload",
            "small_upload": False,
        }
        window.progress_widget.set_status_text("Uploading queued")

        window._on_job_event(
            JobEvent(
                job_id=501,
                job_type="upload",
                status=JobStatus.DONE,
                progress=100.0,
                payload={"_ui_request_id": "req-501"},
                result={"ok": True},
            )
        )

        fmt = window.progress_widget.progress.format().lower()
        assert "done" not in fmt
        assert window.progress_widget.cancel_button.isEnabled() is True
    finally:
        window.close()


def test_done_event_logs_transfer_analytics(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    try:
        window._inflight_requests.add("req-analytics")
        window._active_jobs.add(123)
        window._job_progress[123] = 87.0

        window._on_job_event(
            JobEvent(
                job_id=123,
                job_type="download",
                status=JobStatus.DONE,
                progress=100.0,
                payload={"_ui_request_id": "req-analytics"},
                result={
                    "output_path": "x.bin",
                    "analytics": {
                        "phase_seconds": {
                            "total": 2.0,
                            "transfer": 1.7,
                            "network_download": 1.2,
                            "merge": 0.3,
                            "integrity_check": 0.1,
                        },
                        "speed_mbps": {
                            "transfer_output": 12.5,
                            "total_output": 10.4,
                        },
                        "bytes": {
                            "output_total": 1024 * 1024,
                            "resume_completed": 128 * 1024,
                        },
                    },
                },
            )
        )

        log_text = window.progress_widget.logs.toPlainText()
        assert "Analytics [download]:" in log_text
        assert "Speed [download]:" in log_text
        assert "Bytes [download]:" in log_text
    finally:
        window.close()


def test_enqueue_job_submits_even_when_busy(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    submitted = []
    monkeypatch.setattr(
        window.worker, "submit_job", lambda jt, p: submitted.append((jt, p)) or True
    )
    try:
        window._active_jobs.add(1)

        window._enqueue_job("refresh", {"mode": "incremental"})
        assert len(submitted) == 1
        assert submitted[0][0] == "refresh"
        assert "_ui_request_id" in submitted[0][1]
        assert len(window._inflight_requests) == 1
    finally:
        window.close()


def test_enqueue_job_rolls_back_when_worker_not_ready(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    monkeypatch.setattr(window.worker, "submit_job", lambda *_args, **_kwargs: False)
    try:
        window._enqueue_job("refresh", {"mode": "incremental"})
        assert len(window._inflight_requests) == 0
        assert len(window._inflight_request_meta) == 0
        assert not window.progress_widget.cancel_button.isEnabled()
        assert (
            "worker is not ready" in window.progress_widget.logs.toPlainText().lower()
        )
    finally:
        window.close()


def test_enqueue_delete_retries_until_worker_ready(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    calls = {"count": 0}

    def _submit(_job_type, _payload):
        calls["count"] += 1
        return calls["count"] >= 2

    monkeypatch.setattr(window.worker, "submit_job", _submit)
    try:
        window._enqueue_job("delete", {"folder_path": "A/B", "file_key": "abc"})
        assert len(window._pending_enqueue_retries) == 1
        assert len(window._inflight_requests) == 1

        window._process_pending_enqueue_retries(force=True)

        assert calls["count"] == 2
        assert len(window._pending_enqueue_retries) == 0
        assert len(window._inflight_requests) == 1
        assert "after reconnect" in window.progress_widget.logs.toPlainText().lower()
    finally:
        window.close()


def test_enqueue_delete_retry_timeout_rolls_back(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    monkeypatch.setattr(window.worker, "submit_job", lambda *_args, **_kwargs: False)
    window._ENQUEUE_RETRY_MAX_ATTEMPTS = 2
    try:
        window._enqueue_job("delete", {"folder_path": "A/B", "file_key": "abc"})
        assert len(window._pending_enqueue_retries) == 1
        assert len(window._inflight_requests) == 1

        window._process_pending_enqueue_retries(force=True)
        window._process_pending_enqueue_retries(force=True)
        window._process_pending_enqueue_retries(force=True)

        assert len(window._pending_enqueue_retries) == 0
        assert len(window._inflight_requests) == 0
        assert "retry timeout" in window.progress_widget.logs.toPlainText().lower()
    finally:
        window.close()


def test_cancel_clears_pending_enqueue_retries(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    monkeypatch.setattr(window.worker, "submit_job", lambda *_args, **_kwargs: False)
    try:
        window._enqueue_job("delete_folder", {"folder_path": "A/B"})
        assert len(window._pending_enqueue_retries) == 1
        assert len(window._inflight_requests) == 1

        window._on_cancel_job()

        assert len(window._pending_enqueue_retries) == 0
        assert len(window._inflight_requests) == 0
        assert (
            "cancelled before enqueue"
            in window.progress_widget.logs.toPlainText().lower()
        )
    finally:
        window.close()


def test_build_pending_upload_jobs_batches_small_files(tmp_path, monkeypatch) -> None:
    window = _build_window(
        tmp_path,
        monkeypatch,
        config_overrides={
            "small_file_batching_enabled": True,
            "small_file_threshold_kb": 512,
            "small_file_batch_target_mb": 1,
        },
    )
    try:
        folder = "A/B"
        p1 = tmp_path / "one.txt"
        p2 = tmp_path / "two.txt"
        p3 = tmp_path / "three.txt"
        p1.write_bytes(b"1" * 100)
        p2.write_bytes(b"2" * 120)
        p3.write_bytes(b"3" * 140)

        jobs, stats = window._build_pending_upload_jobs(
            [(str(p1), folder), (str(p2), folder), (str(p3), folder)],
            source="picker",
        )

        assert len(jobs) == 1
        job = jobs[0]
        assert job["folder_path"] == folder
        assert job.get("_ui_small_upload") is True
        assert job.get("_ui_small_batch") is True
        assert len(job.get("file_paths", [])) == 3
        assert stats["batched_jobs"] == 1
        assert stats["batched_files"] == 3
        assert stats["skipped_files"] == 0
    finally:
        window.close()


def test_build_pending_upload_jobs_sets_total_bytes_for_regular_and_batch(
    tmp_path, monkeypatch
) -> None:
    window = _build_window(
        tmp_path,
        monkeypatch,
        config_overrides={
            "small_file_batching_enabled": True,
            "small_file_threshold_kb": 512,
            "small_file_batch_target_mb": 1,
        },
    )
    try:
        folder = "A/B"
        large = tmp_path / "large.bin"
        large.write_bytes(b"L" * 4096)
        regular_jobs, _regular_stats = window._build_pending_upload_jobs(
            [(str(large), folder)],
            source="picker",
        )
        assert len(regular_jobs) == 1
        assert int(regular_jobs[0].get("_ui_total_bytes", 0)) == 4096

        s1 = tmp_path / "s1.txt"
        s2 = tmp_path / "s2.txt"
        s1.write_bytes(b"A" * 100)
        s2.write_bytes(b"B" * 120)
        batched_jobs, _batch_stats = window._build_pending_upload_jobs(
            [(str(s1), folder), (str(s2), folder)],
            source="picker",
        )
        assert len(batched_jobs) == 1
        assert batched_jobs[0].get("_ui_small_batch") is True
        assert int(batched_jobs[0].get("_ui_total_bytes", 0)) == 220
    finally:
        window.close()


def test_coalesce_small_batches_into_single_session(tmp_path, monkeypatch) -> None:
    window = _build_window(
        tmp_path,
        monkeypatch,
        config_overrides={
            "small_file_batching_enabled": True,
            "small_file_threshold_kb": 512,
            "small_file_batch_target_mb": 1,
            "small_batch_mode": "per_folder",
            "small_batch_max_files": 2,
        },
    )
    try:
        s1 = tmp_path / "s1.txt"
        s2 = tmp_path / "s2.txt"
        s3 = tmp_path / "s3.txt"
        large = tmp_path / "large.bin"
        s1.write_bytes(b"A" * 100)
        s2.write_bytes(b"B" * 120)
        s3.write_bytes(b"C" * 140)
        large.write_bytes(b"L" * (600 * 1024))  # > 512KB threshold → not small

        jobs, _stats = window._build_pending_upload_jobs(
            [
                (str(s1), "A/X"),
                (str(s2), "A/X"),
                (str(s3), "B/Y"),
                (str(large), "B/Y"),
            ],
            source="drop",
        )
        coalesced = window._coalesce_small_batches_into_session(jobs)

        sessions = [j for j in coalesced if j.get("_ui_small_session")]
        large_jobs = [j for j in coalesced if str(j.get("_lane")) == "upload_large"]
        assert len(sessions) == 1
        assert len(large_jobs) == 1  # large file stays its own job
        session = sessions[0]
        assert session["_lane"] == "upload_small"
        # 3 small files → at least 2 batches folded into the one session
        assert len(session["batches"]) >= 2
        total_files = sum(len(b["file_paths"]) for b in session["batches"])
        assert total_files == 3
        assert int(session["_ui_total_bytes"]) == 360
    finally:
        window.close()


def test_coalesce_keeps_single_small_job_unchanged(tmp_path, monkeypatch) -> None:
    window = _build_window(
        tmp_path,
        monkeypatch,
        config_overrides={
            "small_file_batching_enabled": True,
            "small_file_threshold_kb": 512,
            "small_file_batch_target_mb": 1,
        },
    )
    try:
        s1 = tmp_path / "s1.txt"
        s2 = tmp_path / "s2.txt"
        s1.write_bytes(b"A" * 100)
        s2.write_bytes(b"B" * 120)
        jobs, _stats = window._build_pending_upload_jobs(
            [(str(s1), "A/X"), (str(s2), "A/X")],
            source="drop",
        )
        # Single small batch job → not worth a session, returned unchanged.
        coalesced = window._coalesce_small_batches_into_session(jobs)
        assert coalesced == jobs
        assert not any(j.get("_ui_small_session") for j in coalesced)
    finally:
        window.close()


def test_enqueue_download_entry_sets_total_bytes_in_payload(
    tmp_path, monkeypatch
) -> None:
    window = _build_window(tmp_path, monkeypatch)
    submitted: list[tuple[str, dict]] = []

    def _fake_enqueue(job_type: str, payload: dict) -> None:
        submitted.append((job_type, payload))

    monkeypatch.setattr(window, "_enqueue_job", _fake_enqueue)
    try:
        target = ObjectEntry(
            file_key="abc123abc123",
            folder_path="Anime/Cache",
            orig_name="x.bin",
            parts_total=1,
            have_parts=1,
            status="complete",
            total_size=123456,
            last_seen_ts=100,
        )
        window._enqueue_download_entry(target, for_export=False)
        assert len(submitted) == 1
        assert submitted[0][0] == "download"
        assert int(submitted[0][1].get("_ui_total_bytes", 0)) == 123456
    finally:
        window.close()


def test_build_pending_upload_jobs_global_mode_cross_folder(
    tmp_path, monkeypatch
) -> None:
    window = _build_window(
        tmp_path,
        monkeypatch,
        config_overrides={
            "small_file_batching_enabled": True,
            "small_file_threshold_kb": 512,
            "small_file_batch_target_mb": 1,
            "small_batch_mode": "global",
            "small_batch_max_files": 2,
        },
    )
    try:
        p1 = tmp_path / "one.txt"
        p2 = tmp_path / "two.txt"
        p3 = tmp_path / "three.txt"
        p1.write_bytes(b"1" * 100)
        p2.write_bytes(b"2" * 120)
        p3.write_bytes(b"3" * 140)

        jobs, stats = window._build_pending_upload_jobs(
            [
                (str(p1), "A/X"),
                (str(p2), "B/Y"),
                (str(p3), "B/Y"),
            ],
            source="picker",
        )

        assert len(jobs) == 2
        assert jobs[0].get("_ui_small_batch") is True
        assert jobs[0].get("_lane") == "upload_small"
        assert len(jobs[0].get("file_paths", [])) == 2
        assert jobs[0].get("member_folder_paths") == ["A/X", "B/Y"]
        assert jobs[1].get("_ui_small_upload") is True
        assert jobs[1].get("_lane") == "upload_small"
        assert stats["batched_jobs"] == 1
        assert stats["batched_files"] == 2
    finally:
        window.close()


def test_start_next_pending_upload_respects_small_parallel_limit(
    tmp_path, monkeypatch
) -> None:
    window = _build_window(
        tmp_path,
        monkeypatch,
        config_overrides={
            "max_active_jobs": 3,
            "small_upload_parallel_jobs": 1,
        },
    )
    launched: list[dict] = []

    def _fake_enqueue(payload: dict) -> None:
        launched.append(payload)

    monkeypatch.setattr(window, "_enqueue_upload_job", _fake_enqueue)
    try:
        window._pending_upload_jobs = [
            {"file_path": "a.bin", "folder_path": "A", "_ui_small_upload": True},
            {"file_path": "b.bin", "folder_path": "A", "_ui_small_upload": True},
            {"file_path": "large.iso", "folder_path": "A", "_ui_small_upload": False},
        ]

        window._start_next_pending_upload()

        assert len(launched) == 2
        assert bool(launched[0].get("_ui_small_upload")) is True
        assert bool(launched[1].get("_ui_small_upload")) is False
        assert len(window._pending_upload_jobs) == 1
        assert window._pending_upload_jobs[0]["file_path"] == "b.bin"
    finally:
        window.close()


def test_toast_overlay_limits_visible_notifications_to_two(
    tmp_path, monkeypatch
) -> None:
    window = _build_window(tmp_path, monkeypatch)
    try:
        for idx in range(4):
            window._toast_overlay.add_toast(
                f"job-{idx}", cancel_cb=window._on_cancel_job
            )
        assert len(window._toast_overlay._cards) == 2
    finally:
        window.close()


def test_file_item_is_draggable_flag(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=10,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="fff111fff111",
                part_index=0,
                parts_total=1,
                orig_name="drag.bin",
                file_size=12,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"fff111fff111","part_index":0,"parts_total":1,"orig_name":"drag.bin"}',
                date_ts=200,
            )
        )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        idx = window.explorer_model.index(0, 0)
        flags = window.explorer_model.flags(idx)
        assert bool(flags & Qt.ItemFlag.ItemIsDragEnabled)
    finally:
        window.close()


def test_export_paths_provider_uses_passed_index(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        cached_dir = tmp_path / "cache" / "Anime" / "Cache"
        cached_dir.mkdir(parents=True, exist_ok=True)
        cached_file = cached_dir / "cached.bin"
        cached_file.write_bytes(b"ok")

        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=11,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="aaa111aaa111",
                part_index=0,
                parts_total=1,
                orig_name="cached.bin",
                file_size=2,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"aaa111aaa111","part_index":0,"parts_total":1,"orig_name":"cached.bin"}',
                date_ts=201,
            )
        )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        idx = window.explorer_model.index(0, 0)
        paths = window._provide_export_paths_for_drag(idx)
        assert paths is not None
        assert len(paths) == 1
        assert paths[0].endswith("cached.bin")
    finally:
        window.close()


def test_mouse_move_triggers_export_drag(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=12,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="bbb111bbb111",
                part_index=0,
                parts_total=1,
                orig_name="drag2.bin",
                file_size=2,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"bbb111bbb111","part_index":0,"parts_total":1,"orig_name":"drag2.bin"}',
                date_ts=202,
            )
        )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        called = {"count": 0}

        def _fake_start(index):
            called["count"] += 1
            return True

        monkeypatch.setattr(window.explorer_view, "_start_export_drag", _fake_start)

        window.explorer_view._drag_start_pos = QPoint(0, 0)
        window.explorer_view._drag_start_index = window.explorer_model.index(0, 0)

        event = QMouseEvent(
            QMouseEvent.Type.MouseMove,
            QPointF(20, 20),
            QPointF(20, 20),
            QPointF(20, 20),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        window.explorer_view.mouseMoveEvent(event)
        app.processEvents()

        assert called["count"] == 1
    finally:
        window.close()


def test_sync_folder_only_downloads_missing_or_changed(tmp_path, monkeypatch) -> None:
    from app.core.utils import build_safe_output_path

    QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:

        def _obj(name: str, key: str, size: int) -> None:
            window.repo.upsert_msg_part(
                PartRecord(
                    msg_id=hash(key) % 100000,
                    chat_id="1",
                    folder_path="Sync/F",
                    file_key=key,
                    part_index=0,
                    parts_total=1,
                    orig_name=name,
                    file_size=size,
                    caption_raw=(
                        'FC1|{"folder_path":"Sync/F","file_key":"' + key + '",'
                        '"part_index":0,"parts_total":1,"orig_name":"' + name + '"}'
                    ),
                    date_ts=1,
                )
            )

        window.repo.upsert_folder("Sync/F")
        _obj("present.bin", "presentkey01", 5)
        _obj("missing.bin", "missingkey02", 3)
        _obj("changed.bin", "changedkey03", 4)
        window.repo.rebuild_objects_aggregates()

        # present.bin exists locally with matching size → must be skipped.
        present_local = build_safe_output_path(
            window.config.cache_dir, "Sync/F", "present.bin"
        )
        present_local.parent.mkdir(parents=True, exist_ok=True)
        present_local.write_bytes(b"12345")
        # changed.bin exists locally but WRONG size → must be re-downloaded.
        changed_local = build_safe_output_path(
            window.config.cache_dir, "Sync/F", "changed.bin"
        )
        changed_local.parent.mkdir(parents=True, exist_ok=True)
        changed_local.write_bytes(b"XX")

        queued: list[str] = []
        monkeypatch.setattr(
            window,
            "_enqueue_download_entry",
            lambda entry, **k: queued.append(entry.file_key),
        )
        monkeypatch.setattr(window, "_start_batch_tracking", lambda *a, **k: "b1")

        window._sync_folder("Sync/F")

        assert sorted(queued) == ["changedkey03", "missingkey02"]
    finally:
        window.close()


def test_folder_download_groups_batch_members_by_blob(tmp_path, monkeypatch) -> None:
    QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        captured: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            window, "_enqueue_job", lambda jt, payload: captured.append((jt, payload))
        )
        monkeypatch.setattr(window, "_start_batch_tracking", lambda *a, **k: "batch1")

        def _member(name: str, key: str, blob: str) -> ObjectEntry:
            return ObjectEntry(
                file_key=key,
                folder_path="Anime/Cache",
                orig_name=name,
                parts_total=1,
                have_parts=1,
                status="complete",
                total_size=10,
                last_seen_ts=1,
                storage_kind="batch_member",
                blob_key=blob,
            )

        regular = ObjectEntry(
            file_key="reg1reg1reg1",
            folder_path="Anime/Cache",
            orig_name="big.bin",
            parts_total=1,
            have_parts=1,
            status="complete",
            total_size=999,
            last_seen_ts=1,
            storage_kind="regular",
        )
        targets = [
            _member("a.txt", "k1k1k1k1k1k1", "blobAAA"),
            _member("b.txt", "k2k2k2k2k2k2", "blobAAA"),
            _member("c.txt", "k3k3k3k3k3k3", "blobBBB"),
            regular,
        ]

        job_count = window._enqueue_download_group(
            targets, fast=False, allow_incomplete=False
        )

        # 2 blobs + 1 regular = 3 jobs (not 4 per-file).
        assert job_count == 3
        assert len(captured) == 3
        blob_jobs = [p for _jt, p in captured if p.get("_download_blob")]
        regular_jobs = [p for _jt, p in captured if not p.get("_download_blob")]
        assert len(blob_jobs) == 2
        assert len(regular_jobs) == 1
        blob_a = next(p for p in blob_jobs if p["blob_key"] == "blobAAA")
        assert sorted(blob_a["member_file_keys"]) == ["k1k1k1k1k1k1", "k2k2k2k2k2k2"]
    finally:
        window.close()


def test_mass_download_asks_confirmation(tmp_path, monkeypatch) -> None:
    """Тысячи джоб (папка из тысяч обычных файлов) — спрашиваем подтверждение;
    отказ не ставит ни одной джобы. Тихий автосинк не спрашивает."""
    QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        captured: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            window, "_enqueue_job", lambda jt, payload: captured.append((jt, payload))
        )
        monkeypatch.setattr(window, "_start_batch_tracking", lambda *a, **k: "batch1")

        def _regular(i: int) -> ObjectEntry:
            return ObjectEntry(
                file_key=f"key{i:09d}",
                folder_path="Anime/Cache",
                orig_name=f"f{i}.bin",
                parts_total=1,
                have_parts=1,
                status="complete",
                total_size=10,
                last_seen_ts=1,
                storage_kind="regular",
            )

        many = [_regular(i) for i in range(window._MASS_DOWNLOAD_CONFIRM_THRESHOLD)]

        questions: list[str] = []

        def _fake_question(parent, title, text, *a, **k):
            questions.append(text)
            return QMessageBox.StandardButton.No

        import app.ui.panels.transfer_ops as transfer_ops_mod

        monkeypatch.setattr(
            transfer_ops_mod.QMessageBox, "question", staticmethod(_fake_question)
        )

        # Отказ → 0 джоб.
        job_count = window._enqueue_download_group(
            many, fast=False, allow_incomplete=False
        )
        assert job_count == 0
        assert captured == []
        assert len(questions) == 1

        # confirm=False (тихий автосинк) → без вопроса, всё в очереди.
        job_count = window._enqueue_download_group(
            many, fast=False, allow_incomplete=False, confirm=False
        )
        assert job_count == len(many)
        assert len(captured) == len(many)
        assert len(questions) == 1  # вопрос не задавался повторно

        # Согласие → джобы ставятся.
        captured.clear()
        monkeypatch.setattr(
            transfer_ops_mod.QMessageBox,
            "question",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
        )
        job_count = window._enqueue_download_group(
            many, fast=False, allow_incomplete=False
        )
        assert job_count == len(many)
        assert len(captured) == len(many)
    finally:
        window.close()


def test_double_click_file_does_not_download(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=14,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="ddd111ddd111",
                part_index=0,
                parts_total=1,
                orig_name="bulk_a.bin",
                file_size=2,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"ddd111ddd111","part_index":0,"parts_total":1,"orig_name":"bulk_a.bin"}',
                date_ts=204,
            )
        )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        from app.ui.models_qt import ExplorerFileItem

        downloads: list[object] = []
        monkeypatch.setattr(window, "_on_download", lambda *a, **k: downloads.append(a))

        file_index = None
        for row in range(window.explorer_model.rowCount()):
            idx = window.explorer_model.index(row, 0)
            if isinstance(window.explorer_model.item_for_index(idx), ExplorerFileItem):
                file_index = idx
                break
        assert file_index is not None
        window._on_item_activated(file_index)
        app.processEvents()

        assert downloads == []  # double-click must NOT trigger a download
    finally:
        window.close()


def test_download_shortcut_enqueues_selected(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=14,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="ddd111ddd111",
                part_index=0,
                parts_total=1,
                orig_name="bulk_a.bin",
                file_size=2,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"ddd111ddd111","part_index":0,"parts_total":1,"orig_name":"bulk_a.bin"}',
                date_ts=204,
            )
        )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()
        window.explorer_view.selectAll()
        app.processEvents()

        queued: list[str] = []
        monkeypatch.setattr(
            window,
            "_enqueue_download_entry",
            lambda entry, **k: queued.append(entry.file_key),
        )
        window._on_download_shortcut()
        app.processEvents()

        assert queued == ["ddd111ddd111"]
    finally:
        window.close()


def test_bulk_download_enqueues_all_selected_files(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=14,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="ddd111ddd111",
                part_index=0,
                parts_total=1,
                orig_name="bulk_a.bin",
                file_size=2,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"ddd111ddd111","part_index":0,"parts_total":1,"orig_name":"bulk_a.bin"}',
                date_ts=204,
            )
        )
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=15,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="eee111eee111",
                part_index=0,
                parts_total=1,
                orig_name="bulk_b.bin",
                file_size=2,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"eee111eee111","part_index":0,"parts_total":1,"orig_name":"bulk_b.bin"}',
                date_ts=205,
            )
        )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        window.explorer_view.selectAll()
        app.processEvents()

        queued: list[str] = []

        def _fake_enqueue(
            entry,
            for_export: bool,
            fast: bool = False,
            allow_incomplete_override=None,
            batch_id=None,
        ):
            _ = (for_export, fast, allow_incomplete_override, batch_id)
            queued.append(entry.file_key)

        monkeypatch.setattr(window, "_enqueue_download_entry", _fake_enqueue)
        window._on_download()
        app.processEvents()

        assert sorted(queued) == ["ddd111ddd111", "eee111eee111"]
    finally:
        window.close()


def test_download_folder_enqueues_recursive_files(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Root")
        window.repo.upsert_folder("Root/Sub")
        for msg_id, folder, file_key, name in (
            (340, "Root", "fld111fld111", "root.bin"),
            (341, "Root/Sub", "fld222fld222", "nested.bin"),
        ):
            window.repo.upsert_msg_part(
                PartRecord(
                    msg_id=msg_id,
                    chat_id="1",
                    folder_path=folder,
                    file_key=file_key,
                    part_index=0,
                    parts_total=1,
                    orig_name=name,
                    file_size=2,
                    caption_raw=(
                        f'FC1|{{"folder_path":"{folder}","file_key":"{file_key}",'
                        f'"part_index":0,"parts_total":1,"orig_name":"{name}"}}'
                    ),
                    date_ts=msg_id,
                )
            )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        app.processEvents()

        queued: list[tuple[str, bool, str | None]] = []

        def _fake_enqueue(
            entry,
            for_export: bool,
            fast: bool = False,
            allow_incomplete_override=None,
            batch_id=None,
        ):
            _ = allow_incomplete_override
            queued.append((entry.file_key, fast, batch_id))
            assert for_export is False

        monkeypatch.setattr(window, "_enqueue_download_entry", _fake_enqueue)
        window._on_download_folder("Root")
        app.processEvents()

        assert sorted(file_key for file_key, _fast, _batch in queued) == [
            "fld111fld111",
            "fld222fld222",
        ]
        batch_ids = {batch_id for _file_key, _fast, batch_id in queued}
        assert len(batch_ids) == 1
        assert None not in batch_ids
    finally:
        window.close()


def test_bulk_delete_remote_enqueues_jobs_for_selected_files(
    tmp_path, monkeypatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        for msg_id, file_key, name in (
            (16, "fff111fff111", "bulk_del_1.bin"),
            (17, "ggg111ggg111", "bulk_del_2.bin"),
        ):
            window.repo.upsert_msg_part(
                PartRecord(
                    msg_id=msg_id,
                    chat_id="1",
                    folder_path="Anime/Cache",
                    file_key=file_key,
                    part_index=0,
                    parts_total=1,
                    orig_name=name,
                    file_size=2,
                    caption_raw=f'FC1|{{"folder_path":"Anime/Cache","file_key":"{file_key}","part_index":0,"parts_total":1,"orig_name":"{name}"}}',
                    date_ts=206 + msg_id,
                )
            )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        window.explorer_view.selectAll()
        app.processEvents()

        monkeypatch.setattr(
            ConfirmDialog,
            "exec",
            lambda *_args, **_kwargs: QDialog.DialogCode.Accepted,
        )
        queued: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            window, "_enqueue_job", lambda jt, payload: queued.append((jt, payload))
        )

        window._on_delete_remote()
        app.processEvents()

        assert len(queued) == 2
        assert queued[0][0] == "delete"
        assert queued[1][0] == "delete"
        keys = [payload["file_key"] for _, payload in queued]
        assert sorted(keys) == ["fff111fff111", "ggg111ggg111"]
    finally:
        window.close()


def test_bulk_delete_local_removes_selected_files(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        names = ["bulk_local_1.bin", "bulk_local_2.bin"]
        keys = ["hhh111hhh111", "iii111iii111"]
        for i, (name, file_key) in enumerate(zip(names, keys, strict=True)):
            window.repo.upsert_msg_part(
                PartRecord(
                    msg_id=18 + i,
                    chat_id="1",
                    folder_path="Anime/Cache",
                    file_key=file_key,
                    part_index=0,
                    parts_total=1,
                    orig_name=name,
                    file_size=2,
                    caption_raw=f'FC1|{{"folder_path":"Anime/Cache","file_key":"{file_key}","part_index":0,"parts_total":1,"orig_name":"{name}"}}',
                    date_ts=230 + i,
                )
            )
        window.repo.rebuild_objects_aggregates()

        cache_dir = tmp_path / "cache" / "Anime" / "Cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        file_a = cache_dir / names[0]
        file_b = cache_dir / names[1]
        file_a.write_bytes(b"a")
        file_b.write_bytes(b"b")

        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        window.explorer_view.selectAll()
        app.processEvents()

        monkeypatch.setattr(
            ConfirmDialog,
            "exec",
            lambda *_args, **_kwargs: QDialog.DialogCode.Accepted,
        )
        window._on_delete_local()
        app.processEvents()

        assert not file_a.exists()
        assert not file_b.exists()
    finally:
        window.close()


def test_explorer_view_uses_windows_like_selection_mode(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    try:
        assert (
            window.explorer_view.selectionMode()
            == QListView.SelectionMode.ExtendedSelection
        )
        assert window.explorer_view.isSelectionRectVisible() is True
    finally:
        window.close()


def test_large_folder_uses_lazy_local_presence_probe(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window._EAGER_LOCAL_PRESENCE_LIMIT = 2
        window.repo.upsert_folder("Anime/Cache")
        for i in range(3):
            file_key = f"lazy{i}lazy{i}11"
            file_name = f"lazy_{i}.bin"
            window.repo.upsert_msg_part(
                PartRecord(
                    msg_id=50 + i,
                    chat_id="1",
                    folder_path="Anime/Cache",
                    file_key=file_key,
                    part_index=0,
                    parts_total=1,
                    orig_name=file_name,
                    file_size=2,
                    caption_raw=f'FC1|{{"folder_path":"Anime/Cache","file_key":"{file_key}","part_index":0,"parts_total":1,"orig_name":"{file_name}"}}',
                    date_ts=700 + i,
                )
            )
        window.repo.rebuild_objects_aggregates()

        cached_file = tmp_path / "cache" / "Anime" / "Cache" / "lazy_1.bin"
        cached_file.parent.mkdir(parents=True, exist_ok=True)
        cached_file.write_bytes(b"cached")

        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        idx_cached = None
        for row in range(window.explorer_model.rowCount()):
            idx = window.explorer_model.index(row, 0)
            if (
                window.explorer_model.data(idx, Qt.ItemDataRole.DisplayRole)
                == "lazy_1.bin"
            ):
                idx_cached = idx
                break
        assert idx_cached is not None

        item_before = window.explorer_model.item_for_index(idx_cached)
        assert item_before is not None
        assert item_before.local_exists is False

        window._refresh_visible_local_presence()
        app.processEvents()

        item_after = window.explorer_model.item_for_index(idx_cached)
        assert item_after is not None
        assert item_after.local_exists is True
    finally:
        window.close()


def test_drag_export_returns_multiple_cached_selected_paths(
    tmp_path, monkeypatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        pairs = [
            ("mul111mul111", "multi_a.bin", 900),
            ("mul222mul222", "multi_b.bin", 901),
        ]
        for file_key, name, ts in pairs:
            window.repo.upsert_msg_part(
                PartRecord(
                    msg_id=ts,
                    chat_id="1",
                    folder_path="Anime/Cache",
                    file_key=file_key,
                    part_index=0,
                    parts_total=1,
                    orig_name=name,
                    file_size=2,
                    caption_raw=f'FC1|{{"folder_path":"Anime/Cache","file_key":"{file_key}","part_index":0,"parts_total":1,"orig_name":"{name}"}}',
                    date_ts=ts,
                )
            )
        window.repo.rebuild_objects_aggregates()

        cache_dir = tmp_path / "cache" / "Anime" / "Cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "multi_a.bin").write_bytes(b"a")
        (cache_dir / "multi_b.bin").write_bytes(b"b")

        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        window.explorer_view.selectAll()
        app.processEvents()

        paths = window._provide_export_paths_for_drag()
        assert paths is not None
        assert len(paths) == 2
        assert any(path.endswith("multi_a.bin") for path in paths)
        assert any(path.endswith("multi_b.bin") for path in paths)
    finally:
        window.close()


def test_drag_export_missing_files_uses_batch_download_tracking(
    tmp_path, monkeypatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        for msg_id, file_key, name in (
            (920, "mis111mis111", "missing_a.bin"),
            (921, "mis222mis222", "missing_b.bin"),
        ):
            window.repo.upsert_msg_part(
                PartRecord(
                    msg_id=msg_id,
                    chat_id="1",
                    folder_path="Anime/Cache",
                    file_key=file_key,
                    part_index=0,
                    parts_total=1,
                    orig_name=name,
                    file_size=2,
                    caption_raw=f'FC1|{{"folder_path":"Anime/Cache","file_key":"{file_key}","part_index":0,"parts_total":1,"orig_name":"{name}"}}',
                    date_ts=msg_id,
                )
            )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        window.explorer_view.selectAll()
        app.processEvents()

        queued_batches: list[str | None] = []

        def _fake_enqueue(
            _entry,
            for_export: bool,
            fast: bool = False,
            allow_incomplete_override=None,
            batch_id=None,
        ):
            _ = (for_export, fast, allow_incomplete_override)
            queued_batches.append(batch_id)

        monkeypatch.setattr(window, "_enqueue_download_entry", _fake_enqueue)
        paths = window._provide_export_paths_for_drag()
        app.processEvents()

        assert paths is None
        assert len(queued_batches) == 2
        assert queued_batches[0] is not None
        assert queued_batches[0] == queued_batches[1]
    finally:
        window.close()


def test_multi_download_adds_batch_id_to_payloads(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        for msg_id, file_key, name in (
            (910, "bch111bch111", "batch_1.bin"),
            (911, "bch222bch222", "batch_2.bin"),
        ):
            window.repo.upsert_msg_part(
                PartRecord(
                    msg_id=msg_id,
                    chat_id="1",
                    folder_path="Anime/Cache",
                    file_key=file_key,
                    part_index=0,
                    parts_total=1,
                    orig_name=name,
                    file_size=2,
                    caption_raw=f'FC1|{{"folder_path":"Anime/Cache","file_key":"{file_key}","part_index":0,"parts_total":1,"orig_name":"{name}"}}',
                    date_ts=msg_id,
                )
            )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        window.explorer_view.selectAll()
        app.processEvents()

        payloads: list[dict] = []
        monkeypatch.setattr(
            window, "_enqueue_job", lambda _jt, payload: payloads.append(payload)
        )
        window._on_download()
        app.processEvents()

        assert len(payloads) == 2
        batch_ids = {payload.get("_ui_batch_id") for payload in payloads}
        assert len(batch_ids) == 1
        assert None not in batch_ids
    finally:
        window.close()


def test_transfer_batch_tracking_keeps_individual_toasts(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    try:
        batch_id = window._start_batch_tracking("download", expected_count=3)
        assert batch_id in window._batch_state_by_id
        assert window._should_suppress_individual_toast(batch_id) is False
        # No aggregate batch toast for transfer operations.
        assert batch_id not in window._batch_toast_by_id
    finally:
        window.close()


def test_upload_toast_title_uses_file_name(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    try:
        src = tmp_path / "video_sample.bin"
        src.write_bytes(b"x" * 16)
        captured: dict[str, str] = {}

        class _DummyToast:
            def __init__(self) -> None:
                self.job_id = None

            def set_cancel_callback(self, _cb) -> None:
                return None

            def update_event(self, _event) -> None:
                return None

        monkeypatch.setattr(
            window._toast_overlay,
            "add_toast",
            lambda title, cancel_cb=None: (
                captured.__setitem__("title", title) or _DummyToast()
            ),
        )
        monkeypatch.setattr(window.worker, "submit_job", lambda _jt, _payload: True)

        window._enqueue_job(
            "upload",
            {
                "file_path": str(src),
                "folder_path": "Anime/Cache",
                "source": "picker",
            },
        )

        assert captured.get("title") == "video_sample.bin"
    finally:
        window.close()


def test_error_dialogs_are_grouped(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    called = {"count": 0, "title": "", "text": ""}

    def _fake_critical(_parent, title, text):
        called["count"] += 1
        called["title"] = title
        called["text"] = text
        return QMessageBox.Ok

    monkeypatch.setattr(QMessageBox, "critical", _fake_critical)
    try:
        window._queue_error_dialog(1, "download", "err-a")
        window._queue_error_dialog(2, "upload", "err-b")
        window._flush_error_dialogs()
        assert called["count"] == 1
        assert "Multiple errors" in called["title"]
        assert "2 operations failed" in called["text"]
    finally:
        window.close()


def test_reload_all_is_debounced_after_terminal_events(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        calls = {"count": 0}
        monkeypatch.setattr(
            window, "reload_all", lambda: calls.__setitem__("count", calls["count"] + 1)
        )

        window._inflight_requests.add("a")
        window._active_jobs.add(1)
        window._on_job_event(
            JobEvent(
                job_id=1,
                job_type="download",
                status=JobStatus.DONE,
                payload={"_ui_request_id": "a"},
            )
        )
        window._inflight_requests.add("b")
        window._active_jobs.add(2)
        window._on_job_event(
            JobEvent(
                job_id=2,
                job_type="upload",
                status=JobStatus.DONE,
                payload={"_ui_request_id": "b"},
            )
        )
        assert calls["count"] == 0
        window._perform_scheduled_reload()
        app.processEvents()
        assert calls["count"] == 1
    finally:
        window.close()


def test_watchdog_logs_stalled_jobs(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    try:
        job_id = 77
        window._active_jobs.add(job_id)
        window._running_jobs.add(job_id)
        window._job_last_update_ts[job_id] = 0.0
        monkeypatch.setattr("app.ui.panels.transfer_ops.time.monotonic", lambda: 500.0)
        window._check_stalled_jobs()
        text = window.progress_widget.logs.toPlainText().lower()
        assert "watchdog" in text
        assert "#77" in text
    finally:
        window.close()


def test_watchdog_ignores_non_running_jobs(tmp_path, monkeypatch) -> None:
    window = _build_window(tmp_path, monkeypatch)
    try:
        job_id = 88
        window._active_jobs.add(job_id)
        window._job_last_update_ts[job_id] = 0.0
        monkeypatch.setattr("app.ui.panels.transfer_ops.time.monotonic", lambda: 500.0)
        window._check_stalled_jobs()
        text = window.progress_widget.logs.toPlainText().lower()
        assert "watchdog" not in text
    finally:
        window.close()


def test_backspace_shortcut_deletes_when_file_selected(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("A/B")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=301,
                chat_id="1",
                folder_path="A/B",
                file_key="backspace_del_01",
                part_index=0,
                parts_total=1,
                orig_name="demo.bin",
                file_size=16,
                caption_raw='FC1|{"folder_path":"A/B","file_key":"backspace_del_01","part_index":0,"parts_total":1,"orig_name":"demo.bin"}',
                date_ts=601,
            )
        )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("A/B", push_history=True, sync_tree=True)
        app.processEvents()

        idx = window.explorer_model.index(0, 0)
        window.explorer_view.setCurrentIndex(idx)
        window.explorer_view.selectionModel().select(
            idx,
            window.explorer_view.selectionModel().SelectionFlag.ClearAndSelect,
        )
        window.explorer_view.setFocus()
        app.processEvents()

        delete_calls = {"count": 0}
        monkeypatch.setattr(
            window,
            "_on_delete_remote",
            lambda: delete_calls.__setitem__("count", delete_calls["count"] + 1),
        )

        window._on_delete_shortcut()

        assert delete_calls["count"] == 1
        assert window.current_folder == "A/B"
    finally:
        window.close()


def test_nav_up_shortcut_navigates_up_when_no_file_selected(
    tmp_path, monkeypatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("A/B")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=302,
                chat_id="1",
                folder_path="A/B",
                file_key="backspace_nav_01",
                part_index=0,
                parts_total=1,
                orig_name="demo2.bin",
                file_size=8,
                caption_raw='FC1|{"folder_path":"A/B","file_key":"backspace_nav_01","part_index":0,"parts_total":1,"orig_name":"demo2.bin"}',
                date_ts=602,
            )
        )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("A/B", push_history=True, sync_tree=True)
        app.processEvents()

        selection_model = window.explorer_view.selectionModel()
        selection_model.clearSelection()
        selection_model.clearCurrentIndex()
        window.explorer_view.setFocus()
        app.processEvents()

        delete_calls = {"count": 0}
        monkeypatch.setattr(
            window,
            "_on_delete_remote",
            lambda: delete_calls.__setitem__("count", delete_calls["count"] + 1),
        )

        window._on_nav_up_shortcut()

        assert delete_calls["count"] == 0
        assert window.current_folder == "A"
    finally:
        window.close()


def test_delete_shortcut_ignores_text_input_focus(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=303,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="delete_shortcut_01",
                part_index=0,
                parts_total=1,
                orig_name="delete.bin",
                file_size=10,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"delete_shortcut_01","part_index":0,"parts_total":1,"orig_name":"delete.bin"}',
                date_ts=603,
            )
        )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        idx = window.explorer_model.index(0, 0)
        window.explorer_view.setCurrentIndex(idx)
        window.explorer_view.selectionModel().select(
            idx,
            window.explorer_view.selectionModel().SelectionFlag.ClearAndSelect,
        )
        app.processEvents()

        delete_calls = {"count": 0}
        monkeypatch.setattr(
            window,
            "_on_delete_remote",
            lambda: delete_calls.__setitem__("count", delete_calls["count"] + 1),
        )

        window.search_edit.setFocus()
        app.processEvents()
        window._on_delete_shortcut()
        assert delete_calls["count"] == 0

        window.explorer_view.setFocus()
        app.processEvents()
        window._on_delete_shortcut()
        assert delete_calls["count"] == 1
    finally:
        window.close()


def test_delete_shortcut_not_blocked_by_readonly_pathbar_focus(
    tmp_path, monkeypatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=304,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="delete_shortcut_ro_01",
                part_index=0,
                parts_total=1,
                orig_name="delete_ro.bin",
                file_size=10,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"delete_shortcut_ro_01","part_index":0,"parts_total":1,"orig_name":"delete_ro.bin"}',
                date_ts=604,
            )
        )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        idx = window.explorer_model.index(0, 0)
        window.explorer_view.setCurrentIndex(idx)
        window.explorer_view.selectionModel().select(
            idx,
            window.explorer_view.selectionModel().SelectionFlag.ClearAndSelect,
        )
        app.processEvents()

        delete_calls = {"count": 0}
        monkeypatch.setattr(
            window,
            "_on_delete_remote",
            lambda: delete_calls.__setitem__("count", delete_calls["count"] + 1),
        )

        window.path_bar.setFocus()
        app.processEvents()
        window._on_delete_shortcut()
        assert delete_calls["count"] == 1
    finally:
        window.close()


def test_delete_shortcut_deletes_selected_folder_from_tree(
    tmp_path, monkeypatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=305,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="delete_folder_tree_01",
                part_index=0,
                parts_total=1,
                orig_name="folder.bin",
                file_size=10,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"delete_folder_tree_01","part_index":0,"parts_total":1,"orig_name":"folder.bin"}',
                date_ts=605,
            )
        )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        app.processEvents()

        idx = window.folder_model.find_index_by_path("Anime/Cache")
        assert idx.isValid()
        window.folder_tree.setCurrentIndex(idx)
        selection_model = window.folder_tree.selectionModel()
        selection_model.select(
            idx,
            selection_model.SelectionFlag.ClearAndSelect
            | selection_model.SelectionFlag.Rows,
        )
        window.folder_tree.setFocus()
        app.processEvents()

        delete_remote_calls = {"count": 0}
        folder_calls: list[list[str]] = []

        monkeypatch.setattr(
            window,
            "_on_delete_remote",
            lambda: delete_remote_calls.__setitem__(
                "count", delete_remote_calls["count"] + 1
            ),
        )

        def _fake_delete_folders(paths: list[str]) -> bool:
            folder_calls.append(list(paths))
            return True

        monkeypatch.setattr(
            window, "_confirm_and_enqueue_delete_folders", _fake_delete_folders
        )

        window._on_delete_shortcut()

        assert delete_remote_calls["count"] == 0
        assert folder_calls == [["Anime/Cache"]]
    finally:
        window.close()


def test_delete_shortcut_deletes_selected_folder_from_explorer(
    tmp_path, monkeypatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Root/Sub")
        window.reload_all()
        window._set_current_folder("Root", push_history=True, sync_tree=True)
        app.processEvents()

        folder_index = None
        for row in range(window.explorer_model.rowCount()):
            idx = window.explorer_model.index(row, 0)
            item = window.explorer_model.item_for_index(idx)
            if isinstance(item, ExplorerFolderItem) and item.path == "Root/Sub":
                folder_index = idx
                break
        assert folder_index is not None

        window.explorer_view.setCurrentIndex(folder_index)
        window.explorer_view.selectionModel().select(
            folder_index,
            window.explorer_view.selectionModel().SelectionFlag.ClearAndSelect,
        )
        window.explorer_view.setFocus()
        app.processEvents()

        delete_remote_calls = {"count": 0}
        folder_calls: list[list[str]] = []

        monkeypatch.setattr(
            window,
            "_on_delete_remote",
            lambda: delete_remote_calls.__setitem__(
                "count", delete_remote_calls["count"] + 1
            ),
        )

        def _fake_delete_folders(paths: list[str]) -> bool:
            folder_calls.append(list(paths))
            return True

        monkeypatch.setattr(
            window, "_confirm_and_enqueue_delete_folders", _fake_delete_folders
        )

        window._on_delete_shortcut()

        assert delete_remote_calls["count"] == 0
        assert folder_calls == [["Root/Sub"]]
    finally:
        window.close()


def test_click_empty_area_clears_file_selection(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=13,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="ccc111ccc111",
                part_index=0,
                parts_total=1,
                orig_name="clear_select.bin",
                file_size=2,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"ccc111ccc111","part_index":0,"parts_total":1,"orig_name":"clear_select.bin"}',
                date_ts=203,
            )
        )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        idx = window.explorer_model.index(0, 0)
        window.explorer_view.setCurrentIndex(idx)
        window.explorer_view.selectionModel().select(
            idx,
            window.explorer_view.selectionModel().SelectionFlag.ClearAndSelect,
        )
        app.processEvents()
        assert window.explorer_view.currentIndex().isValid()
        assert window.explorer_view.selectedIndexes()

        blank = window.explorer_view.viewport().rect().bottomRight() - QPoint(6, 6)
        event_press = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(blank),
            QPointF(blank),
            QPointF(window.explorer_view.viewport().mapToGlobal(blank)),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        window.explorer_view.mousePressEvent(event_press)
        event_release = QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease,
            QPointF(blank),
            QPointF(blank),
            QPointF(window.explorer_view.viewport().mapToGlobal(blank)),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        window.explorer_view.mouseReleaseEvent(event_release)
        app.processEvents()

        assert not window.explorer_view.currentIndex().isValid()
        assert not window.explorer_view.selectedIndexes()
    finally:
        window.close()


def test_file_icon_badge_differs_for_cached_state(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=21,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="cached111111",
                part_index=0,
                parts_total=1,
                orig_name="cached_state.bin",
                file_size=10,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"cached111111","part_index":0,"parts_total":1,"orig_name":"cached_state.bin"}',
                date_ts=301,
            )
        )
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=22,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="remote111111",
                part_index=0,
                parts_total=1,
                orig_name="remote_only.bin",
                file_size=10,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"remote111111","part_index":0,"parts_total":1,"orig_name":"remote_only.bin"}',
                date_ts=302,
            )
        )
        window.repo.rebuild_objects_aggregates()

        cached_file = tmp_path / "cache" / "Anime" / "Cache" / "cached_state.bin"
        cached_file.parent.mkdir(parents=True, exist_ok=True)
        cached_file.write_bytes(b"cached")

        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        idx_cached = None
        idx_remote = None
        for row in range(window.explorer_model.rowCount()):
            idx = window.explorer_model.index(row, 0)
            name = window.explorer_model.data(idx, Qt.ItemDataRole.DisplayRole)
            if name == "cached_state.bin":
                idx_cached = idx
            elif name == "remote_only.bin":
                idx_remote = idx

        assert idx_cached is not None
        assert idx_remote is not None

        icon_cached = window.explorer_model.data(
            idx_cached, Qt.ItemDataRole.DecorationRole
        )
        icon_remote = window.explorer_model.data(
            idx_remote, Qt.ItemDataRole.DecorationRole
        )
        assert icon_cached is not None
        assert icon_remote is not None

        image_cached = icon_cached.pixmap(58, 58).toImage()
        image_remote = icon_remote.pixmap(58, 58).toImage()
        # Bottom-right badge area should differ (checkmark vs down-arrow badge color).
        assert image_cached.pixelColor(50, 50) != image_remote.pixelColor(50, 50)
    finally:
        window.close()


def test_file_icons_differ_by_extension(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=23,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="txt111txt111",
                part_index=0,
                parts_total=1,
                orig_name="notes.txt",
                file_size=10,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"txt111txt111","part_index":0,"parts_total":1,"orig_name":"notes.txt"}',
                date_ts=303,
            )
        )
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=24,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="rar111rar111",
                part_index=0,
                parts_total=1,
                orig_name="archive.rar",
                file_size=10,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"rar111rar111","part_index":0,"parts_total":1,"orig_name":"archive.rar"}',
                date_ts=304,
            )
        )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        idx_txt = None
        idx_rar = None
        for row in range(window.explorer_model.rowCount()):
            idx = window.explorer_model.index(row, 0)
            name = window.explorer_model.data(idx, Qt.ItemDataRole.DisplayRole)
            if name == "notes.txt":
                idx_txt = idx
            elif name == "archive.rar":
                idx_rar = idx

        assert idx_txt is not None
        assert idx_rar is not None
        icon_txt = window.explorer_model.data(idx_txt, Qt.ItemDataRole.DecorationRole)
        icon_rar = window.explorer_model.data(idx_rar, Qt.ItemDataRole.DecorationRole)
        assert icon_txt is not None
        assert icon_rar is not None
        assert icon_txt.cacheKey() != icon_rar.cacheKey()
    finally:
        window.close()


def test_popular_extension_icons_are_distinct(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        entries = [
            (
                71,
                "txt222txt222",
                "readme.txt",
                'FC1|{"folder_path":"Anime/Cache","file_key":"txt222txt222","part_index":0,"parts_total":1,"orig_name":"readme.txt"}',
            ),
            (
                72,
                "rar222rar222",
                "bundle.rar",
                'FC1|{"folder_path":"Anime/Cache","file_key":"rar222rar222","part_index":0,"parts_total":1,"orig_name":"bundle.rar"}',
            ),
            (
                73,
                "exe222exe222",
                "installer.exe",
                'FC1|{"folder_path":"Anime/Cache","file_key":"exe222exe222","part_index":0,"parts_total":1,"orig_name":"installer.exe"}',
            ),
        ]
        for msg_id, file_key, name, caption in entries:
            window.repo.upsert_msg_part(
                PartRecord(
                    msg_id=msg_id,
                    chat_id="1",
                    folder_path="Anime/Cache",
                    file_key=file_key,
                    part_index=0,
                    parts_total=1,
                    orig_name=name,
                    file_size=10,
                    caption_raw=caption,
                    date_ts=800 + msg_id,
                )
            )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        icon_keys: dict[str, int] = {}
        for row in range(window.explorer_model.rowCount()):
            idx = window.explorer_model.index(row, 0)
            name = window.explorer_model.data(idx, Qt.ItemDataRole.DisplayRole)
            icon = window.explorer_model.data(idx, Qt.ItemDataRole.DecorationRole)
            if icon is not None and isinstance(name, str):
                icon_keys[name] = icon.cacheKey()

        assert icon_keys["readme.txt"] != icon_keys["bundle.rar"]
        assert icon_keys["readme.txt"] != icon_keys["installer.exe"]
        assert icon_keys["bundle.rar"] != icon_keys["installer.exe"]
    finally:
        window.close()


def test_local_presence_updates_after_file_removed(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=31,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="dyn111dyn111",
                part_index=0,
                parts_total=1,
                orig_name="dynamic.bin",
                file_size=6,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"dyn111dyn111","part_index":0,"parts_total":1,"orig_name":"dynamic.bin"}',
                date_ts=401,
            )
        )
        window.repo.rebuild_objects_aggregates()

        cached_file = tmp_path / "cache" / "Anime" / "Cache" / "dynamic.bin"
        cached_file.parent.mkdir(parents=True, exist_ok=True)
        cached_file.write_bytes(b"cached")

        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        idx = window.explorer_model.index(0, 0)
        item_before = window.explorer_model.item_for_index(idx)
        assert item_before is not None
        assert item_before.local_exists is True

        cached_file.unlink()
        window._refresh_visible_local_presence()
        app.processEvents()

        item_after = window.explorer_model.item_for_index(idx)
        assert item_after is not None
        assert item_after.local_exists is False
    finally:
        window.close()


def test_recent_export_marker_auto_expires(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        clock = {"t": 100.0}
        monkeypatch.setattr("app.ui.models_qt.time.monotonic", lambda: clock["t"])

        window.repo.upsert_folder("Anime/Cache")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=41,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="exp111exp111",
                part_index=0,
                parts_total=1,
                orig_name="exported.bin",
                file_size=6,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"exp111exp111","part_index":0,"parts_total":1,"orig_name":"exported.bin"}',
                date_ts=501,
            )
        )
        window.repo.rebuild_objects_aggregates()

        cached_file = tmp_path / "cache" / "Anime" / "Cache" / "exported.bin"
        cached_file.parent.mkdir(parents=True, exist_ok=True)
        cached_file.write_bytes(b"cached")

        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        idx = window.explorer_model.index(0, 0)
        tip_before = window.explorer_model.data(idx, Qt.ItemDataRole.ToolTipRole)
        assert "Recently exported: no" in tip_before

        window._on_export_success(idx)
        tip_during = window.explorer_model.data(idx, Qt.ItemDataRole.ToolTipRole)
        assert "Recently exported: yes" in tip_during

        clock["t"] = 104.0
        window._refresh_visible_local_presence()
        tip_after = window.explorer_model.data(idx, Qt.ItemDataRole.ToolTipRole)
        assert "Recently exported: no" in tip_after
    finally:
        window.close()


def test_download_transfer_state_changes_badge_and_tooltip(
    tmp_path, monkeypatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=51,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="dl111dl11111",
                part_index=0,
                parts_total=1,
                orig_name="need_download.bin",
                file_size=6,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"dl111dl11111","part_index":0,"parts_total":1,"orig_name":"need_download.bin"}',
                date_ts=601,
            )
        )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        idx = window.explorer_model.index(0, 0)
        tip_idle = window.explorer_model.data(idx, Qt.ItemDataRole.ToolTipRole)
        icon_idle = window.explorer_model.data(idx, Qt.ItemDataRole.DecorationRole)
        assert "Transfer: idle" in tip_idle

        window.explorer_model.set_transfer_state(
            "Anime/Cache", "dl111dl11111", "downloading"
        )
        tip_loading = window.explorer_model.data(idx, Qt.ItemDataRole.ToolTipRole)
        icon_loading = window.explorer_model.data(idx, Qt.ItemDataRole.DecorationRole)
        assert "Transfer: downloading" in tip_loading
        assert icon_idle.cacheKey() != icon_loading.cacheKey()

        window.explorer_model.set_transfer_state("Anime/Cache", "dl111dl11111", None)
        tip_done = window.explorer_model.data(idx, Qt.ItemDataRole.ToolTipRole)
        assert "Transfer: idle" in tip_done
    finally:
        window.close()


def test_loading_badge_animation_changes_icon_by_phase(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        window.repo.upsert_folder("Anime/Cache")
        window.repo.upsert_msg_part(
            PartRecord(
                msg_id=61,
                chat_id="1",
                folder_path="Anime/Cache",
                file_key="anim111anim11",
                part_index=0,
                parts_total=1,
                orig_name="anim.bin",
                file_size=6,
                caption_raw='FC1|{"folder_path":"Anime/Cache","file_key":"anim111anim11","part_index":0,"parts_total":1,"orig_name":"anim.bin"}',
                date_ts=701,
            )
        )
        window.repo.rebuild_objects_aggregates()
        window.reload_all()
        window._set_current_folder("Anime/Cache", push_history=True, sync_tree=True)
        app.processEvents()

        idx = window.explorer_model.index(0, 0)
        window.explorer_model.set_transfer_state(
            "Anime/Cache", "anim111anim11", "downloading"
        )
        icon_phase_0 = window.explorer_model.data(idx, Qt.ItemDataRole.DecorationRole)

        advanced = window.explorer_model.advance_loading_animation()
        assert advanced is True
        icon_phase_1 = window.explorer_model.data(idx, Qt.ItemDataRole.DecorationRole)
        assert icon_phase_0.cacheKey() != icon_phase_1.cacheKey()
    finally:
        window.close()


def test_reconnect_action_requests_restart_and_keeps_ui_responsive(
    tmp_path, monkeypatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = _build_window(tmp_path, monkeypatch)
    try:
        called = {"count": 0}

        def fake_restart() -> None:
            called["count"] += 1

        monkeypatch.setattr(window.worker, "request_restart", fake_restart)
        window.action_reconnect.setEnabled(True)

        window._on_reconnect()
        app.processEvents()

        assert called["count"] == 1
        assert not window.action_reconnect.isEnabled()
        assert (
            "Restarting Telegram connection"
            in window.progress_widget.logs.toPlainText()
        )
        assert "Restarting Telegram connection" in window.statusBar().currentMessage()
    finally:
        window.close()
