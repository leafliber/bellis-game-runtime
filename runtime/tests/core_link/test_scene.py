"""`scene@1` 经帧环与真实实时核的联调：合成游戏的帧写进假显示面，实时核的假采集源取帧写进帧环，
定序帧源 `SequencedFrameSource` 接纳后交给内核里的 `scene@1`，判别结果与真值一致。

Windows 上跑命名文件映射，Linux 容器里跑 POSIX 共享内存。只用内存帧源的测试见
`runtime/tests/scene/test_plugin.py`。
"""

import asyncio
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from core_protocol import models
from game_runtime.adapter import GameAdapterPlugin
from game_runtime.core_link import CaptureConfig, CoreLink, CoreLinkOptions, shutdown_runtime
from game_runtime.frame_ring.fake import FakeDisplay
from game_runtime.frames import AdmissionPolicy, SequencedFrameSource
from game_runtime.kernel import (
    FRAMES_READ,
    ControlGrant,
    Kernel,
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
from game_runtime.scene import (
    SCENE,
    ScenePlugin,
    SceneService,
    SceneUpdate,
    Stability,
    Verdict,
    store_template,
)
from synthetic_game import Click, GameConfig, ManualClock, SyntheticGame
from synthetic_game.anchors import anchor_templates

WAIT = 10.0
WIDTH, HEIGHT = 1280, 720


class Probe(Plugin[NoConfig]):
    """依赖 `scene@1` 的探针插件，把服务交给测试。"""

    manifest = Manifest(id="scene-probe", version="1", tier=Tier.ORDINARY, inject=(SCENE.ref,))
    service: SceneService | None = None

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        Probe.service = ctx.service(SCENE)


class NotAProducer:
    """内核的 `CoreControl`：这里没有控制产出者，不会被调用。动作块的联调见 `test_actions.py`。"""

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


def entry(
    entry_id: str, plugin: str, config: dict[str, str], grant: Sequence[str] = ()
) -> ResolvedEntry:
    return ResolvedEntry(
        id=entry_id, plugin=plugin, config=config, disabled=False, grant=frozenset(grant)
    )


async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.005)
    return value


def test_scene_judges_synthetic_frames_that_came_through_the_core(
    core_exe: Path, tmp_path: Path
) -> None:
    # 模板路径带 64 位摘要：Windows 上加扩展长度前缀，免得临时目录较深时超过 MAX_PATH。
    root = Path("\\\\?\\" + str(tmp_path.resolve())) if sys.platform == "win32" else tmp_path
    data_dir = root / "data"
    for template in anchor_templates():
        store_template(data_dir, "synthetic-game", template.png)
    game = SyntheticGame(GameConfig(WIDTH, HEIGHT), ManualClock())
    Probe.service = None

    async def main() -> None:
        with FakeDisplay.create(WIDTH, HEIGHT, slot_count=2) as display:
            capture = CaptureConfig(target=display.target, max_width=WIDTH, max_height=HEIGHT)
            options = CoreLinkOptions(
                core_exe,
                heartbeat_interval=0.05,
                heartbeat_timeout=0.3,
                health_interval=0.05,
                capture=capture,
            )
            link = CoreLink(options)
            await link.start()
            ring = link.frames
            assert ring is not None
            # 每一帧都接纳：读帧不限速，内容不变的帧也不等。
            frames = SequencedFrameSource(ring, policy=AdmissionPolicy(1000.0, 0.0))
            kernel = Kernel(
                catalog=catalog(GameAdapterPlugin, ScenePlugin, Probe),
                core=NotAProducer(),
                frames=frames,
            )
            frames.start()
            try:
                kernel.add(
                    [
                        entry("game", "game-adapter", {"game": "synthetic-game"}),
                        entry("scene", "scene", {"data_dir": str(data_dir)}, [FRAMES_READ]),
                        entry("probe", "scene-probe", {}),
                    ]
                )
                await kernel.settled()
                assert kernel.status("scene").state is State.ACTIVE
                scene = await until(lambda: Probe.service)

                async def show() -> SceneUpdate:
                    """显示当前画面，等 scene@1 判别到帧环里的这一帧。"""
                    frame = game.frame()
                    capture_ns = time.perf_counter_ns()
                    display.present(frame.data, WIDTH, HEIGHT, capture_ns=capture_ns)

                    def probe() -> SceneUpdate | None:
                        latest = scene.latest
                        return latest if latest and latest.state.capture_ns == capture_ns else None

                    update = await until(probe)
                    assert update.frame.pixels == frame.data
                    return update

                def click(target: str) -> None:
                    rect = next(t.rect for t in game.truth().targets if t.id == target)
                    game.handle(Click(*rect.center))

                # 标题菜单的签名里有固定文字项，这里没有 OCR：不可判，不当作命中。
                menu = await show()
                assert menu.state.verdict is Verdict.UNDETERMINED
                assert menu.state.frame_number == ring.latest_frame_number()

                click("menu.start")
                dialogue = await show()
                assert (dialogue.state.verdict, dialogue.state.screen) == (
                    Verdict.SCREEN,
                    "dialogue",
                )
                [anchor] = next(s for s in dialogue.state.signatures if s.id == "dialogue").anchors
                assert anchor.score is not None
                assert anchor.score > 0.99
                assert dialogue.state.stability is Stability.TRANSITION

                # 同一画面持续显示：按采集时刻稳定下来。
                game.handle(Click(*next(t.rect for t in game.truth().targets).center))
                still = await show()
                for _ in range(8):
                    await asyncio.sleep(0.05)
                    still = await show()
                assert still.state.stability is Stability.STABLE
                assert still.state.frame_number > dialogue.state.frame_number
            finally:
                await frames.close()
                await shutdown_runtime(kernel, link)
            assert link.frames is None
            assert frames.failure is None

    asyncio.run(main())
