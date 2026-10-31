"""帧源：读画面的插件取帧的统一接口，实时运行读帧环，测试用内存帧源，回放另有实现。

设计见 `docs/modules/perception.md` 的“取帧”。帧沿用帧环的 `RingFrame`。
"""

import asyncio
import bisect
from typing import Final, Protocol

from game_runtime._threads import in_thread
from game_runtime.frame_ring import FrameRing, FrameRingError, FrameRingTornError, RingFrame
from game_runtime.frame_ring.ring import BYTES_PER_PIXEL

DEFAULT_POLL_INTERVAL: Final = 0.005
"""实时帧源查看最新帧号的间隔（秒）。"""


class FrameSource(Protocol):
    """帧源。帧源不记读者的进度，每个读者自己记下读到的帧号。"""

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


class RingFrameSource:
    """实时帧源：读主进程的帧环，总给出最新的一帧，读者处理不过来时跳过中间的帧。

    帧环归 `CoreLink` 所有；内核运行期间帧环不关闭（主进程先停内核再停实时核）。帧环关闭后
    `next_frame` 抛 `FrameRingError`。
    """

    def __init__(self, ring: FrameRing, *, poll_interval: float = DEFAULT_POLL_INTERVAL) -> None:
        if not poll_interval > 0:
            raise ValueError(f"poll_interval 须为正数：{poll_interval}")
        self._ring = ring
        self._poll_interval = poll_interval

    async def next_frame(self, after: int) -> RingFrame | None:
        """每隔 `poll_interval` 看一次最新帧号，有新帧时在工作线程里拷出最新一帧。

        每次尝试都读到撕裂的帧时稍后再读。实时帧源不会结束，从不返回 None。
        """
        while True:
            if self._ring.latest_frame_number() > after:
                frame = await in_thread(self._read)
                if isinstance(frame, FrameRingError):
                    raise frame
                if frame is not None and frame.frame_number > after:
                    return frame
            await asyncio.sleep(self._poll_interval)

    def _read(self) -> RingFrame | FrameRingError | None:
        """在工作线程里读最新一帧；撕裂时为 None。

        错误不带工作线程里的调用栈交回：栈帧里有映射上的 ctypes 视图，异常若经线程池的 future
        交回，引用环要等垃圾回收才释放，在那之前帧环关不掉。
        """
        try:
            return self._ring.read_latest()
        except FrameRingTornError:
            return None
        except FrameRingError as error:
            return error.with_traceback(None)


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
