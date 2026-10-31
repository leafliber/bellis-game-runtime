"""普通插件 `voice-activity`：定期从 `ctx.audio` 读新块，逐块交给核心，提供 `voice-activity@1`。

设计见 `docs/modules/perception.md` 的“配音检测”。实时运行时外壳把块号之外喂入核心的事（从哪一块
接着喂、按墙钟发现的中断）作为喂入标记发布，录制记下它们；回放时 `ctx.audio` 是录制的声音
（`RecordedAudio`），外壳不读墙钟，按录下的标记在同样的位置喂入中断。
"""

import asyncio
import functools
import logging
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_runtime._threads import in_thread
from game_runtime.audio_ring import AudioBlock, AudioRead, AudioReader, AudioRingError
from game_runtime.kernel import AUDIO_READ, Manifest, Plugin, PluginContext, ServiceToken, Tier
from game_runtime.local_data import local_data_dir
from game_runtime.voice_activity.detector import SpeechModel, VoiceActivityDetector
from game_runtime.voice_activity.silero import SileroVad
from game_runtime.voice_activity.state import (
    DEFAULT_MIN_SILENCE,
    DEFAULT_MIN_SPEECH,
    DEFAULT_OFFSET,
    DEFAULT_ONSET,
    INITIAL_STATUS,
    UnknownReason,
    VoiceChange,
    VoiceParams,
    VoiceStatus,
)

logger = logging.getLogger(__name__)

READ_WINDOW_NS: Final = 60 * 1_000_000_000
"""每次读取所求的时长：足够长，读到上次之后的全部块（至多音频环里的全部块）。"""

type VoiceListener = Callable[[VoiceChange], None]


@dataclass(frozen=True, slots=True)
class FeedSkip:
    """喂入的下一块不是上一块加一：外壳从 `next_block` 接着喂（启动后的第一块不是 1，或读得太慢、
    其间的块已被覆盖）。"""

    after: int
    """此前喂入的最后一块的块号；还没有喂过为 0。"""
    next_block: int


@dataclass(frozen=True, slots=True)
class FeedInterrupt:
    """外壳在块之间喂入的中断：按墙钟发现的 `no-audio`、`stalled`，或检测出错的 `error`。"""

    after: int
    """此前喂入的最后一块的块号；还没有喂过为 0。"""
    reason: UnknownReason
    at_ns: int


@dataclass(frozen=True, slots=True)
class FeedEnd:
    """记录喂入标记的一方停止记录时外壳喂到的位置；回放喂到这一块为止。外壳不发布它。"""

    after: int


type FeedMark = FeedSkip | FeedInterrupt | FeedEnd
"""喂入核心的序列里块号之外的事，按发生的顺序。块本身按块号从录制里复现。"""

type FeedListener = Callable[[FeedMark], None]


@runtime_checkable
class RecordedAudio(Protocol):
    """回放的声音：录下的块与实时运行时外壳的喂入标记。`ctx.audio` 满足它时外壳按录制喂入。"""

    async def next_blocks(self, after: int) -> AudioRead | None:
        """块号大于 `after` 的下一批块，按块号从小到大；读完后返回 None。"""
        ...

    def voice_marks(self) -> Sequence[FeedMark]:
        """实时运行时外壳发布的喂入标记，按发生的顺序，末尾可以有一个 `FeedEnd`。"""
        ...


class VoiceActivityService(Protocol):
    """`voice-activity@1`。"""

    @property
    def status(self) -> VoiceStatus:
        """当前的状态。"""
        ...

    def subscribe(self, listener: VoiceListener) -> Callable[[], None]:
        """监听者按发生的顺序收到每一条变化，返回取消订阅的函数（可重复调用）。

        监听者在事件循环里同步调用，抛错只记录，不影响其它监听者；要做重活的监听者自己排队。
        订阅方把返回的函数登记为撤销函数。
        """
        ...

    @property
    def fed_block(self) -> int:
        """已喂入核心、变化已发布的最后一块的块号；还没有为 0。"""
        ...

    def subscribe_feed(self, listener: FeedListener) -> Callable[[], None]:
        """监听者按发生的顺序收到每一个喂入标记（`FeedSkip`、`FeedInterrupt`），规则同 `subscribe`。
        录制以此记下块号之外喂入核心的事，回放时在同样的位置喂入。"""
        ...


VOICE_ACTIVITY: Final = ServiceToken[VoiceActivityService]("voice-activity", 1)


class VoiceHub:
    """`voice-activity@1` 的实现：保存当前的状态并分发变化。`publish` 只由外壳调用。"""

    def __init__(self, status: VoiceStatus = INITIAL_STATUS) -> None:
        self._status = status
        self._listeners: list[VoiceListener] = []
        self._feed_listeners: list[FeedListener] = []
        self._fed = 0

    @property
    def status(self) -> VoiceStatus:
        return self._status

    @property
    def fed_block(self) -> int:
        return self._fed

    def subscribe(self, listener: VoiceListener) -> Callable[[], None]:
        return _subscribe(self._listeners, listener)

    def subscribe_feed(self, listener: FeedListener) -> Callable[[], None]:
        return _subscribe(self._feed_listeners, listener)

    def publish(
        self,
        changes: list[VoiceChange],
        status: VoiceStatus,
        *,
        marks: Sequence[FeedMark] = (),
        fed: int | None = None,
    ) -> None:
        """先发布喂入标记，再发布变化；`fed` 是此后已喂入的最后一块。"""
        for mark in marks:
            for listener in list(self._feed_listeners):
                try:
                    listener(mark)
                except Exception:
                    logger.exception("voice-activity@1 的监听者处理喂入标记 %s 时出错", mark)
        if fed is not None:
            self._fed = fed
        self._status = status
        for change in changes:
            for listener in list(self._listeners):
                try:
                    listener(change)
                except Exception:
                    logger.exception("voice-activity@1 的监听者处理变化 %s 时出错", change)


def _subscribe[T](listeners: list[T], listener: T) -> Callable[[], None]:
    entry: list[T] = [listener]
    listeners.append(listener)

    def unsubscribe() -> None:
        if entry:
            listeners.remove(entry.pop())

    return unsubscribe


@dataclass(frozen=True, slots=True)
class _Fed:
    """喂入一次的结果：变化、此后的状态、喂入标记与此后已喂入的最后一块。"""

    changes: list[VoiceChange]
    status: VoiceStatus
    marks: list[FeedMark]
    fed: int


class VoiceRunner:
    """外壳：每隔 `poll_interval` 秒读一次音频环，把新块逐块交给核心，发布状态、变化与喂入标记。

    读音频环在事件循环里（只拷新块，很快；音频环在同一线程上销毁，读与销毁不会并发）；核心在工作
    线程里运行，被取消时先等在途的工作做完。墙钟只用来发现块号不前进，`now_ns` 与采集时刻同一时钟。
    `reader` 是录制的声音（`RecordedAudio`）时不读墙钟，按录下的块与喂入标记喂入，读完即结束。
    """

    def __init__(
        self,
        reader: AudioReader,
        detector: VoiceActivityDetector,
        hub: VoiceHub,
        *,
        poll_interval: float,
        stall_timeout: float,
        now_ns: Callable[[], int] = time.perf_counter_ns,
        name: str = "voice-activity",
    ) -> None:
        if not poll_interval > 0 or not stall_timeout > 0:
            raise ValueError(f"间隔与时限须为正数：{poll_interval}、{stall_timeout}")
        self._reader = reader
        self._detector = detector
        self._hub = hub
        self._poll_interval = poll_interval
        self._stall_ns = round(stall_timeout * 1e9)
        self._now = now_ns
        self._name = name
        self._fed = 0
        """已喂入核心的最后一块的块号，换了音频环也不归零。"""

    async def run(self) -> None:
        """一直运行到被取消；按录制喂入时读完即结束。出错时记录，状态转为未知（`error`）并停止检测。"""
        recorded = isinstance(self._reader, RecordedAudio)
        try:
            if isinstance(self._reader, RecordedAudio):
                await self._recorded(self._reader)
            else:
                await self._loop()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s 检测出错，停止检测", self._name)
            # 按录制喂入时没有“现在”：出错的时刻取已处理到的声音。
            at = (self._detector.status.processed_ns or 0) if recorded else self._now()
            changes = self._detector.interrupt(UnknownReason.ERROR, at)
            mark = FeedInterrupt(self._fed, UnknownReason.ERROR, at)
            self._hub.publish(changes, self._detector.status, marks=[mark])

    async def _loop(self) -> None:
        after = 0
        """已从这个音频环读到的最大块号。"""
        reference = self._now()
        """块号不前进从这一刻算起：最新一块的末尾，还没有块时为开始等的时刻。"""
        failing = False
        while True:
            now = self._now()
            try:
                read = self._reader.read_recent(READ_WINDOW_NS, after=after)
                replaced = read is not None and not read.blocks and self._replaced(after)
            except AudioRingError as error:
                # 撕裂（写方在读的期间绕了一圈）或损坏：这一次不读，稍后再读，恢复前不再重复记录。
                if not failing:
                    logger.warning("%s 读音频环失败，稍后再读：%s", self._name, error)
                    failing = True
                await asyncio.sleep(self._poll_interval)
                continue
            if failing:
                logger.info("%s 恢复读音频环", self._name)
                failing = False
            if replaced:
                after = 0
                continue
            if read is None:
                after = 0
                reference = now
                if self._hub.status.reason is not UnknownReason.NO_AUDIO:
                    await self._interrupt(UnknownReason.NO_AUDIO, now)
            elif read.blocks:
                after = read.blocks[-1].block_number
                last = read.blocks[-1]
                reference = last.capture_ns + read.duration_ns(last.sample_count)
                await self._feed_blocks(read.blocks, read.sample_rate, read.channels)
            elif now - reference > self._stall_ns and (
                self._hub.status.reason is not UnknownReason.STALLED
            ):
                await self._interrupt(UnknownReason.STALLED, now)
            await asyncio.sleep(self._poll_interval)

    async def _recorded(self, source: RecordedAudio) -> None:
        """按录制喂入：块按块号顺序；每个喂入标记在它记下的位置上处理——跳过的块不喂，中断照原样
        喂入，到 `FeedEnd` 或 `error` 中断为止。读完录下的块即结束。"""
        marks = deque(source.voice_marks())
        after = 0
        stopped = False
        while (read := await source.next_blocks(after)) is not None:
            batch: list[AudioBlock] = []
            for block in read.blocks:
                after = block.block_number
                if stopped:
                    continue
                take = True
                while marks and marks[0].after <= (batch[-1].block_number if batch else self._fed):
                    mark = marks[0]
                    if isinstance(mark, FeedSkip):
                        if block.block_number < mark.next_block:
                            take = False
                        else:
                            marks.popleft()
                        break
                    await self._feed_blocks(batch, read.sample_rate, read.channels)
                    batch = []
                    marks.popleft()
                    if isinstance(mark, FeedEnd):
                        stopped, take = True, False
                        break
                    await self._interrupt(mark.reason, mark.at_ns)
                    if mark.reason is UnknownReason.ERROR:
                        stopped, take = True, False
                        break
                if take:
                    batch.append(block)
            await self._feed_blocks(batch, read.sample_rate, read.channels)
        while not stopped and marks and marks[0].after <= self._fed:
            mark = marks.popleft()
            if isinstance(mark, FeedEnd):
                break
            if isinstance(mark, FeedInterrupt):
                await self._interrupt(mark.reason, mark.at_ns)

    def _replaced(self, after: int) -> bool:
        """没有新块时看一眼最新一块：块号比已读的小即换了音频环，要从头读。"""
        latest = self._reader.read_recent(0)
        return latest is not None and bool(latest.blocks) and latest.blocks[-1].block_number < after

    async def _feed_blocks(
        self, blocks: Sequence[AudioBlock], sample_rate: int, channels: int
    ) -> None:
        if not blocks:
            return
        fed = self._fed

        def work() -> _Fed:
            changes: list[VoiceChange] = []
            marks: list[FeedMark] = []
            last = fed
            for block in blocks:
                if block.block_number != last + 1:
                    marks.append(FeedSkip(last, block.block_number))
                changes.extend(
                    self._detector.feed(block, sample_rate=sample_rate, channels=channels)
                )
                last = block.block_number
            return _Fed(changes, self._detector.status, marks, last)

        await self._apply(work)

    async def _interrupt(self, reason: UnknownReason, at_ns: int) -> None:
        fed = self._fed

        def work() -> _Fed:
            changes = self._detector.interrupt(reason, at_ns)
            return _Fed(changes, self._detector.status, [FeedInterrupt(fed, reason, at_ns)], fed)

        await self._apply(work)

    async def _apply(self, work: Callable[[], _Fed]) -> None:
        result = await in_thread(work)
        self._fed = result.fed
        self._hub.publish(result.changes, result.status, marks=result.marks, fed=result.fed)


type _Probability = Annotated[float, Field(gt=0.0, le=1.0)]
type _Seconds = Annotated[float, Field(gt=0.0, le=60.0, allow_inf_nan=False)]


class VoiceActivityConfig(BaseModel):
    """参数的含义见 `VoiceParams` 与 `VoiceRunner`；`data_dir` 缺省为本地数据目录。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: Path | None = None
    """本地数据目录，绝对路径，模型制品在其中；缺省按 `BELLIS_GAME_RUNTIME_DATA` 与平台约定。"""
    onset: _Probability = DEFAULT_ONSET
    offset: _Probability = DEFAULT_OFFSET
    min_speech: _Seconds = DEFAULT_MIN_SPEECH
    min_silence: _Seconds = DEFAULT_MIN_SILENCE
    poll_interval: Annotated[float, Field(gt=0.0, le=1.0, allow_inf_nan=False)] = 0.02
    """读音频环的间隔（秒）。"""
    stall_timeout: _Seconds = 0.5
    """最新一块的末尾早于现在超过这么久（秒）即块号不前进。"""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.data_dir is not None and not self.data_dir.is_absolute():
            raise ValueError("data_dir 须为绝对路径")
        if self.offset > self.onset:
            raise ValueError(f"offset {self.offset} 不能大于 onset {self.onset}")
        return self

    def params(self) -> VoiceParams:
        return VoiceParams(
            onset=self.onset,
            offset=self.offset,
            min_speech=self.min_speech,
            min_silence=self.min_silence,
        )


class VoiceActivityPlugin(Plugin[VoiceActivityConfig]):
    """激活时加载模型，启动读音频环的任务，提供 `voice-activity@1`；停用时等在途的推理做完。"""

    manifest = Manifest(
        id="voice-activity",
        version="1",
        tier=Tier.ORDINARY,
        provides=(VOICE_ACTIVITY.ref,),
        permissions=(AUDIO_READ,),
        config=VoiceActivityConfig,
    )

    def load_model(self, data_dir: Path) -> SpeechModel:
        """核对并加载本地数据目录里的模型制品，在工作线程里调用；测试在子类里换成假模型。"""
        return SileroVad.load(data_dir)

    async def activate(self, ctx: PluginContext[VoiceActivityConfig]) -> None:
        reader = ctx.audio
        data_dir = ctx.config.data_dir or local_data_dir()
        model = await in_thread(functools.partial(self.load_model, data_dir))
        detector = VoiceActivityDetector(model, ctx.config.params())
        hub = VoiceHub(detector.status)
        runner = VoiceRunner(
            reader,
            detector,
            hub,
            poll_interval=ctx.config.poll_interval,
            stall_timeout=ctx.config.stall_timeout,
            name=ctx.entry_id,
        )
        task = asyncio.get_running_loop().create_task(
            runner.run(), name=f"{ctx.instance_id} 读音频"
        )

        async def stop() -> None:
            task.cancel()
            await asyncio.wait((task,))
            if not task.cancelled() and task.exception() is not None:
                logger.error("%s 的读音频任务异常结束", ctx.instance_id, exc_info=task.exception())

        ctx.register(stop)
        ctx.provide(VOICE_ACTIVITY, hub)
