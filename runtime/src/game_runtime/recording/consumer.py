"""监督录制的有界交接。采集端只入队复制件，写盘和留存由独立任务处理。"""

import asyncio
import functools
import logging
from collections import deque
from typing import Final, Literal

from game_runtime._threads import in_thread
from game_runtime.audio_ring import AudioRead
from game_runtime.frame_ring import RingFrame
from game_runtime.recording.layout import RecordingMeta
from game_runtime.recording.retention import QuotaKeeper, QuotaStatus, StoredRecording
from game_runtime.recording.writer import RecordingWriter

logger = logging.getLogger(__name__)

QUOTA_INTERVAL: Final = 5.0

type Interruption = Literal["error", "quota"]


def _log_removal(recording: StoredRecording) -> None:
    logger.info(
        "超出录制配额，删除最旧的录制 %s（%s，%d 字节）",
        recording.path,
        recording.state,
        recording.size,
    )


def _log_removal_failure(recording: StoredRecording) -> None:
    logger.warning("没能删干净 %s，剩余 %d 字节仍计入用量", recording.path, recording.size)


def check_recording_quota(quota: QuotaKeeper) -> QuotaStatus:
    """在线程中调用，供开录预检与录制期间的检查共用。"""
    return quota.check(_log_removal, failed=_log_removal_failure)


class RecordingConsumer:
    """在同一事件循环里交接复制出的帧和声音。容量指等待写入的件数，另有一件正在写入。

    调用方先核对本游戏的配额，再创建 writer 和本消费者。消费者用该配额守卫保护当前目录。
    撤权和停核由调用方完成，之后才等录制收尾。
    """

    def __init__(self, writer: RecordingWriter, quota: QuotaKeeper, *, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("录制交接容量须为正整数")
        loop = asyncio.get_running_loop()
        self._writer = writer
        self._quota = quota
        self._capacity = capacity
        self._pending = deque[RingFrame | AudioRead]()
        self._wake = asyncio.Event()
        self._closed = asyncio.Event()
        self._interruption: asyncio.Future[Interruption] = loop.create_future()
        self._failure: Exception | None = None
        self._finish_task: asyncio.Task[RecordingMeta] | None = None
        quota.set_current(writer.path)
        self._writing = loop.create_task(self._write(), name="监督录制写入")
        self._retention = loop.create_task(self._watch_quota(), name="监督录制配额")

    async def accept_frame(self, frame: RingFrame) -> None:
        """可直接作为定序帧源的 sink，只做非阻塞交接。中断通过 `wait_interruption` 上报。"""
        self._offer(frame)

    def offer_audio(self, read: AudioRead) -> bool:
        """交接复制出的音频块。收下时返回真，调用方可推进读环水位。"""
        return self._offer(read)

    def _offer(self, data: RingFrame | AudioRead) -> bool:
        if self._closed.is_set():
            return False
        if len(self._pending) >= self._capacity:
            self._interrupt("error", BufferError("录制交接队列已满"))
            return False
        self._pending.append(data)
        self._wake.set()
        return True

    async def wait_interruption(self) -> Interruption:
        """等待第一次录制中断，供宿主结束当前验收。取消某个等待者后，其他等待者仍可收取。"""
        return await asyncio.shield(self._interruption)

    @property
    def interruption(self) -> Interruption | None:
        """第一次中断的原因。宿主可以立即停止授权，然后异步收尾。"""
        return self._interruption.result() if self._interruption.done() else None

    def fail(self, error: Exception) -> None:
        """宿主的采集或停机路径失败，录制须按失败收尾。"""
        self._interrupt("error", error)

    def close_input(self) -> None:
        """停止接收复制件并唤醒消费者，立即返回。"""
        self._closed.set()
        self._wake.set()

    def _interrupt(self, reason: Interruption, error: Exception | None = None) -> None:
        if self._failure is None:
            self._failure = error
        self.close_input()
        if not self._interruption.done():
            self._interruption.set_result(reason)

    async def _write(self) -> None:
        try:
            while True:
                if self._pending:
                    data = self._pending.popleft()
                    if isinstance(data, RingFrame):
                        await self._writer.write_frame(data)
                        if not self._pending or not isinstance(self._pending[0], RingFrame):
                            await self._writer.flush_frames()
                    else:
                        await in_thread(functools.partial(self._writer.write_audio, data))
                elif self._closed.is_set():
                    return
                else:
                    self._wake.clear()
                    await self._wake.wait()
        except Exception as error:
            # writer 已遇到错误，继续给它提交会破坏或阻塞这一段录制。
            self._pending.clear()
            self._interrupt("error", error)

    async def _watch_quota(self) -> None:
        check = functools.partial(check_recording_quota, self._quota)
        try:
            while not self._closed.is_set():
                try:
                    await asyncio.wait_for(self._closed.wait(), QUOTA_INTERVAL)
                    return
                except TimeoutError:
                    pass
                status = await in_thread(check)
                if status.over:
                    self._interrupt("quota")
                    return
        except Exception as error:
            self._interrupt("error", error)

    async def finish(self, *, stop_reason: str, reader_overflows: int = 0) -> RecordingMeta:
        """停核后调用，等已接纳的复制件与配额任务收尾，再写结束元数据。"""
        self.close_input()
        task = self._finish_task
        if task is None or task.done():
            task = self._finish_task = asyncio.create_task(
                self._finish(stop_reason, reader_overflows), name="监督录制收尾"
            )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.wait((task,))
                except asyncio.CancelledError:
                    continue
            if not task.cancelled():
                task.exception()
            raise

    async def _finish(self, stop_reason: str, reader_overflows: int) -> RecordingMeta:
        await self._writing
        await self._retention
        if self._failure is not None:
            stop_reason = "error"
            logger.error("录制 %s 中断：%s", self._writer.path, self._failure)
        elif self._interruption.done():
            stop_reason = self._interruption.result()
        try:
            return await self._writer.finish(
                stop_reason=stop_reason,
                reader_overflows=reader_overflows,
                state="failed" if stop_reason == "error" else None,
            )
        except Exception as error:
            logger.exception("录制 %s 收尾失败", self._writer.path)
            self._interrupt("error", error)
            raise
