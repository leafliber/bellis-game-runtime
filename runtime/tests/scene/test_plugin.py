"""`scene@1` 在插件内核中：清单与 WAITING 原因、逐帧判别与订阅、可选的 `ocr@1`、模板问题、
帧源缺失与停用时等在途的判别。

帧源用内存帧源，帧由合成游戏渲染；内核用不提交动作的假实时核。宿主拿不到服务目录，测试经一个依赖
`scene@1` 的探针插件取得服务。经帧环与真实实时核的联调见 `runtime/tests/core_link/test_scene.py`。
"""

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from core_protocol import models
from game_runtime.adapter import GameAdapterPlugin, PixelRect
from game_runtime.frame_ring import RingFrame
from game_runtime.frames import MemoryFrameSource
from game_runtime.kernel import (
    FRAMES_READ,
    ControlGrant,
    Kernel,
    KernelOptions,
    Manifest,
    NoConfig,
    Plugin,
    PluginContext,
    ReasonCode,
    ResolvedEntry,
    State,
    Submission,
    Tier,
    catalog,
)
from game_runtime.scene import (
    OCR,
    SCENE,
    SceneConfig,
    ScenePlugin,
    SceneService,
    SceneUpdate,
    Stability,
    Verdict,
)
from synthetic_game import Click, GameConfig, Key, ManualClock, Press, SyntheticGame

WAIT = 5.0


class NoCore:
    """这里的插件都不是控制产出者，内核不会调用实时核。"""

    async def invalidate_generation(self, grant: ControlGrant) -> None:
        raise AssertionError("不应调用")

    async def wait_released(self, grant: ControlGrant) -> None:
        raise AssertionError("不应调用")

    async def submit(
        self,
        grant: ControlGrant,
        steps: Sequence[models.ControlStep],
        *,
        base_frame: int,
        valid_for: float,
        start_delay: float = 0.0,
    ) -> Submission:
        raise AssertionError("不应调用")


class LabelReader:
    """OCR 的替身：按 ROI 的左上角给出合成游戏菜单按钮的文字；`gate` 设下时读之前先等它。"""

    LABELS: ClassVar = {(460, 300): "开始", (460, 388): "商店", (460, 476): "说明"}

    def __init__(self) -> None:
        self.calls = 0
        self.gate: threading.Event | None = None
        self.started = threading.Event()

    def read_lines(self, frame: RingFrame, rect: PixelRect) -> Sequence[str]:
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(WAIT)
        self.calls += 1
        label = self.LABELS.get((rect.left, rect.top))
        return [] if label is None else [label]


class Shared:
    """替身插件与测试之间共享的东西，每个测试重置。"""

    reader = LabelReader()
    scene: SceneService | None = None


@pytest.fixture(autouse=True)
def _reset() -> None:
    Shared.reader = LabelReader()
    Shared.scene = None


class FakeOcr(Plugin[NoConfig]):
    """提供 `ocr@1` 的替身插件。"""

    manifest = Manifest(id="fake-ocr", version="1", tier=Tier.ORDINARY, provides=(OCR.ref,))

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        ctx.provide(OCR, Shared.reader)


class SceneProbe(Plugin[NoConfig]):
    """依赖 `scene@1` 的探针插件，把服务交给测试。"""

    manifest = Manifest(id="scene-probe", version="1", tier=Tier.ORDINARY, inject=(SCENE.ref,))

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        Shared.scene = ctx.service(SCENE)

        def forget() -> None:
            Shared.scene = None

        ctx.register(forget)


def entry(
    entry_id: str,
    plugin: str | None = None,
    *,
    config: dict[str, Any] | None = None,
    grant: Sequence[str] = (),
    disabled: bool = False,
) -> ResolvedEntry:
    return ResolvedEntry(
        id=entry_id,
        plugin=plugin or entry_id,
        config=config or {},
        disabled=disabled,
        grant=frozenset(grant),
    )


def kernel(frames: MemoryFrameSource | None) -> Kernel:
    return Kernel(
        catalog=catalog(GameAdapterPlugin, ScenePlugin, FakeOcr, SceneProbe),
        core=NoCore(),
        options=KernelOptions(retry_delays=()),
        frames=frames,
    )


def profile(data_dir: Path, *, ocr: bool = False) -> list[ResolvedEntry]:
    entries = [
        entry("game", "game-adapter", config={"game": "synthetic-game"}),
        entry("scene", config={"data_dir": str(data_dir)}, grant=[FRAMES_READ]),
        entry("probe", "scene-probe"),
    ]
    if ocr:
        entries.append(entry("fake-ocr"))
    return entries


class Feed:
    """把合成游戏的帧逐个放进内存帧源，采集时刻取手动时钟。"""

    def __init__(self, frames: MemoryFrameSource) -> None:
        self.frames = frames
        self.clock = ManualClock()
        self.game = SyntheticGame(GameConfig(), self.clock)
        self.number = 0

    def push(self, seconds: float = 1 / 30) -> int:
        self.clock.advance(seconds)
        frame = self.game.frame()
        self.number += 1
        capture_ns = round(self.clock.now() * 1e9)
        ring = RingFrame(
            self.number, capture_ns, 1, frame.width, frame.height, frame.stride, frame.data
        )
        self.frames.push(ring)
        return self.number

    def click(self, target: str) -> None:
        rect = next(t.rect for t in self.game.truth().targets if t.id == target)
        self.game.handle(Click(*rect.center))


async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.005)
    return value


async def scene_service() -> SceneService:
    return await until(lambda: Shared.scene)


async def processed(scene: SceneService, number: int) -> SceneUpdate:
    def probe() -> SceneUpdate | None:
        latest = scene.latest
        return latest if latest is not None and latest.state.frame_number >= number else None

    return await until(probe)


def test_manifest() -> None:
    manifest = ScenePlugin.manifest

    assert manifest.tier is Tier.ORDINARY
    assert manifest.provides == ("scene@1",)
    assert manifest.inject == ("game-adapter@1", "ocr@1?")
    assert manifest.permissions == (FRAMES_READ,)
    assert SceneConfig().change_params().stable_after_ns == 250_000_000


def test_config_is_checked_and_given_in_seconds() -> None:
    params = SceneConfig().change_params()
    assert (
        params.motion_fraction,
        params.playing_after_ns,
        params.playing_ratio,
        params.playing_end_ns,
    ) == (0.1, 2_000_000_000, 0.8, 1_500_000_000)
    custom = SceneConfig(
        motion_fraction=0.2, playing_after=3.5, playing_ratio=1.0, playing_end=0.25
    ).change_params()
    assert (custom.motion_fraction, custom.playing_after_ns, custom.playing_end_ns) == (
        0.2,
        3_500_000_000,
        250_000_000,
    )
    for field, value in (
        ("motion_fraction", 0.0),
        ("motion_fraction", 1.5),
        ("playing_after", 0.0),
        ("playing_after", float("inf")),
        ("playing_ratio", 0.0),
        ("playing_ratio", 1.01),
        ("playing_end", -1.0),
        ("playing_end", float("nan")),
    ):
        with pytest.raises(ValidationError, match=field):
            SceneConfig.model_validate({field: value})


def test_scene_waits_for_the_adapter_and_the_frames_permission(data_dir: Path) -> None:
    async def scenario() -> None:
        k = kernel(MemoryFrameSource())
        k.add(
            [
                entry("scene", config={"data_dir": str(data_dir)}, grant=[FRAMES_READ]),
                entry("bare", "scene", config={"data_dir": str(data_dir)}),
                entry("game", "game-adapter", config={"game": "synthetic-game"}, disabled=True),
            ]
        )
        await k.settled()
        assert [r.code for r in k.status("scene").reasons] == [ReasonCode.SERVICE_MISSING]
        await k.enable("game")
        # 没有 OCR 也照常激活：ocr@1 是可选依赖。
        assert k.status("scene").state is State.ACTIVE
        assert [r.code for r in k.status("bare").reasons] == [ReasonCode.PERMISSION_NOT_GRANTED]
        await k.shutdown()

    asyncio.run(scenario())


def test_every_frame_is_judged_in_order_and_delivered_to_subscribers(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        frames = MemoryFrameSource()
        k = kernel(frames)
        k.add(profile(data_dir))
        await k.settled()
        scene = await scene_service()
        assert scene.latest is None
        received: list[SceneUpdate] = []
        unsubscribe = scene.subscribe(received.append)

        def broken(update: SceneUpdate) -> None:
            raise RuntimeError("监听者出错")

        stop_broken = scene.subscribe(broken)

        feed = Feed(frames)
        feed.click("menu.start")
        numbers = [feed.push() for _ in range(4)]
        await processed(scene, numbers[-1])
        unsubscribe()
        unsubscribe()  # 可以重复调用
        stop_broken()
        feed.push(1.0)  # 这一句已经显示完
        last = feed.push(0.3)
        await processed(scene, last)

        # 监听者按顺序收到每一帧，抛错的监听者不影响其它监听者。
        assert [u.state.frame_number for u in received] == numbers
        assert [u.frame.frame_number for u in received] == numbers
        assert all(u.state.screen == "dialogue" for u in received)
        assert received[0].state.stability is Stability.TRANSITION
        assert all(u.state.stability is Stability.CHANGING for u in received[1:])
        latest = scene.latest
        assert latest is not None
        assert latest.state.frame_number == last
        assert latest.state.stability is Stability.STABLE
        assert (scene.problems, scene.uncalibrated) == ((), ())
        errors = [r for r in caplog.records if "监听者处理帧" in r.getMessage()]
        assert len(errors) == len(numbers)

        # 帧源结束即停止读帧；插件仍是 ACTIVE。
        with caplog.at_level(logging.INFO, logger="game_runtime.scene.plugin"):
            frames.close()
            await until(lambda: any("帧源已结束" in r.getMessage() for r in caplog.records) or None)
        assert k.status("scene").state is State.ACTIVE
        await k.shutdown()
        assert Shared.scene is None

    asyncio.run(scenario())


def test_fixed_texts_use_ocr_while_it_is_there(data_dir: Path) -> None:
    async def scenario() -> None:
        frames = MemoryFrameSource()
        k = kernel(frames)
        k.add(profile(data_dir, ocr=True))
        await k.settled()
        scene = await scene_service()
        feed = Feed(frames)

        menu = await processed(scene, feed.push())
        assert (menu.state.verdict, menu.state.screen) == (Verdict.SCREEN, "title-menu")
        assert Shared.reader.calls == 1

        # OCR 停用后 scene 仍在运行；“开始”按钮的像素变了，固定文字项不可判。
        await k.disable("fake-ocr")
        assert k.status("scene").state is State.ACTIVE
        feed.game.handle(Press(Key.DOWN))
        later = await processed(scene, feed.push())
        assert later.state.verdict is Verdict.UNDETERMINED
        [signature] = [s for s in later.state.signatures if s.id == "title-menu"]
        assert signature.detail == "固定文字 [0]：OCR 不可用"

        # OCR 恢复后下一帧就用上。
        await k.enable("fake-ocr")
        feed.game.handle(Press(Key.DOWN))
        again = await processed(scene, feed.push())
        assert again.state.screen == "title-menu"
        await k.shutdown()

    asyncio.run(scenario())


def test_template_problems_are_reported(root: Path, caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        frames = MemoryFrameSource()
        k = kernel(frames)
        k.add(profile(root / "empty"))
        await k.settled()
        scene = await scene_service()

        [problem] = scene.problems
        assert problem.startswith("签名 dialogue 的锚点 [0]：没有模板文件")
        assert any(problem in r.getMessage() for r in caplog.records if r.levelname == "ERROR")
        feed = Feed(frames)
        feed.click("menu.start")
        dialogue = await processed(scene, feed.push())
        assert dialogue.state.verdict is Verdict.UNDETERMINED
        await k.shutdown()

    asyncio.run(scenario())


def test_without_a_frame_source_activation_fails(data_dir: Path) -> None:
    async def scenario() -> None:
        k = kernel(None)
        k.add(profile(data_dir))
        await k.settled()

        [reason] = k.status("scene").reasons
        assert reason.code is ReasonCode.RETRIES_EXHAUSTED
        assert "宿主没有给出帧源" in reason.message
        await k.shutdown()

    asyncio.run(scenario())


def test_relative_data_dir_is_a_config_error() -> None:
    async def scenario() -> None:
        k = kernel(MemoryFrameSource())
        k.add(profile(Path("relative")))
        await k.settled()

        [reason] = k.status("scene").reasons
        assert reason.code is ReasonCode.CONFIG_INVALID
        await k.shutdown()

    asyncio.run(scenario())


def test_disabling_waits_for_the_judgement_in_progress(data_dir: Path) -> None:
    async def scenario() -> None:
        frames = MemoryFrameSource()
        k = kernel(frames)
        k.add(profile(data_dir, ocr=True))
        await k.settled()
        scene = await scene_service()
        gate = threading.Event()
        Shared.reader.gate = gate

        Feed(frames).push()
        await asyncio.to_thread(Shared.reader.started.wait, WAIT)
        disabling = asyncio.create_task(k.disable("scene"))
        await asyncio.sleep(0.1)
        assert not disabling.done()  # 判别还在工作线程里
        assert k.status("scene").state is State.UNLOADING

        gate.set()
        await asyncio.wait_for(disabling, WAIT)
        assert k.status("scene").state is State.DISPOSED
        assert Shared.reader.calls == 1
        assert scene.latest is None  # 取消后不再发布结果
        await k.shutdown()

    asyncio.run(scenario())
