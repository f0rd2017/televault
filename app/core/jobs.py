from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
import logging
import threading
from typing import Any, Awaitable, Callable

from app.core.types import JobEvent, JobStatus

logger = logging.getLogger(__name__)


class JobCancelledError(RuntimeError):
    pass


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise JobCancelledError("Job was cancelled")


@dataclass(slots=True)
class JobContext:
    job_id: int
    job_type: str
    payload: dict[str, Any]
    cancel_token: CancelToken
    report_progress: Callable[[float, str], Awaitable[None]]
    log: Callable[[str], Awaitable[None]]


Runner = Callable[[JobContext], Awaitable[Any]]
Subscriber = Callable[[JobEvent], Any]


class JobManager:
    """Manages job execution with lane-based scheduling and resource management."""
    def __init__(
        self,
        parallelism: int = 1,
        *,
        lane_caps: dict[str, int] | None = None,
        lane_weights: dict[str, int] | None = None,
    ) -> None:
        self._parallelism = max(1, int(parallelism))
        self._queue: asyncio.Queue[tuple[int, str, dict[str, Any], Runner]] = asyncio.Queue()
        self._subscribers: list[Subscriber] = []
        self._tokens: dict[int, CancelToken] = {}
        self._active_job_tasks: dict[int, asyncio.Task[Any]] = {}
        self._next_id = 1
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._callback_tasks: set[asyncio.Task[None]] = set()
        self._stopped = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._state_lock = threading.Lock()
        self._lane_caps: dict[str, int] = {
            str(name): max(1, int(value))
            for name, value in (lane_caps or {}).items()
            if str(name).strip()
        }
        self._lane_weights: dict[str, int] = {
            str(name): max(1, int(value))
            for name, value in (lane_weights or {}).items()
            if str(name).strip()
        }
        if "default" not in self._lane_weights:
            self._lane_weights["default"] = 1
        self._lane_active: dict[str, int] = defaultdict(int)
        self._lane_pending: dict[str, int] = defaultdict(int)
        self._lane_credits: dict[str, int] = {}

    def subscribe(self, callback: Subscriber) -> None:
        self._subscribers.append(callback)

    def start(self) -> None:
        if self._worker_tasks:
            return
        with self._state_lock:
            self._loop = asyncio.get_running_loop()
        logger.info("JobManager start: parallelism=%d", self._parallelism)
        for worker_idx in range(self._parallelism):
            task = asyncio.create_task(
                self._worker(worker_idx),
                name=f"job-manager-worker-{worker_idx + 1}",
            )
            self._worker_tasks.append(task)

    async def stop(self) -> None:
        self._stopped = True
        with self._state_lock:
            loop = self._loop
            tokens = list(self._tokens.values())
            active_tasks = [
                task for task in self._active_job_tasks.values() if not task.done()
            ]
        if self._worker_tasks:
            logger.info(
                "JobManager stop requested: running_workers=%d queued=%d active_tokens=%d",
                len(self._worker_tasks),
                self._queue.qsize(),
                len(self._tokens),
            )
            for token in tokens:
                token.cancel()
            for task in active_tasks:
                if loop is not None and loop.is_running():
                    try:
                        loop.call_soon(task.cancel)
                    except RuntimeError:
                        task.cancel()
                else:
                    task.cancel()

            # Cancel pending jobs that workers did not start yet.
            while True:
                try:
                    job_id, job_type, payload, _runner = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                lane = str(payload.get("_lane") or "default").strip() or "default"
                self._emit(
                    JobEvent(
                        job_id=job_id,
                        job_type=job_type,
                        status=JobStatus.CANCELLED,
                        progress=0.0,
                        payload=payload,
                    )
                )
                self._tokens.pop(job_id, None)
                self._lane_pending[lane] = max(0, int(self._lane_pending.get(lane, 0)) - 1)
                self._queue.task_done()

            for task in self._worker_tasks:
                task.cancel()
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
            self._worker_tasks.clear()
        with self._state_lock:
            self._active_job_tasks.clear()
            self._loop = None

        if self._callback_tasks:
            await asyncio.gather(*tuple(self._callback_tasks), return_exceptions=True)
            self._callback_tasks.clear()
        logger.info("JobManager stopped")

    async def __aenter__(self):
        """Enter async context manager."""
        self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context manager and clean up resources."""
        await self.stop()
        return False

    def enqueue(self, job_type: str, payload: dict[str, Any], runner: Runner) -> int:
        if self._stopped:
            raise RuntimeError("Job manager is stopped")
        self.start()
        job_id = self._next_id
        self._next_id += 1
        with self._state_lock:
            self._tokens[job_id] = CancelToken()
            lane = str(payload.get("_lane") or "default").strip() or "default"
            if lane not in self._lane_weights:
                self._lane_weights[lane] = 1
                self._lane_credits = {}
            self._lane_pending[lane] = int(self._lane_pending.get(lane, 0)) + 1
        self._queue.put_nowait((job_id, job_type, payload, runner))
        self._emit(
            JobEvent(
                job_id=job_id,
                job_type=job_type,
                status=JobStatus.QUEUED,
                progress=0.0,
                payload=payload,
            )
        )
        logger.info(
            "Job enqueued: id=%d type=%s queue_size=%d active_tokens=%d",
            job_id,
            job_type,
            self._queue.qsize(),
            len(self._tokens),
        )
        return job_id

    def cancel(self, job_id: int) -> bool:
        with self._state_lock:
            token = self._tokens.get(job_id)
            active_task = self._active_job_tasks.get(job_id)
            loop = self._loop
        if token is None:
            # UI can race with terminal events and send cancel for an already-finished job.
            logger.debug("Cancel ignored: unknown job id=%d", job_id)
            return False
        token.cancel()
        if active_task is not None and not active_task.done():
            if loop is not None and loop.is_running():
                try:
                    loop.call_soon_threadsafe(active_task.cancel)
                except RuntimeError:
                    active_task.cancel()
            else:
                active_task.cancel()
        logger.info("Cancel requested for job id=%d", job_id)
        return True

    def _emit(self, event: JobEvent) -> None:
        if not self._subscribers:
            return

        async_results: list[Awaitable[object]] = []
        for callback in tuple(self._subscribers):
            try:
                result = callback(event)
            except Exception as e:
                logger.warning("Error in job event callback: %s", str(e))
                continue
            if asyncio.iscoroutine(result):
                async_results.append(result)

        if not async_results:
            return

        async def _wait_async_callbacks() -> None:
            for result in async_results:
                try:
                    await result
                except Exception as e:
                    logger.warning("Error awaiting job event callback: %s", str(e))
                    continue

        task = asyncio.create_task(_wait_async_callbacks())
        self._callback_tasks.add(task)
        task.add_done_callback(self._callback_tasks.discard)

    async def _handle_job_execution(self, job_id: int, job_type: str, payload: dict[str, Any], runner: Runner, token: CancelToken, lane: str, worker_idx: int) -> None:
        """Handle the execution of a single job."""
        current_progress = 0.0

        async def report_progress(progress: float, message: str = "") -> None:
            nonlocal current_progress
            current_progress = max(0.0, min(100.0, progress))
            self._emit(
                JobEvent(
                    job_id=job_id,
                    job_type=job_type,
                    status=JobStatus.RUNNING,
                    progress=current_progress,
                    message=message,
                    payload=payload,
                )
            )

        async def log(message: str) -> None:
            await report_progress(current_progress, message)

        context = JobContext(
            job_id=job_id,
            job_type=job_type,
            payload=payload,
            cancel_token=token,
            report_progress=report_progress,
            log=log,
        )

        self._emit(
            JobEvent(
                job_id=job_id,
                job_type=job_type,
                status=JobStatus.STARTED,
                progress=0.0,
                payload=payload,
            )
        )
        logger.info(
            "Job started: id=%d type=%s worker=%d queue_remaining=%d",
            job_id,
            job_type,
            worker_idx + 1,
            self._queue.qsize(),
        )

        job_task: asyncio.Task[Any] | None = None
        try:
            token.raise_if_cancelled()
            job_task = asyncio.create_task(
                runner(context),
                name=f"job-runner-{job_id}",
            )
            with self._state_lock:
                self._active_job_tasks[job_id] = job_task
            result = await job_task
            token.raise_if_cancelled()
            self._emit(
                JobEvent(
                    job_id=job_id,
                    job_type=job_type,
                    status=JobStatus.DONE,
                    progress=100.0,
                    result=result,
                    payload=payload,
                )
            )
            logger.info("Job done: id=%d type=%s", job_id, job_type)
        except asyncio.CancelledError:
            if job_task is not None and not job_task.done():
                job_task.cancel()
                await asyncio.gather(job_task, return_exceptions=True)
            self._emit(
                JobEvent(
                    job_id=job_id,
                    job_type=job_type,
                    status=JobStatus.CANCELLED,
                    progress=0.0,
                    payload=payload,
                )
            )
            if self._stopped:
                logger.info("Job cancelled by worker shutdown: id=%d type=%s", job_id, job_type)
                raise
            logger.info("Job cancelled: id=%d type=%s", job_id, job_type)
        except JobCancelledError:
            if job_task is not None and not job_task.done():
                job_task.cancel()
                await asyncio.gather(job_task, return_exceptions=True)
            self._emit(
                JobEvent(
                    job_id=job_id,
                    job_type=job_type,
                    status=JobStatus.CANCELLED,
                    progress=0.0,
                    payload=payload,
                )
            )
            logger.info("Job cancelled: id=%d type=%s", job_id, job_type)
        except Exception as exc:
            self._emit(
                JobEvent(
                    job_id=job_id,
                    job_type=job_type,
                    status=JobStatus.ERROR,
                    progress=0.0,
                    error=str(exc),
                    payload=payload,
                )
            )
            logger.exception("Job failed: id=%d type=%s error=%s", job_id, job_type, exc)
        finally:
            with self._state_lock:
                self._active_job_tasks.pop(job_id, None)
                self._tokens.pop(job_id, None)
                self._lane_active[lane] = max(0, int(self._lane_active.get(lane, 0)) - 1)
            self._queue.task_done()

    async def _worker(self, worker_idx: int) -> None:
        logger.info("Job worker started: worker=%d", worker_idx + 1)
        while True:
            job_id, job_type, payload, runner = await self._queue.get()
            lane = str(payload.get("_lane") or "default").strip() or "default"
            
            # Check if job can be processed
            with self._state_lock:
                token = self._tokens.get(job_id)
                can_start = token is not None and self._try_acquire_lane_slot_locked(lane)
                
            if token is None:
                with self._state_lock:
                    self._lane_pending[lane] = max(0, int(self._lane_pending.get(lane, 0)) - 1)
                self._queue.task_done()
                continue
                
            if not can_start:
                self._queue.put_nowait((job_id, job_type, payload, runner))
                self._queue.task_done()
                await asyncio.sleep(0.01)
                continue
                
            # Decrement pending count
            with self._state_lock:
                self._lane_pending[lane] = max(0, int(self._lane_pending.get(lane, 0)) - 1)
                
            # Execute the job
            await self._handle_job_execution(job_id, job_type, payload, runner, token, lane, worker_idx)

    def _try_acquire_lane_slot_locked(self, lane: str) -> bool:
        normalized_lane = str(lane or "default").strip() or "default"
        lane_cap = int(self._lane_caps.get(normalized_lane, self._parallelism))
        active_now = int(self._lane_active.get(normalized_lane, 0))
        if active_now >= lane_cap:
            return False

        if not self._lane_weights:
            self._lane_active[normalized_lane] = active_now + 1
            return True

        if not self._lane_credits:
            self._reset_lane_credits_locked()

        available_other_lanes = any(
            int(self._lane_pending.get(name, 0)) > 0
            and int(self._lane_active.get(name, 0)) < int(self._lane_caps.get(name, self._parallelism))
            for name in self._lane_weights
            if name != normalized_lane
        )
        lane_credit = int(self._lane_credits.get(normalized_lane, 0))
        if lane_credit <= 0 and available_other_lanes:
            has_pending_credit = any(
                int(self._lane_pending.get(name, 0)) > 0 and int(self._lane_credits.get(name, 0)) > 0
                for name in self._lane_weights
            )
            if not has_pending_credit:
                self._reset_lane_credits_locked()
                lane_credit = int(self._lane_credits.get(normalized_lane, 0))
            if lane_credit <= 0:
                return False

        self._lane_credits[normalized_lane] = max(0, lane_credit - 1)
        has_pending_credit = any(
            int(self._lane_pending.get(name, 0)) > 0 and int(self._lane_credits.get(name, 0)) > 0
            for name in self._lane_weights
        )
        if not has_pending_credit:
            self._reset_lane_credits_locked()
        self._lane_active[normalized_lane] = active_now + 1
        return True

    def _reset_lane_credits_locked(self) -> None:
        self._lane_credits = {
            str(name): max(1, int(weight))
            for name, weight in self._lane_weights.items()
            if str(name).strip()
        }
