"""普通插件 `scene`：从帧源逐帧读取，判别变化与稳定和画面签名，提供 `scene@1`。

设计见 `docs/modules/perception.md` 的“画面变化与签名”。
"""

import asyncio
import functools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_runtime._threads import in_thread
from game_runtime.adapter import GAME_ADAPTER
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
from game_runtime.scene.analyzer import SceneAnalyzer, SceneState
from game_runtime.scene.change import ChangeParams
from game_runtime.scene.signatures import OCR, SignatureJudge

logger = logging.getLogger(__name__)

RETRY_INTERVAL: Final = 0.5
"""读帧失败后再试的间隔（秒）。"""


@dataclass(frozen=True, slots=True)
class SceneUpdate:
    """处理过的一帧与它的判别结果。"""

    frame: RingFrame
    state: SceneState


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
        self._listeners: list[SceneListener] = []

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
        entry: list[SceneListener] = [listener]
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if entry:
                self._listeners.remove(entry.pop())

        return unsubscribe

    def publish(self, update: SceneUpdate) -> None:
        self._latest = update
        for listener in list(self._listeners):
            try:
                listener(update)
            except Exception:
                logger.exception("scene@1 的监听者处理帧 %d 时出错", update.state.frame_number)


class SceneConfig(BaseModel):
    """参数的含义见 `ChangeParams`；`data_dir` 缺省为本地数据目录。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grid_width: Annotated[int, Field(ge=4, le=1024)] = 64
    cell_threshold: Annotated[int, Field(ge=0, le=254)] = 8
    stable_after: Annotated[float, Field(gt=0.0, allow_inf_nan=False)] = 0.25
    """秒。"""
    transition_fraction: Annotated[float, Field(gt=0.0, le=1.0)] = 0.5
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
            stable_after_ns=max(1, round(self.stable_after * 1e9)),
            transition_fraction=self.transition_fraction,
        )


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
            logger.error("%s 积压超限：%s", ctx.entry_id, error)
            after = error.resume_after
            continue
        except Exception as error:
            if not failing:
                logger.warning("%s 读帧失败，%g 秒后重试：%s", ctx.entry_id, RETRY_INTERVAL, error)
                failing = True
            await asyncio.sleep(RETRY_INTERVAL)
            continue
        if failing:
            logger.info("%s 恢复读帧", ctx.entry_id)
            failing = False
        if frame is None:
            logger.info("%s 的帧源已结束", ctx.entry_id)
            return
        after = frame.frame_number
        reader = ctx.optional(OCR)
        try:
            state = await in_thread(functools.partial(analyzer.analyze, frame, reader))
        except Exception:
            logger.exception("%s 判别帧 %d 出错，跳过这一帧", ctx.entry_id, frame.frame_number)
            continue
        hub.publish(SceneUpdate(frame, state))
