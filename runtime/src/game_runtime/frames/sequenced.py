"""定序帧源：读主进程的帧环，决定接纳哪些帧，接纳的每一帧按顺序交给每个读者。

设计见 `docs/modules/perception.md` 的“定序帧源”。实时运行时读者处理的就是这里接纳的序列；
录制写下同一序列（接收方），回放按录制的顺序给出同样的帧。
"""

import asyncio
import bisect
import dataclasses
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from game_runtime._threads import in_thread
from game_runtime.frame_ring import FrameRing, FrameRingError, FrameRingTornError, RingFrame

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL: Final = 0.02
"""看一次最新帧号的间隔（秒）。不短于 Windows 上事件循环的时钟精度（约 15.6 ms）：更短的定时器
一排上就算到期，几个这样定时的任务会互相唤醒，事件循环不再休眠，空转占满一个核。"""
DEFAULT_MAX_RATE: Final = 10.0
"""每秒至多读帧与接纳的次数。"""
DEFAULT_IDLE_INTERVAL: Final = 0.25
"""内容不变时，距上一接纳帧的采集时刻至少隔这么久（秒）才再接纳一帧。"""
DEFAULT_BACKLOG: Final = 32
"""接纳队列里至多保留的帧数。"""

type FrameSink = Callable[[RingFrame], Awaitable[None]]
"""接收方：收下一帧接纳的帧；泵等它返回才读下一帧，抛错即停止接纳。"""


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    """接纳的参数。"""

    max_rate: float = DEFAULT_MAX_RATE
    """每秒至多读帧与接纳的次数，大于 0。"""
    idle_interval: float = DEFAULT_IDLE_INTERVAL
    """内容不变时再接纳一帧的最短间隔（秒，按采集时刻），不小于 0。"""

    def __post_init__(self) -> None:
        if not (math.isfinite(self.max_rate) and self.max_rate > 0):
            raise ValueError(f"max_rate 须为正数：{self.max_rate}")
        if not (math.isfinite(self.idle_interval) and self.idle_interval >= 0):
            raise ValueError(f"idle_interval 不能为负：{self.idle_interval}")

    @property
    def read_interval_ns(self) -> int:
        """两次读帧之间至少隔的纳秒数（墙钟）。"""
        return round(1e9 / self.max_rate)

    @property
    def idle_interval_ns(self) -> int:
        return round(self.idle_interval * 1e9)


class FrameBacklogError(Exception):
    """读者要的下一帧已移出接纳队列：读者落后超过队列的长度，或开始得太晚。

    读者从 `resume_after` 继续（即从队列里最旧的一帧接着读）；跳过的帧不再给它，此后它的结果与回放
    不一致。
    """

    def __init__(self, after: int, resume_after: int) -> None:
        super().__init__(
            f"帧 {after} 之后的帧已移出接纳队列，可从帧 {resume_after} 之后继续；"
            "跳过的帧不再给出，此后的结果与回放不一致"
        )
        self.after = after
        self.resume_after = resume_after


@dataclass(frozen=True, slots=True)
class SequencerStats:
    admitted: int
    """接纳的帧数。"""
    reads: int
    """读帧的次数（每次读出一帧，不论接纳与否）。"""
    latest_ring_frame: int
    """最近一次看到的帧环最新帧号。"""
    overflows: int
    """积压超限的次数。"""
    ended: int
    """读到结束的次数。"""


@dataclass(frozen=True, slots=True)
class _Read:
    frame: RingFrame
    same_pixels: bool
    """尺寸、行跨度与像素都与上一接纳帧相同。"""


class SequencedFrameSource:
    """定序帧源。`start` 开始接纳，`close` 停止；只在事件循环的线程里使用。

    帧环归 `CoreLink` 所有，在本帧源关闭之前不能关闭。
    """

    def __init__(
        self,
        ring: FrameRing,
        *,
        policy: AdmissionPolicy | None = None,
        backlog: int = DEFAULT_BACKLOG,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        sink: FrameSink | None = None,
    ) -> None:
        if backlog < 1:
            raise ValueError(f"backlog 须为正数：{backlog}")
        if not poll_interval > 0:
            raise ValueError(f"poll_interval 须为正数：{poll_interval}")
        self._ring = ring
        self.policy = policy or AdmissionPolicy()
        self.backlog = backlog
        self._poll_interval = poll_interval
        self._sink = sink
        self._frames: list[RingFrame] = []
        self._numbers: list[int] = []
        self._evicted_through = 0
        """被移出队列的最大帧号。"""
        self._last: RingFrame | None = None
        """上一接纳帧；只由泵改写，读帧的工作线程只读它。"""
        self._admitted = 0
        self._reads = 0
        self._latest_ring = 0
        self._overflows = 0
        self._ended = 0
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._closed = False
        self._failure: BaseException | None = None
        self._waiters: set[asyncio.Future[None]] = set()

    # 状态

    @property
    def closed(self) -> bool:
        """不再接纳；读者读完队列里的帧后得到 None。"""
        return self._closed

    @property
    def failure(self) -> BaseException | None:
        """泵因错误而停止时的错误：读帧环失败或接收方抛错。"""
        return self._failure

    @property
    def done(self) -> asyncio.Task[None] | None:
        """泵的任务；`start` 之前为 None。宿主可以等它，得知接纳已因错误停止。"""
        return self._task

    @property
    def stats(self) -> SequencerStats:
        return SequencerStats(
            admitted=self._admitted,
            reads=self._reads,
            latest_ring_frame=self._latest_ring,
            overflows=self._overflows,
            ended=self._ended,
        )

    # 接纳

    def start(self) -> None:
        """开始接纳。只能调用一次。"""
        if self._task is not None or self._closed:
            raise RuntimeError("定序帧源已经开始或已关闭")
        self._task = asyncio.get_running_loop().create_task(self._pump(), name="定序帧源的泵")

    async def close(self) -> None:
        """停止接纳：泵做完手头的一帧（含等接收方收下）后停下，不取消它。可以重复调用。"""
        self._stopping = True
        if self._task is not None:
            await asyncio.wait((self._task,))
        self._closed = True
        self._wake()

    async def _pump(self) -> None:
        last_read = 0
        next_read_ns = 0
        try:
            while not self._stopping:
                now = time.perf_counter_ns()
                latest = self._ring.latest_frame_number()
                self._latest_ring = max(self._latest_ring, latest)
                if now >= next_read_ns and latest > last_read:
                    read = await in_thread(self._read)
                    if isinstance(read, FrameRingError):
                        raise read
                    if read is not None and read.frame.frame_number > last_read:
                        last_read = read.frame.frame_number
                        next_read_ns = now + self.policy.read_interval_ns
                        self._reads += 1
                        admitted = self._admit(read)
                        if admitted is not None:
                            self._publish(admitted)
                            if self._sink is not None:
                                await self._sink(admitted)
                        continue
                await asyncio.sleep(self._poll_interval)
        except Exception as error:
            self._failure = error
            logger.error("定序帧源停止接纳：%s", error)
        finally:
            self._closed = True
            self._wake()

    def _read(self) -> _Read | FrameRingError | None:
        """在工作线程里读最新一帧并与上一接纳帧比较；撕裂或还没有帧时为 None。

        错误不带工作线程里的调用栈交回，理由同 `RingFrame` 的读取：栈帧里有映射上的视图。
        """
        try:
            frame = self._ring.read_latest()
        except FrameRingTornError:
            return None
        except FrameRingError as error:
            return error.with_traceback(None)
        if frame is None:
            return None
        last = self._last
        same = (
            last is not None
            and (last.width, last.height, last.row_stride)
            == (frame.width, frame.height, frame.row_stride)
            and last.pixels == frame.pixels
        )
        return _Read(frame, same)

    def _admit(self, read: _Read) -> RingFrame | None:
        """按接纳规则决定是否接纳；接纳时返回交给读者的帧。"""
        frame = read.frame
        last = self._last
        if last is not None and read.same_pixels:
            unchanged = frame.transform_version == last.transform_version
            if unchanged and frame.capture_ns - last.capture_ns < self.policy.idle_interval_ns:
                return None
            # 像素相同：与上一接纳帧共用同一个对象，不另占内存。
            frame = dataclasses.replace(frame, pixels=last.pixels)
        self._last = frame
        self._admitted += 1
        return frame

    def _publish(self, frame: RingFrame) -> None:
        self._frames.append(frame)
        self._numbers.append(frame.frame_number)
        if len(self._frames) > self.backlog:
            evicted = self._frames.pop(0)
            self._numbers.pop(0)
            self._evicted_through = evicted.frame_number
        self._wake()

    # 读者

    async def next_frame(self, after: int) -> RingFrame | None:
        """给出接纳序列里帧号大于 `after` 的第一帧；还没有就等。关闭后读完队列即返回 None。

        要的帧已移出队列时抛 `FrameBacklogError`。
        """
        while True:
            if after < self._evicted_through:
                self._overflows += 1
                logger.warning(
                    "读者落后：帧 %d 之后的帧已移出接纳队列，从帧 %d 之后继续",
                    after,
                    self._evicted_through,
                )
                raise FrameBacklogError(after, self._evicted_through)
            index = bisect.bisect_right(self._numbers, after)
            if index < len(self._frames):
                return self._frames[index]
            if self._closed:
                self._ended += 1
                self._wake()
                return None
            await self._wait()

    def queued(self, after: int) -> int:
        """接纳队列里帧号大于 `after` 的帧数，即读到 `after` 的读者还没读的帧；只读运行的状态行
        以此显示积压。"""
        return len(self._numbers) - bisect.bisect_right(self._numbers, after)

    async def wait_ended(self, count: int) -> None:
        """等到读到结束的次数达到 `count`；宿主以此等全部读者处理完已接纳的帧。"""
        while self._ended < count:
            await self._wait()

    async def _wait(self) -> None:
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
