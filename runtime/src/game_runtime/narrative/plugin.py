"""普通插件 `narrative`：按顺序处理 `scene@1` 的每个更新，在对白框画面上以 `ocr@1` 读文字，
逐句记录剧情，存本机 SQLite，提供 `narrative@1`。

设计见 `docs/modules/narrative-planner.md` 的“剧情记录”。记录只取决于依次处理的 scene 更新与
OCR 结果，不读墙钟；不调用 VLM 或其它模型，不外发，完整日志不出本机。
"""

import asyncio
import collections
import functools
import hashlib
import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Protocol, Self

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_runtime._threads import in_thread
from game_runtime.adapter import GAME_ADAPTER, Adapter, DialogueBox, PixelRect, ScreenType
from game_runtime.frame_ring import RingFrame
from game_runtime.kernel import Manifest, Plugin, PluginContext, ServiceToken, Tier
from game_runtime.local_data import local_data_dir
from game_runtime.narrative.record import Discontinuity, NarrativeRecord, Scope
from game_runtime.narrative.store import NarrativeStore
from game_runtime.narrative.tracker import (
    BoxFrame,
    Changes,
    Gap,
    Observation,
    OtherScreen,
    Reading,
    SentenceTracker,
    TrackerParams,
    Unjudged,
)
from game_runtime.ocr import OCR, OcrService
from game_runtime.scene import SCENE, Outcome, SceneUpdate, Verdict

logger = logging.getLogger(__name__)

NARRATIVE_WRITE: Final = "narrative.write"
"""写剧情记录的权限（`docs/modules/plugin-kernel.md` 的“权限”）。"""

SCOPE_TYPES: Final = frozenset({ScreenType.BAD_ENDING, ScreenType.SAVE_LOAD})
"""判出这些通用类型的画面后，下一句开始新的剧情范围（重来与读档）。"""
READING_CACHE_SIZE: Final = 32
"""按 ROI 与像素内容的摘要缓存的识别结果的条数。"""
BYTES_PER_PIXEL: Final = 4

type NarrativeListener = Callable[[NarrativeRecord], None]


class NarrativeService(Protocol):
    """`narrative@1`：本次运行的剧情记录，只在本机进程内取用。"""

    @property
    def session(self) -> int:
        """本次运行号。"""
        ...

    @property
    def latest(self) -> NarrativeRecord | None:
        """出现编号最大的一句的最新状态，即当前这一句；还没有时为 None。"""
        ...

    def records(self, after: int = 0) -> tuple[NarrativeRecord, ...]:
        """本次运行中出现编号大于 `after` 的各句的最新状态，按出现编号。"""
        ...

    @property
    def scopes(self) -> tuple[Scope, ...]: ...

    @property
    def discontinuities(self) -> tuple[Discontinuity, ...]: ...

    def subscribe(self, listener: NarrativeListener) -> Callable[[], None]:
        """每句每变一次（新建、修订、说话人、完整度、结束），监听者按处理的顺序收到它的新状态。

        在事件循环里同步调用，抛错只记录；返回取消订阅的函数（可重复调用），订阅方登记为撤销函数。
        最后出现的帧号与时刻每帧都变，不单独通知，`latest` 与 `records` 里总是最新的。
        """
        ...

    @property
    def processed_frame(self) -> int:
        """最近处理完的 scene 更新的帧号；还没有时为 0。"""
        ...

    @property
    def backlog(self) -> int:
        """收到而还没处理完的更新个数。"""
        ...

    async def wait_idle(self) -> None:
        """等到收到的更新都处理完。"""
        ...


NARRATIVE: Final = ServiceToken[NarrativeService]("narrative", 1)


class NarrativeConfig(BaseModel):
    """参数的含义见模块文档的“完整度”“处理顺序与积压”与“存储”。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    complete_frames: Annotated[int, Field(ge=1)] = 3
    complete_after: Annotated[float, Field(ge=0.0, allow_inf_nan=False)] = 0.5
    """秒。"""
    low_confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.8
    max_backlog: Annotated[int, Field(ge=2)] = 600
    keep_sessions: Annotated[int, Field(ge=1)] = 20
    max_records: Annotated[int, Field(ge=1)] = 200_000
    data_dir: Path | None = None
    """本地数据目录，绝对路径；缺省按 `BELLIS_GAME_RUNTIME_DATA` 与平台约定。"""

    @model_validator(mode="after")
    def _absolute(self) -> Self:
        if self.data_dir is not None and not self.data_dir.is_absolute():
            raise ValueError("data_dir 须为绝对路径")
        return self

    def tracker_params(self) -> TrackerParams:
        return TrackerParams(
            complete_frames=self.complete_frames,
            complete_after_ns=round(self.complete_after * 1e9),
            low_confidence=self.low_confidence,
        )


@dataclass(frozen=True, slots=True)
class RoiPixels:
    """从一帧拷出的一个 ROI 的像素（BGRA8，原点在 ROI 的左上角），满足 `ocr@1` 的 `FrameLike`。"""

    roi: str
    frame_number: int
    transform_version: int
    width: int
    height: int
    pixels: bytes

    @property
    def row_stride(self) -> int:
        return self.width * BYTES_PER_PIXEL


@dataclass(frozen=True, slots=True)
class _BoxItem:
    """排队中的对白框帧：ROI 的像素已拷出，等识别。"""

    frame_number: int
    capture_ns: int
    screen: str
    text: RoiPixels
    speaker: RoiPixels | None
    marker: bool


type _Item = _BoxItem | OtherScreen | Unjudged | Gap


def _frame_number(item: _Item) -> int:
    return item.last_frame if isinstance(item, Gap) else item.frame_number


def crop_roi(frame: RingFrame, roi: str, rect: PixelRect, previous: RoiPixels | None) -> RoiPixels:
    """拷出 `rect`（客户区物理像素）的像素；与 `previous` 相同时共用它的那一份。"""
    left, top, right, bottom = rect
    if not (0 <= left < right <= frame.width and 0 <= top < bottom <= frame.height):
        raise ValueError(f"区域 {tuple(rect)} 为空或超出 {frame.width}×{frame.height} 的帧")
    rows = np.frombuffer(frame.pixels, np.uint8, count=frame.row_stride * frame.height)
    region = rows.reshape(frame.height, frame.row_stride)[
        top:bottom, left * BYTES_PER_PIXEL : right * BYTES_PER_PIXEL
    ]
    pixels = region.tobytes()
    width, height = right - left, bottom - top
    if (
        previous is not None
        and (previous.width, previous.height) == (width, height)
        and previous.pixels == pixels
    ):
        pixels = previous.pixels
    return RoiPixels(roi, frame.frame_number, frame.transform_version, width, height, pixels)


class Recorder:
    """`narrative@1` 的实现：scene 的监听者排队，一个任务按顺序处理并写库。"""

    def __init__(
        self,
        adapter: Adapter,
        boxes: Sequence[DialogueBox],
        ocr: OcrService,
        store: NarrativeStore,
        db: ThreadPoolExecutor,
        *,
        params: TrackerParams,
        max_backlog: int,
    ) -> None:
        self._adapter = adapter
        self._boxes = {box.screen: box for box in boxes}
        self._scope_screens = frozenset(
            screen.id
            for screen in adapter.screens
            if adapter.generic_type(screen.id) in SCOPE_TYPES
        )
        self._ocr = ocr
        self._store = store
        self._db = db
        self._tracker = SentenceTracker(store.session, params)
        self._max_backlog = max_backlog
        self._queue: collections.deque[_Item] = collections.deque()
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._busy = False
        self._dropping: list[int] | None = None
        """丢弃中的一段：首帧号、末帧号、个数。"""
        self._previous: dict[str, RoiPixels] = {}
        self._last: dict[str, tuple[bytes, Reading]] = {}
        self._cache: collections.OrderedDict[tuple[str, int, int, bytes], Reading] = (
            collections.OrderedDict()
        )
        self._listeners: list[NarrativeListener] = []
        self._processed = 0
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._ocr_failing = False
        self._db_failing = False

    # narrative@1

    @property
    def session(self) -> int:
        return self._tracker.session

    @property
    def latest(self) -> NarrativeRecord | None:
        return self._tracker.latest

    def records(self, after: int = 0) -> tuple[NarrativeRecord, ...]:
        return self._tracker.records[max(0, after) :]

    @property
    def scopes(self) -> tuple[Scope, ...]:
        return self._tracker.scopes

    @property
    def discontinuities(self) -> tuple[Discontinuity, ...]:
        return self._tracker.discontinuities

    def subscribe(self, listener: NarrativeListener) -> Callable[[], None]:
        entry: list[NarrativeListener] = [listener]
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if entry:
                self._listeners.remove(entry.pop())

        return unsubscribe

    @property
    def processed_frame(self) -> int:
        return self._processed

    @property
    def backlog(self) -> int:
        return len(self._queue) + (1 if self._busy else 0)

    async def wait_idle(self) -> None:
        await self._idle.wait()

    # 生命周期

    def start(self, name: str) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run(), name=name)

    async def stop(self) -> None:
        """取消处理任务，丢弃还没处理的更新，把当前句的最新状态写库。"""
        self._stopped = True
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.wait((task,))
            if not task.cancelled() and task.exception() is not None:
                logger.error("剧情记录的处理任务异常结束", exc_info=task.exception())
        pending = len(self._queue) + (self._dropping[2] if self._dropping else 0)
        if pending:
            logger.warning("停用时丢弃了 %d 个尚未处理的画面更新", pending)
        self._queue.clear()
        self._idle.set()
        latest = self._tracker.latest
        if latest is not None and not latest.closed:
            await self._write(Changes((latest,)))

    # scene@1 的监听者

    def on_update(self, update: SceneUpdate) -> None:
        """在事件循环里同步调用：只把更新排队，对白框画面的帧在这时拷出 ROI 的像素。"""
        if self._stopped:
            return
        frame_number = update.state.frame_number
        self._resume()
        dropping = self._dropping
        if dropping is not None:
            dropping[1] = frame_number
            dropping[2] += 1
            return
        if len(self._queue) >= self._max_backlog:
            logger.error("剧情记录积压达到上限 %d，开始丢弃更新", self._max_backlog)
            self._dropping = [frame_number, frame_number, 1]
            return
        self._enqueue(self._item(update))

    def _resume(self) -> None:
        """丢弃中且积压已降到一半时停止丢弃，把丢掉的一段作为中断排进队列。"""
        dropping = self._dropping
        if dropping is None or len(self._queue) > self._max_backlog // 2:
            return
        first, last, count = dropping
        self._dropping = None
        logger.error(
            "剧情记录积压超限，丢弃了帧 %d–%d 的 %d 个更新；有中断的运行不保证与回放相同",
            first,
            last,
            count,
        )
        self._enqueue(Gap(first, last, count))

    def _enqueue(self, item: _Item) -> None:
        self._queue.append(item)
        self._idle.clear()
        self._wake.set()

    def _item(self, update: SceneUpdate) -> _Item:
        state = update.state
        if state.verdict is not Verdict.SCREEN or state.screen is None:
            return Unjudged(state.frame_number)
        box = self._boxes.get(state.screen)
        if box is None:
            return OtherScreen(
                state.frame_number, state.screen, state.screen in self._scope_screens
            )
        frame = update.frame
        try:
            text = self._crop(frame, box.text)
            speaker = None if box.speaker is None else self._crop(frame, box.speaker)
        except ValueError as error:
            logger.warning("帧 %d 的对白框 ROI 拷不出来：%s", state.frame_number, error)
            return Unjudged(state.frame_number)
        marker = box.continue_signature is not None and any(
            result.id == box.continue_signature and result.outcome is Outcome.MATCH
            for result in state.signatures
        )
        return _BoxItem(state.frame_number, state.capture_ns, box.screen, text, speaker, marker)

    def _crop(self, frame: RingFrame, roi: str) -> RoiPixels:
        rect = self._adapter.roi(roi).to_pixels(frame.width, frame.height)
        pixels = crop_roi(frame, roi, rect, self._previous.get(roi))
        self._previous[roi] = pixels
        return pixels

    # 处理

    async def _run(self) -> None:
        while True:
            while not self._queue:
                self._idle.set()
                self._wake.clear()
                await self._wake.wait()
            item = self._queue.popleft()
            self._resume()
            self._busy = True
            try:
                changes = self._tracker.observe(await self._observe(item))
                if changes:
                    await self._write(changes)
                    self._notify(changes)
                self._processed = _frame_number(item)
            finally:
                self._busy = False

    async def _observe(self, item: _Item) -> Observation:
        if not isinstance(item, _BoxItem):
            return item
        try:
            text = await self._read(item.text)
            speaker = None if item.speaker is None else await self._read(item.speaker)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._ocr_failing:
                logger.warning("帧 %d 的 OCR 出错，这一帧不改变记录：%s", item.frame_number, error)
                self._ocr_failing = True
            return Unjudged(item.frame_number)
        if self._ocr_failing:
            logger.info("剧情记录的 OCR 已恢复")
            self._ocr_failing = False
        return BoxFrame(item.frame_number, item.capture_ns, item.screen, text, speaker, item.marker)

    async def _read(self, pixels: RoiPixels) -> Reading:
        """识别一个 ROI；像素与识别过的相同时沿用那次的结果。"""
        last = self._last.get(pixels.roi)
        if last is not None and last[0] is pixels.pixels:
            return last[1]
        digest = await in_thread(functools.partial(_digest, pixels.pixels))
        key = (pixels.roi, pixels.width, pixels.height, digest)
        reading = self._cache.get(key)
        if reading is None:
            result = await self._ocr.recognize_rect(
                pixels, PixelRect(0, 0, pixels.width, pixels.height)
            )
            reading = Reading(
                pixels.roi, tuple((line.text, line.confidence) for line in result.lines)
            )
            self._cache[key] = reading
            while len(self._cache) > READING_CACHE_SIZE:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(key)
        self._last[pixels.roi] = (pixels.pixels, reading)
        return reading

    async def _write(self, changes: Changes) -> None:
        scopes = () if changes.scope is None else (changes.scope,)
        gaps = () if changes.discontinuity is None else (changes.discontinuity,)
        job = functools.partial(self._store.write, changes.records, scopes, gaps)
        try:
            await asyncio.get_running_loop().run_in_executor(self._db, job)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._db_failing:
                logger.exception("剧情记录写库出错，内存里的记录照常更新")
                self._db_failing = True
            return
        if self._db_failing:
            logger.info("剧情记录写库已恢复")
            self._db_failing = False

    def _notify(self, changes: Changes) -> None:
        for record in changes.records:
            for listener in list(self._listeners):
                try:
                    listener(record)
                except Exception:
                    logger.exception("narrative@1 的监听者处理第 %d 句时出错", record.occurrence)


def _digest(pixels: bytes) -> bytes:
    return hashlib.blake2b(pixels, digest_size=16).digest()


class NarrativePlugin(Plugin[NarrativeConfig]):
    """激活时取对白框、打开存储并新开运行、订阅 `scene@1`，提供 `narrative@1`。"""

    manifest = Manifest(
        id="narrative",
        version="1",
        tier=Tier.ORDINARY,
        provides=(NARRATIVE.ref,),
        inject=(GAME_ADAPTER.ref, SCENE.ref, OCR.ref),
        permissions=(NARRATIVE_WRITE,),
        config=NarrativeConfig,
    )

    async def activate(self, ctx: PluginContext[NarrativeConfig]) -> None:
        config = ctx.config
        adapter = ctx.service(GAME_ADAPTER)
        scene = ctx.service(SCENE)
        ocr = ctx.service(OCR)
        boxes = adapter.require_dialogue_boxes()
        data_dir = config.data_dir or local_data_dir()

        db = ThreadPoolExecutor(max_workers=1, thread_name_prefix="narrative-db")
        ctx.register(functools.partial(_shutdown, db))
        loop = asyncio.get_running_loop()
        opening = functools.partial(
            NarrativeStore.open,
            data_dir,
            adapter.id,
            adapter.version,
            keep_sessions=config.keep_sessions,
            max_records=config.max_records,
        )
        store = await loop.run_in_executor(db, opening)

        async def close_store() -> None:
            await loop.run_in_executor(db, store.close)

        ctx.register(close_store)
        logger.info("剧情记录写入 %s，运行号 %d", store.path, store.session)

        recorder = Recorder(
            adapter,
            boxes,
            ocr,
            store,
            db,
            params=config.tracker_params(),
            max_backlog=config.max_backlog,
        )
        recorder.start(f"{ctx.instance_id} 剧情记录")
        ctx.register(recorder.stop)
        ctx.register(scene.subscribe(recorder.on_update))
        ctx.provide(NARRATIVE, recorder)


async def _shutdown(executor: ThreadPoolExecutor) -> None:
    await in_thread(functools.partial(executor.shutdown, wait=True))
