"""帧源：读画面的插件取帧的统一接口。实时运行是定序帧源（`game_runtime.frames.sequenced`），
测试用内存帧源，回放是录制的回放帧源（`game_runtime.recording.replay`）。

设计见 `docs/modules/perception.md` 的“取帧”。帧沿用帧环的 `RingFrame`。
"""

import asyncio
import bisect
from typing import Protocol

from game_runtime.frame_ring import RingFrame
from game_runtime.frame_ring.ring import BYTES_PER_PIXEL


class FrameSource(Protocol):
    """帧源：给出一个确定的帧序列，每个读者按顺序逐帧读到，不跳过。帧源不记读者的进度，每个读者
    自己记下读到的帧号。"""

    async def next_frame(self, after: int) -> RingFrame | None:
        """等到帧号大于 `after` 的一帧并返回；帧源结束后返回 None。"""
        ...


def check_frame(frame: RingFrame) -> None:
    """帧本身合规则：帧号为正，宽高为正，行跨度不小于宽 × 4，像素放得下 `行跨度 × 高`。"""
    if frame.frame_number < 1:
        raise ValueError(f"帧号须为正数：{frame.frame_number}")
    if frame.width < 1 or frame.height < 1:
        raise ValueError(f"帧的尺寸须为正数：{frame.width}×{frame.height}")
    if frame.row_stride < frame.width * BYTES_PER_PIXEL:
        raise ValueError(f"行跨度 {frame.row_stride} 小于宽 {frame.width} × 4")
    if len(frame.pixels) < frame.row_stride * frame.height:
        raise ValueError(
            f"像素只有 {len(frame.pixels)} 字节，少于 {frame.row_stride} × {frame.height}"
        )


class MemoryFrameSource:
    """内存帧源，供测试：`push` 依次放入帧号递增的帧，每个读者按顺序逐帧读到，不跳过；
    `close` 之后读完即结束。只在事件循环的线程里使用。"""

    def __init__(self) -> None:
        self._frames: list[RingFrame] = []
        self._numbers: list[int] = []
        self._closed = False
        self._waiters: set[asyncio.Future[None]] = set()

    @property
    def frames(self) -> tuple[RingFrame, ...]:
        """已放入的帧。"""
        return tuple(self._frames)

    @property
    def closed(self) -> bool:
        return self._closed

    def push(self, frame: RingFrame) -> None:
        """放入一帧；帧号须大于已放入的帧。"""
        if self._closed:
            raise RuntimeError("内存帧源已关闭")
        check_frame(frame)
        if self._numbers and frame.frame_number <= self._numbers[-1]:
            raise ValueError(f"帧号须递增：{frame.frame_number} 不大于 {self._numbers[-1]}")
        self._frames.append(frame)
        self._numbers.append(frame.frame_number)
        self._wake()

    def close(self) -> None:
        """不再放入帧；读者读完已放入的帧后得到 None。可以重复调用。"""
        self._closed = True
        self._wake()

    async def next_frame(self, after: int) -> RingFrame | None:
        while True:
            index = bisect.bisect_right(self._numbers, after)
            if index < len(self._frames):
                return self._frames[index]
            if self._closed:
                return None
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.add(waiter)
            try:
                await waiter
            finally:
                self._waiters.discard(waiter)

    def _wake(self) -> None:
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)
