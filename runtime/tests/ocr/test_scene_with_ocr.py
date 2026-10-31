"""`scene@1` 用真实的 `ocr@1` 判别合成游戏的画面签名：标题菜单与坏结局的签名里有固定文字项，
由 `read_lines` 在 scene 的工作线程里读出。

标 `ocr_model`：本地数据目录里没有模型时跳过。锚点模板放在临时目录里（从合成游戏渲染裁出），
模型从本地数据目录读。帧源是内存帧源。
"""

import asyncio
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from core_protocol import models
from game_runtime.adapter import GameAdapterPlugin
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
    ResolvedEntry,
    State,
    Submission,
    Tier,
    catalog,
)
from game_runtime.ocr import OcrEngine, OcrPlugin
from game_runtime.scene import (
    SCENE,
    ScenePlugin,
    SceneService,
    SceneUpdate,
    Verdict,
    store_template,
)
from synthetic_game import GameConfig, Key, ManualClock, Press, SceneType, SyntheticGame
from synthetic_game.anchors import anchor_templates

pytestmark = pytest.mark.ocr_model

WAIT = 30.0


class NoCore:
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


scenes: list[SceneService] = []


class SceneProbe(Plugin[NoConfig]):
    manifest = Manifest(id="scene-probe", version="1", tier=Tier.ORDINARY, inject=(SCENE.ref,))

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        scenes.append(ctx.service(SCENE))


def entry(
    entry_id: str, plugin: str, config: dict[str, Any], grant: Sequence[str] = ()
) -> ResolvedEntry:
    return ResolvedEntry(
        id=entry_id, plugin=plugin, config=config, disabled=False, grant=frozenset(grant)
    )


@pytest.fixture
def templates(tmp_path: Path) -> Path:
    """放好合成游戏锚点模板的临时目录；Windows 上加扩展长度前缀，同 `runtime/tests/scene`。"""
    root = Path("\\\\?\\" + str(tmp_path.resolve())) if sys.platform == "win32" else tmp_path
    directory = root / "data"
    for template in anchor_templates():
        assert store_template(directory, "synthetic-game", template.png) == template.sha256
    return directory


async def until[T](probe: Callable[[], T | None]) -> T:
    deadline = time.monotonic() + WAIT
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.01)
    return value


def test_signatures_with_fixed_texts_are_judged_with_real_ocr(
    real_engine: OcrEngine, templates: Path
) -> None:
    del real_engine  # 只用来在没有模型时跳过
    scenes.clear()
    frames = MemoryFrameSource()
    clock = ManualClock()
    game = SyntheticGame(GameConfig(), clock)
    pushed = 0

    def push() -> int:
        nonlocal pushed
        clock.advance(1 / 30)
        frame = game.frame()
        pushed += 1
        capture_ns = round(clock.now() * 1e9)
        frames.push(
            RingFrame(pushed, capture_ns, 1, frame.width, frame.height, frame.stride, frame.data)
        )
        return pushed

    async def judged(number: int) -> SceneUpdate:
        scene = await until(lambda: scenes[0] if scenes else None)

        def probe() -> SceneUpdate | None:
            latest = scene.latest
            return latest if latest is not None and latest.state.frame_number >= number else None

        return await until(probe)

    async def scenario() -> tuple[SceneUpdate, SceneUpdate]:
        k = Kernel(
            catalog=catalog(GameAdapterPlugin, ScenePlugin, OcrPlugin, SceneProbe),
            core=NoCore(),
            options=KernelOptions(retry_delays=(), activate_timeout=60),
            frames=frames,
        )
        k.add(
            [
                entry("game", "game-adapter", {"game": "synthetic-game"}),
                entry("ocr", "ocr", {}),
                entry("scene", "scene", {"data_dir": str(templates)}, [FRAMES_READ]),
                entry("probe", "scene-probe", {}),
            ]
        )
        await k.settled()
        try:
            assert k.status("ocr").state is State.ACTIVE, k.status("ocr").reasons
            assert k.status("scene").state is State.ACTIVE, k.status("scene").reasons
            title = await judged(push())

            # 开始，读完开场对话，在选项上选“回去睡觉”，进入坏结局。
            game.handle(Press(Key.ENTER))
            while game.truth().scene is not SceneType.CHOICE:
                clock.advance(1.0)
                game.handle(Press(Key.ENTER))
            game.handle(Press(Key.DOWN))
            game.handle(Press(Key.DOWN))
            game.handle(Press(Key.ENTER))
            assert game.truth().scene is SceneType.BAD_ENDING
            ending = await judged(push())
            return title, ending
        finally:
            await k.shutdown()

    title, ending = asyncio.run(scenario())

    assert (title.state.verdict, title.state.screen) == (Verdict.SCREEN, "title-menu"), title.state
    assert (ending.state.verdict, ending.state.screen) == (Verdict.SCREEN, "bad-ending"), (
        ending.state
    )
