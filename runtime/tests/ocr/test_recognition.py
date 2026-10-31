"""用真实模型识别合成游戏的画面：对白（含逐字显示到一半）、名字、选项、计时选择、弹窗与坏结局，
两种分辨率；语言核对在标题菜单上相符，在合成的不符样本上不符；插件在内核里加载真实模型。

标 `ocr_model`：本地数据目录里没有模型（`uv run fetch-models`）时跳过。合成游戏的字体是
Noto Sans SC 的子集。准确率的断言留有余量，不要求 100%。
"""

import asyncio
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from core_protocol import models
from game_runtime.adapter import Adapter, GameAdapterPlugin
from game_runtime.frame_ring import RingFrame
from game_runtime.kernel import (
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
from game_runtime.ocr import OCR, OcrEngine, OcrPlugin, OcrService, recognize_frame
from synthetic_game import GameConfig, Key, ManualClock, Press, SceneType, SyntheticGame
from synthetic_game.layout import Layout
from synthetic_game.render import Button, MenuView, Renderer, View
from synthetic_game.story import NODES, UI, Dialogue

pytestmark = pytest.mark.ocr_model

RESOLUTIONS = [(1280, 720), (2560, 1440)]
MIN_ACCURACY = 0.95
"""全部样本合计的字符准确率下限；本机实测更高，留出余量。"""


@dataclass(frozen=True)
class Shot:
    label: str
    frame: RingFrame
    roi: str
    truth: str


def to_ring(game: SyntheticGame, number: int) -> RingFrame:
    frame = game.frame()
    return RingFrame(number, 0, 1, frame.width, frame.height, frame.stride, frame.data)


def walkthrough(width: int, height: int) -> Iterator[Shot]:
    """按回车走一遍：开场对话、选项、地下对话、弹窗、计时选择，等到超时进入坏结局。"""
    clock = ManualClock()
    game = SyntheticGame(GameConfig(width=width, height=height), clock)
    count = 0

    def shot(label: str, roi: str, truth: str) -> Shot:
        nonlocal count
        count += 1
        return Shot(label, to_ring(game, count), roi, truth)

    game.handle(Press(Key.ENTER))  # 开始
    intro = NODES["intro"]
    assert isinstance(intro, Dialogue)
    clock.advance(0.2)
    yield shot("逐字显示到一半", "dialogue", game.truth().visible_text)
    for index, line in enumerate(intro.lines):
        clock.advance(1.0)
        yield shot(f"开场第 {index + 1} 句", "dialogue", line.text)
        yield shot(f"开场第 {index + 1} 句的名字", "speaker", line.speaker)
        game.handle(Press(Key.ENTER))
    assert game.truth().scene is SceneType.CHOICE
    yield shot("选项提问", "choice-prompt", game.truth().text)
    yield shot("选项", "choices", "推门进去敲门回应回去睡觉")
    game.handle(Press(Key.ENTER))  # 推门进去
    while game.truth().overlay is None:
        clock.advance(1.0)
        yield shot("地下对话", "dialogue", game.truth().text)
        game.handle(Press(Key.ENTER))
    overlay = game.truth().overlay
    assert overlay is not None
    yield shot("弹窗", "popup-text", overlay.text)
    game.handle(Press(Key.ENTER))
    assert game.truth().scene is SceneType.TIMED_CHOICE
    clock.advance(1.0)
    yield shot("计时选择的提问", "choice-prompt", game.truth().text)
    yield shot("计时选择", "choices", "左边的门右边的门")
    clock.advance(10.0)
    assert game.truth().scene is SceneType.BAD_ENDING
    yield shot("坏结局", "ending-text", game.truth().text)


def edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def compact(text: str) -> str:
    """严格口径：只去掉空白，不做 NFKC。"""
    return "".join(text.split())


@pytest.mark.parametrize(("width", "height"), RESOLUTIONS)
def test_synthetic_screens_are_read_accurately(
    real_engine: OcrEngine, synthetic_adapter: Adapter, width: int, height: int
) -> None:
    errors = characters = 0
    misread: list[str] = []
    for shot in walkthrough(width, height):
        roi = synthetic_adapter.roi(shot.roi).to_pixels(width, height)
        result = recognize_frame(real_engine, shot.frame, roi, shot.roi)
        truth, read = compact(shot.truth), compact(result.text)
        distance = edit_distance(truth, read)
        errors += distance
        characters += len(truth)
        if distance:
            misread.append(f"{shot.label}：{truth!r} 读成 {read!r}")
        assert result.frame_number == shot.frame.frame_number
        for line in result.lines:
            box = line.box
            assert roi.left <= box.left < box.right <= roi.right, shot.label
            assert roi.top <= box.top < box.bottom <= roi.bottom, shot.label
            assert 0.0 <= line.confidence <= 1.0

    accuracy = 1 - errors / characters
    assert accuracy >= MIN_ACCURACY, misread


@pytest.mark.parametrize(("width", "height"), RESOLUTIONS)
def test_names_are_boxed_where_they_are_drawn(
    real_engine: OcrEngine, synthetic_adapter: Adapter, width: int, height: int
) -> None:
    speaker = next(shot for shot in walkthrough(width, height) if shot.roi == "speaker")
    roi = synthetic_adapter.roi("speaker").to_pixels(width, height)

    (line,) = recognize_frame(real_engine, speaker.frame, roi, "speaker").lines

    # 名字居中画在名字栏里：识别框的中心离名字栏的中心不远。
    center = Layout(width, height).speaker_box().center
    box_center = ((line.box.left + line.box.right) / 2, (line.box.top + line.box.bottom) / 2)
    scale = height / 720
    assert abs(box_center[0] - center[0]) <= 8 * scale
    assert abs(box_center[1] - center[1]) <= 8 * scale


def menu_frame(width: int, height: int, labels: Sequence[str]) -> RingFrame:
    layout = Layout(width, height)
    buttons = tuple(
        Button(layout.menu_button(index), label, index == 0) for index, label in enumerate(labels)
    )
    pixels = Renderer(width, height).render(View(MenuView(UI.game_title, buttons), None))
    return RingFrame(1, 0, 1, width, height, width * 4, pixels)


@pytest.mark.parametrize(("width", "height"), RESOLUTIONS)
def test_language_check_on_the_title_menu(
    real_engine: OcrEngine, synthetic_adapter: Adapter, width: int, height: int
) -> None:
    service = OcrService(synthetic_adapter, real_engine, model=real_engine.model)
    genuine = menu_frame(width, height, (UI.menu_start, UI.menu_shop, UI.menu_help))
    # 合成的不符样本：按钮上是别的界面文字（字体子集里只有合成游戏用到的字）。
    other = menu_frame(width, height, (UI.shop_leave, UI.popup_ok, UI.retry))

    async def scenario() -> tuple[bool, bool, int]:
        try:
            matched = await service.check_language(genuine, "title-menu")
            mismatched = await service.check_language(other, "title-menu")
            return matched.passed, mismatched.passed, len(mismatched.mismatches)
        finally:
            await service.aclose()

    assert asyncio.run(scenario()) == (True, False, 3)


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


def entry(entry_id: str, plugin: str, config: dict[str, Any]) -> ResolvedEntry:
    return ResolvedEntry(
        id=entry_id, plugin=plugin, config=config, disabled=False, grant=frozenset()
    )


services: list[OcrService] = []


class Consumer(Plugin[NoConfig]):
    manifest = Manifest(id="ocr-consumer", version="1", tier=Tier.ORDINARY, inject=(OCR.ref,))

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        services.append(ctx.service(OCR))


def test_the_plugin_loads_the_real_models(real_engine: OcrEngine) -> None:
    del real_engine  # 只用来在没有模型时跳过
    frame = menu_frame(1280, 720, (UI.menu_start, UI.menu_shop, UI.menu_help))
    services.clear()

    async def scenario() -> str:
        k = Kernel(
            catalog=catalog(GameAdapterPlugin, OcrPlugin, Consumer),
            core=NoCore(),
            options=KernelOptions(retry_delays=(), activate_timeout=60),
        )
        k.add(
            [
                entry("game", "game-adapter", {"game": "synthetic-game"}),
                entry("ocr", "ocr", {}),
                entry("consumer", "ocr-consumer", {}),
            ]
        )
        await k.settled()
        try:
            assert k.status("ocr").state is State.ACTIVE, k.status("ocr").reasons
            (service,) = services
            return (await service.recognize(frame, "menu-shop")).text
        finally:
            await k.shutdown()

    assert asyncio.run(scenario()) == "商店"
