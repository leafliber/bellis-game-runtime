"""本机阻塞操作在取消后仍计入调用及进程的并发预算。"""

import asyncio
from collections.abc import Callable
from threading import Event, Lock
from typing import Any

from game_runtime.models.config import Purpose
from game_runtime.models.errors import ModelCallError, ModelFailure

_WORKERS = dict.fromkeys(Purpose, 0)
_LOCK = Lock()


class CallWork:
    def __init__(self, purpose: Purpose, concurrency: int, deadline: float) -> None:
        self.purpose = purpose
        self.deadline = deadline
        self.task: asyncio.Task[Any] | None = None
        self.worker: asyncio.Future[Any] | None = None
        self._concurrency = concurrency

    async def run[T](self, action: Callable[[], T]) -> T:
        loop = asyncio.get_running_loop()
        if loop.time() >= self.deadline:
            raise ModelCallError(ModelFailure.TIMEOUT)
        with _LOCK:
            if _WORKERS[self.purpose] >= self._concurrency:
                raise ModelCallError(ModelFailure.CONCURRENCY_LIMIT)
            _WORKERS[self.purpose] += 1

        cancelled = Event()

        def perform() -> T:
            try:
                if cancelled.is_set():
                    raise ModelCallError(ModelFailure.CANCELLED)
                if loop.time() >= self.deadline:
                    raise ModelCallError(ModelFailure.TIMEOUT)
                return action()
            finally:
                # 在线程里归还名额，旧实例的事件循环已停止时也能收尾。
                self._release()

        try:
            job = loop.run_in_executor(None, perform)
        except BaseException:
            self._release()
            raise
        self.worker = job
        job.add_done_callback(_consume)
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            if job.done():
                self.worker = None

    def _release(self) -> None:
        with _LOCK:
            _WORKERS[self.purpose] -= 1


def _consume[T](job: asyncio.Future[T]) -> None:
    if not job.cancelled():
        job.exception()
