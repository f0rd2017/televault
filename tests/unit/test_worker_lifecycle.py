from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.core.types import AppConfig, CryptoConfig, RetryConfig
from app.core.worker import TelegramWorker
from app.db.database import connect_db
from app.db.repo import DbRepo


class _RunningLoop:
    def __init__(self) -> None:
        self._callbacks: list[object] = []

    def is_running(self) -> bool:
        return True

    def call_soon_threadsafe(self, callback) -> None:
        self._callbacks.append(callback)
        callback()


class _StopEvent:
    def __init__(self) -> None:
        self.called = 0

    def set(self) -> None:
        self.called += 1


def _build_worker(tmp_path: Path) -> TelegramWorker:
    config = AppConfig(
        tg_api_id=1,
        tg_api_hash="x",
        tg_session_path=str(tmp_path / "data" / "session.session"),
        cache_dir=str(tmp_path / "cache"),
        retry=RetryConfig(),
        crypto=CryptoConfig(),
    )
    repo = DbRepo(connect_db(tmp_path / "index.sqlite3"))
    return TelegramWorker(config, repo)


def test_submit_job_returns_false_when_not_accepting(tmp_path, monkeypatch) -> None:
    worker = _build_worker(tmp_path)
    loop = _RunningLoop()
    called = {"count": 0}

    def fake_submit(coro, _loop):
        called["count"] += 1
        coro.close()
        return object()

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", fake_submit)
    with worker._state_lock:
        worker._loop = loop
        worker._jobs = object()
        worker._accepting_jobs = False

    assert worker.submit_job("refresh", {"mode": "incremental"}) is False
    assert called["count"] == 0


def test_submit_job_returns_true_only_when_accepting(tmp_path, monkeypatch) -> None:
    worker = _build_worker(tmp_path)
    loop = _RunningLoop()
    called = {"count": 0}

    def fake_submit(coro, _loop):
        called["count"] += 1
        coro.close()
        return object()

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", fake_submit)
    with worker._state_lock:
        worker._loop = loop
        worker._jobs = object()
        worker._accepting_jobs = True

    assert worker.submit_job("refresh", {"mode": "incremental"}) is True
    assert called["count"] == 1


def test_request_restart_is_non_blocking_and_sets_restart_flag(
    tmp_path, monkeypatch
) -> None:
    worker = _build_worker(tmp_path)
    loop = _RunningLoop()
    stop_event = _StopEvent()

    monkeypatch.setattr(worker, "isRunning", lambda: True)
    with worker._state_lock:
        worker._loop = loop
        worker._stop_event = stop_event
        worker._accepting_jobs = True
        worker._restart_requested = False

    started = time.perf_counter()
    worker.request_restart()
    elapsed = time.perf_counter() - started

    assert elapsed < 0.2
    with worker._state_lock:
        assert worker._accepting_jobs is False
        assert worker._restart_requested is True
    assert stop_event.called == 1


def test_finished_hook_restarts_when_requested(tmp_path, monkeypatch) -> None:
    worker = _build_worker(tmp_path)
    started = {"count": 0}

    monkeypatch.setattr(
        worker, "start", lambda: started.__setitem__("count", started["count"] + 1)
    )
    with worker._state_lock:
        worker._restart_requested = True
        worker._accepting_jobs = True
        worker._jobs = object()
        worker._scanner = object()
        worker._uploader = object()
        worker._downloader = object()
        worker._deleter = object()

    worker._on_thread_finished()

    assert started["count"] == 1
    with worker._state_lock:
        assert worker._restart_requested is False
        assert worker._accepting_jobs is False
        assert worker._jobs is None
        assert worker._scanner is None
        assert worker._uploader is None
        assert worker._downloader is None
        assert worker._deleter is None
