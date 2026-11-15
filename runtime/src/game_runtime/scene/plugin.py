"""普通插件 `scene`：从帧源逐帧读取，判别变化与稳定和画面签名，提供 `scene@1`。

设计见 `docs/modules/perception.md` 的“画面变化与签名”。
"""

import asyncio
import functools
import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Final, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_runtime._threads import in_thread
from game_runtime.adapter import GAME_ADAPTER, PixelRect
from game_runtime.frame_ring import RingFrame
from game_runtime.frames import FrameBacklogError, FrameSource
from game_runtime.kernel import (
    FRAMES_READ,
    Manifest,
    Plugin,
    PluginContext,
    ServiceToken,
    Tier,
)
from game_runtime.local_data import local_data_dir
from game_runtime.ocr.service import OcrLine, OcrResult
from game_runtime.scene.analyzer import SceneAnalyzer, SceneUpdate
from game_runtime.scene.change import ChangeParams
from game_runtime.scene.signatures import OCR, SignatureJudge, Verdict
from game_runtime.scene.state import ScreenObservation

logger = logging.getLogger(__name__)

RETRY_INTERVAL: Final = 0.5
"""读帧失败后再试的间隔（秒）。"""


type SceneListener = Callable[[SceneUpdate], None]


class SceneService(Protocol):
    """`scene@1`。"""

    @property
    def latest(self) -> SceneUpdate | None:
        """最近一次的结果；还没有时为 None。"""
        ...

    def subscribe(self, listener: SceneListener) -> Callable[[], None]:
        """监听者按处理的顺序收到每一帧的结果，返回取消订阅的函数（可重复调用）。

        监听者在事件循环里同步调用，抛错只记录，不影响其它监听者；要做重活的监听者自己排队。
        订阅方把返回的函数登记为撤销函数。
        """
        ...

    def observe(self, update: SceneUpdate, observation: ScreenObservation) -> bool:
        """合入原帧上的观察，已换画面或已失效时返回 False。"""
        ...

    def with_ocr(self, update: SceneUpdate, results: Sequence[OcrResult]) -> SceneUpdate | None:
        """为原帧补充 OCR 吸附目标。画面已换或已失效时返回 None。"""
        ...

    @property
    def problems(self) -> tuple[str, ...]:
        """激活时发现的、使签名不可判的问题（模板缺失、摘要不符等）。"""
        ...

    @property
    def uncalibrated(self) -> tuple[str, ...]:
        """尚未标定、不参与判别的签名。"""
        ...


SCENE: Final = ServiceToken[SceneService]("scene", 1)


class SceneHub:
    """`scene@1` 的实现：保存最近的结果并分发给监听者。`publish` 只由 `scene` 插件调用。"""

    def __init__(self, *, problems: tuple[str, ...], uncalibrated: tuple[str, ...]) -> None:
        self._problems = problems
        self._uncalibrated = uncalibrated
        self._latest: SceneUpdate | None = None
        self._listeners: dict[object, SceneListener] = {}
        self._closed = False

    @property
    def latest(self) -> SceneUpdate | None:
        return self._latest

    @property
    def problems(self) -> tuple[str, ...]:
        return self._problems

    @property
    def uncalibrated(self) -> tuple[str, ...]:
        return self._uncalibrated

    def subscribe(self, listener: SceneListener) -> Callable[[], None]:
        if self._closed:
            raise RuntimeError("scene@1 已停用")
        entry = object()
        self._listeners[entry] = listener

        def unsubscribe() -> None:
            self._listeners.pop(entry, None)

        return unsubscribe

    def observe(self, update: SceneUpdate, observation: ScreenObservation) -> bool:
        latest = self._matching_scope(update)
        if latest is None or latest.state.playing:
            return False
        screen = latest.screen_state
        observations = tuple(
            item for item in screen.observations if item.screen_type is not observation.screen_type
        )
        screen = replace(screen, observations=(*observations, observation))
        self._latest = replace(latest, screen_state=screen)
        return True

    def with_ocr(self, update: SceneUpdate, results: Sequence[OcrResult]) -> SceneUpdate | None:
        latest = self._matching_scope(update)
        if latest is None:
            return None
        targets: tuple[OcrLine, ...] = ()
        if not latest.state.playing and latest.state.verdict is not Verdict.DISABLED:
            targets = _text_targets(update.frame, results)
        screen = replace(latest.screen_state, text_targets=targets)
        return replace(update, screen_state=screen)

    def _matching_scope(self, update: SceneUpdate) -> SceneUpdate | None:
        latest = self._latest
        if latest is not None and latest.observation_scope is update.observation_scope:
            return latest
        return None

    def invalidate(self) -> None:
        self._latest = None

    def begin_frame(self, frame: RingFrame) -> None:
        if self._latest is not None and not _same_picture(self._latest.frame, frame):
            self.invalidate()

    def close(self) -> None:
        self._closed = True
        self.invalidate()
        self._listeners.clear()

    def publish(self, update: SceneUpdate) -> None:
        if self._closed:
            return
        old = self._latest
        if old is not None and _same_picture(old.frame, update.frame):
            screen = replace(update.screen_state, observations=old.screen_state.observations)
            # 确定性判别改变后重新观察，但同一画面已追加的限制仍然有效。
            scope = (
                old.observation_scope if _same_judgement(old, update) else update.observation_scope
            )
            update = replace(update, screen_state=screen, observation_scope=scope)
        self._latest = update
        for listener in list(self._listeners.values()):
            try:
                listener(update)
            except Exception:
                logger.exception("scene@1 的监听者处理帧 %d 时出错", update.state.frame_number)


def _same_picture(before: RingFrame, current: RingFrame) -> bool:
    return (
        before.frame_number < current.frame_number
        and before.transform_version == current.transform_version
        and before.width == current.width
        and before.height == current.height
        and before.row_stride == current.row_stride
        and before.pixels == current.pixels
    )


def _same_judgement(previous: SceneUpdate, update: SceneUpdate) -> bool:
    before, current = previous.state, update.state
    return (before.playing, before.verdict, before.screen, before.matched) == (
        current.playing,
        current.verdict,
        current.screen,
        current.matched,
    )


def _inside(box: PixelRect, outer: PixelRect) -> bool:
    return (
        outer.left <= box.left < box.right <= outer.right
        and outer.top <= box.top < box.bottom <= outer.bottom
    )


def _text_targets(frame: RingFrame, results: Sequence[OcrResult]) -> tuple[OcrLine, ...]:
    client = PixelRect(0, 0, frame.width, frame.height)
    lines: dict[OcrLine, None] = {}
    for result in results:
        if (
            result.frame_number != frame.frame_number
            or result.transform_version != frame.transform_version
            or not _inside(result.rect, client)
        ):
            continue
        for line in result.lines:
            if (
                line.text.strip()
                and 0.0 <= line.confidence <= 1.0
                and _inside(line.box, result.rect)
            ):
                lines[line] = None
    return tuple(lines)


class SceneConfig(BaseModel):
    """参数的含义见 `ChangeParams`；`data_dir` 缺省为本地数据目录。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grid_width: Annotated[int, Field(ge=4, le=1024)] = 64
    cell_threshold: Annotated[int, Field(ge=0, le=254)] = 8
    stable_after: Annotated[float, Field(gt=0.0, allow_inf_nan=False)] = 0.25
    """秒。"""
    transition_fraction: Annotated[float, Field(gt=0.0, le=1.0)] = 0.5
    motion_fraction: Annotated[float, Field(gt=0.0, le=1.0)] = 0.1
    playing_gap: Annotated[float, Field(gt=0.0, allow_inf_nan=False)] = 0.5
    """秒。"""
    playing_after: Annotated[float, Field(gt=0.0, allow_inf_nan=False)] = 2.0
    """秒。"""
    playing_end: Annotated[float, Field(gt=0.0, allow_inf_nan=False)] = 1.5
    """秒。"""
    data_dir: Path | None = None
    """本地数据目录，绝对路径；缺省按 `BELLIS_GAME_RUNTIME_DATA` 与平台约定。"""

    @model_validator(mode="after")
    def _absolute(self) -> Self:
        if self.data_dir is not None and not self.data_dir.is_absolute():
            raise ValueError("data_dir 须为绝对路径")
        return self

    def change_params(self) -> ChangeParams:
        return ChangeParams(
            grid_width=self.grid_width,
            cell_threshold=self.cell_threshold,
            stable_after_ns=_ns(self.stable_after),
            transition_fraction=self.transition_fraction,
            motion_fraction=self.motion_fraction,
            playing_gap_ns=_ns(self.playing_gap),
            playing_after_ns=_ns(self.playing_after),
            playing_end_ns=_ns(self.playing_end),
        )


def _ns(seconds: float) -> int:
    """秒换成纳秒，至少 1。"""
    return max(1, round(seconds * 1e9))


class ScenePlugin(Plugin[SceneConfig]):
    """激活时读入锚点模板，启动读帧任务，提供 `scene@1`；停用时等在途的判别做完。"""

    manifest = Manifest(
        id="scene",
        version="1",
        tier=Tier.ORDINARY,
        provides=(SCENE.ref,),
        inject=(GAME_ADAPTER.ref, f"{OCR.ref}?"),
        permissions=(FRAMES_READ,),
        config=SceneConfig,
    )

    async def activate(self, ctx: PluginContext[SceneConfig]) -> None:
        frames = ctx.frames
        adapter = ctx.service(GAME_ADAPTER)
        data_dir = ctx.config.data_dir or local_data_dir()
        judge = await in_thread(functools.partial(SignatureJudge.load, adapter, data_dir))
        for problem in judge.problems:
            logger.error("适配包 %s：%s，引用它的锚点不可判", adapter.id, problem)
        analyzer = SceneAnalyzer(judge, ctx.config.change_params())
        hub = SceneHub(problems=judge.problems, uncalibrated=judge.uncalibrated)
        task = asyncio.get_running_loop().create_task(
            _read_frames(ctx, frames, analyzer, hub), name=f"{ctx.instance_id} 读帧"
        )

        async def stop() -> None:
            hub.close()
            task.cancel()
            await asyncio.wait((task,))
            if not task.cancelled() and task.exception() is not None:
                logger.error("%s 的读帧任务异常结束", ctx.instance_id, exc_info=task.exception())

        ctx.register(stop)
        ctx.provide(SCENE, hub)


async def _read_frames(
    ctx: PluginContext[SceneConfig],
    frames: FrameSource,
    analyzer: SceneAnalyzer,
    hub: SceneHub,
) -> None:
    after = 0
    failing = False
    while True:
        try:
            frame = await frames.next_frame(after)
        except FrameBacklogError as error:
            hub.invalidate()
            logger.error("%s 积压超限：%s", ctx.entry_id, error)
            after = error.resume_after
            continue
        except Exception as error:
            hub.invalidate()
            if not failing:
                logger.warning("%s 读帧失败，%g 秒后重试：%s", ctx.entry_id, RETRY_INTERVAL, error)
                failing = True
            await asyncio.sleep(RETRY_INTERVAL)
            continue
        if failing:
            logger.info("%s 恢复读帧", ctx.entry_id)
            failing = False
        if frame is None:
            hub.invalidate()
            logger.info("%s 的帧源已结束", ctx.entry_id)
            return
        after = frame.frame_number
        hub.begin_frame(frame)
        try:
            reader = ctx.optional(OCR)
            update = await in_thread(functools.partial(analyzer.analyze_update, frame, reader))
        except Exception:
            hub.invalidate()
            logger.exception("%s 判别帧 %d 出错，跳过这一帧", ctx.entry_id, frame.frame_number)
            continue
        hub.publish(update)
