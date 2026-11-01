"""合成游戏全部画面上的判别：按适配包的签名与真值逐帧核对，含逐字显示、计时选择、弹窗、播片与三种
坏结局，以及不同分辨率与没有 OCR 的情形。播片不命中任何签名，按持续整屏的动态判出；别的画面不是
播片。

帧由合成游戏渲染、带手动时钟的采集时刻，经 `SceneAnalyzer` 逐帧判别；固定文字项用按真值给出文字的
OCR 替身，真实 OCR 由 `ocr@1` 提供。适配包里锚点引用的摘要就是 `synthetic_game.anchors` 裁出的模板。
"""

from collections.abc import Sequence
from pathlib import Path

import pytest

from game_runtime.adapter import Adapter, PixelRect
from game_runtime.frame_ring import RingFrame
from game_runtime.scene import SceneAnalyzer, SceneState, SignatureJudge, Stability, Verdict
from synthetic_game import (
    Click,
    GameConfig,
    ManualClock,
    SceneType,
    Scroll,
    SyntheticGame,
    Truth,
)
from synthetic_game.anchors import anchor_templates

SCREENS = {
    SceneType.MENU: "title-menu",
    SceneType.DIALOGUE: "dialogue",
    SceneType.CHOICE: "choice",
    SceneType.TIMED_CHOICE: "timed-choice",
    SceneType.SHOP: "shop",
    SceneType.BAD_ENDING: "bad-ending",
}
WITH_TEXTS = {"title-menu", "bad-ending"}
"""签名里有固定文字项的画面。"""
FRAME_INTERVAL = 1 / 30


class TruthOcr:
    """OCR 的替身：按真值给出按钮上的文字（ROI 里有哪些可点目标的中心）。"""

    def __init__(self) -> None:
        self.labels: dict[int, list[tuple[int, int, str]]] = {}

    def record(self, number: int, truth: Truth) -> None:
        self.labels[number] = [(*t.rect.center, t.label) for t in truth.targets if t.label]

    def read_lines(self, frame: RingFrame, rect: PixelRect) -> Sequence[str]:
        return [
            label
            for x, y, label in self.labels.get(frame.frame_number, [])
            if rect.left <= x < rect.right and rect.top <= y < rect.bottom
        ]


class Walk:
    """驱动合成游戏，每次观察渲染一帧、判别并与真值核对。"""

    def __init__(self, adapter: Adapter, data_dir: Path, size: tuple[int, int], *, ocr: bool):
        self.clock = ManualClock()
        self.game = SyntheticGame(GameConfig(*size), self.clock)
        self.ocr = TruthOcr() if ocr else None
        self.analyzer = SceneAnalyzer(SignatureJudge.load(adapter, data_dir))
        assert self.analyzer.judge.problems == ()
        self.number = 0
        self.seen: set[str] = set()
        self.endings: set[str] = set()
        self.history: list[tuple[Truth, SceneState]] = []
        self.playing: list[int] = []
        """看播片时在播片中的各帧在 `history` 里的下标。"""

    def observe(self) -> SceneState:
        self.number += 1
        frame = self.game.frame()
        truth = self.game.truth()
        if self.ocr is not None:
            self.ocr.record(self.number, truth)
        ring = RingFrame(
            self.number,
            round(self.clock.now() * 1e9),
            1,
            frame.width,
            frame.height,
            frame.stride,
            frame.data,
        )
        state = self.analyzer.analyze(ring, self.ocr)
        assert (state.verdict, state.screen) == self.expected(truth), (truth, state.signatures)
        assert (state.frame_number, state.width, state.height) == (
            self.number,
            frame.width,
            frame.height,
        )
        if truth.overlay is None and truth.scene in SCREENS:
            self.seen.add(SCREENS[truth.scene])
        if truth.scene is SceneType.BAD_ENDING:
            self.endings.add(truth.node)
        self.history.append((truth, state))
        return state

    def expected(self, truth: Truth) -> tuple[Verdict, str | None]:
        if truth.overlay is not None:
            return Verdict.NONE, None  # 弹窗不是画面
        if truth.scene is SceneType.CUTSCENE:
            return Verdict.NONE, None  # 播片不是画面，不命中任何签名
        screen = SCREENS[truth.scene]
        if self.ocr is None and screen in WITH_TEXTS:
            return Verdict.UNDETERMINED, None
        return Verdict.SCREEN, screen

    def wait(self, seconds: float) -> SceneState:
        self.clock.advance(seconds)
        return self.observe()

    def click(self, target: str) -> SceneState:
        rect = next(t.rect for t in self.game.truth().targets if t.id == target)
        effect = self.game.handle(Click(*rect.center))
        assert effect.target == target
        self.clock.advance(FRAME_INTERVAL)
        return self.observe()

    def settle(self) -> None:
        """画面不再变化后稳定下来。"""
        self.wait(FRAME_INTERVAL)
        assert self.wait(0.3).stability is Stability.STABLE

    def watch_cutscene(self) -> None:
        """刚进入播片：逐帧看完，回到选项后再看 2 秒。每帧都是动帧，连成一段；从播片的第一帧起覆盖
        2 秒时判出播片，开始时刻是这第一帧。回到选项的那一帧签名命中，起即不再在播片中，结束时刻
        是这一帧。"""
        start = len(self.history) - 1
        (cutscene, entered), (_, before) = self.history[start], self.history[start - 1]
        assert cutscene.scene is SceneType.CUTSCENE
        assert before.stability is Stability.STABLE
        while self.game.truth().scene is SceneType.CUTSCENE:
            change = self.wait(FRAME_INTERVAL)
            assert change.changed_fraction >= 0.1
            assert change.stability is not Stability.STABLE
        switch = len(self.history) - 1
        assert self.history[switch][0].scene is SceneType.CHOICE
        for _ in range(60):
            self.wait(FRAME_INTERVAL)
        states = [state for _, state in self.history]
        playing = [n for n in range(start, len(states)) if states[n].playing]
        first, last = playing[0], playing[-1]
        assert playing == list(range(first, last + 1))
        self.playing += playing
        # 距播片的第一帧不短于 2 秒的第一帧起在播片中，之后的帧都在播片中。
        second = 2_000_000_000
        assert states[first].capture_ns - entered.capture_ns >= second
        assert states[first - 1].capture_ns - entered.capture_ns < second
        assert first < switch
        assert {states[n].playing_since_ns for n in playing} == {entered.capture_ns}
        assert states[switch].screen is not None
        assert last + 1 == switch
        assert states[switch].playing_ended_ns == states[switch].capture_ns
        assert [n for n, s in enumerate(states) if s.playing_ended_ns is not None] == [switch]

    def read_dialogue(self, *, watch_typing: bool = False) -> None:
        """读完当前的对话节点：每句等逐字显示完再推进。"""
        node = self.game.truth().node
        while (
            (now := self.game.truth()).scene is SceneType.DIALOGUE
            and now.node == node
            and now.overlay is None
        ):
            truth = self.game.truth()
            if truth.visible_text != truth.text:
                if watch_typing:
                    # 逐字显示期间每帧多一个字：变化中，签名照样判出对话。
                    while (truth := self.game.truth()).visible_text != truth.text:
                        assert self.wait(FRAME_INTERVAL).stability is not Stability.STABLE
                else:
                    self.wait(len(truth.text) / self.game.config.text_cps)
                self.settle()
            self.click("dialogue.box")


def play_everything(walk: Walk) -> None:
    """走遍合成游戏的全部画面、弹窗与三种坏结局。"""
    assert walk.observe().stability is Stability.TRANSITION  # 第一帧没有可比的上一帧
    walk.settle()

    # 说明弹窗。
    walk.click("menu.help")
    walk.settle()
    walk.click("popup.ok")

    # 商店：滚动、购买、金币不足的弹窗、离开。
    assert walk.click("menu.shop").stability is Stability.TRANSITION
    walk.settle()
    shop_list = walk.game.truth().targets[0].rect
    walk.game.handle(Scroll(*shop_list.center, notches=-2))
    walk.wait(FRAME_INTERVAL)
    walk.click("shop.item.4")
    walk.click("shop.item.5")  # 买了旧地图后只剩 25 金币，买不起铜钥匙
    assert walk.game.truth().overlay is not None
    walk.click("popup.ok")
    walk.click("shop.leave")

    # 开场对话，逐字显示；选项进插曲、看完播片再回来；回去睡觉的坏结局。
    walk.click("menu.start")
    walk.read_dialogue(watch_typing=True)
    walk.settle()
    walk.click("choice.1")
    walk.read_dialogue()
    walk.watch_cutscene()
    walk.settle()
    walk.click("choice.2")
    walk.settle()

    # 重来，下到地下：读完弹窗后进入计时选择，等到超时的坏结局。
    walk.click("ending.retry")
    walk.read_dialogue()
    walk.click("choice.0")
    walk.read_dialogue()
    assert walk.game.truth().overlay is not None
    walk.settle()
    walk.click("popup.ok")
    timer = walk.game.truth().timer_remaining
    assert timer is not None
    while (remaining := walk.game.truth().timer_remaining) is not None and remaining > 0.2:
        # 计时条与剩余秒数一直在走：画面不稳定，签名照样判出计时选择。
        assert walk.wait(0.2).stability is not Stability.STABLE
    walk.wait(0.3)
    assert walk.game.truth().node == "late"
    walk.settle()

    # 再来一次：烫手的左门是坏结局，右门通向结尾对话，读完回到标题菜单。
    for door, then in (("choice.0", SceneType.BAD_ENDING), ("choice.1", SceneType.DIALOGUE)):
        walk.click("ending.retry")
        walk.read_dialogue()
        walk.click("choice.0")
        walk.read_dialogue()
        walk.click("popup.ok")
        walk.click(door)
        assert walk.game.truth().scene is then
    walk.read_dialogue()
    assert walk.game.truth().scene is SceneType.MENU
    walk.settle()

    assert walk.seen == set(SCREENS.values())
    assert walk.endings == {"sleep", "late", "burn"}
    # 别的画面（逐字显示、计时条、换画面的转场）都不是播片。
    assert [n for n, (_, state) in enumerate(walk.history) if state.playing] == walk.playing


@pytest.mark.parametrize("size", [(1280, 720), (2560, 1440)])
def test_every_screen_is_recognised_with_ocr(
    synthetic: Adapter, data_dir: Path, size: tuple[int, int]
) -> None:
    play_everything(Walk(synthetic, data_dir, size, ocr=True))


def test_without_ocr_the_screens_with_fixed_texts_are_undetermined(
    synthetic: Adapter, data_dir: Path
) -> None:
    play_everything(Walk(synthetic, data_dir, (1280, 720), ocr=False))


@pytest.mark.parametrize("size", [(1920, 1080), (640, 360)])
def test_signatures_scale_to_other_resolutions(
    synthetic: Adapter, data_dir: Path, size: tuple[int, int]
) -> None:
    walk = Walk(synthetic, data_dir, size, ocr=True)
    walk.observe()
    walk.click("menu.start")
    walk.read_dialogue()
    walk.click("choice.0")
    walk.read_dialogue()
    walk.click("popup.ok")
    assert walk.wait(1.0).screen == "timed-choice"
    assert walk.seen == {"title-menu", "dialogue", "choice", "timed-choice"}


def test_the_package_references_the_rendered_templates(synthetic: Adapter) -> None:
    referenced = {
        anchor.template_sha256 for signature in synthetic.signatures for anchor in signature.anchors
    }

    assert referenced == {template.sha256 for template in anchor_templates()}
    assert all(signature.calibrated for signature in synthetic.signatures)
    assert {signature.screen for signature in synthetic.signatures} == set(SCREENS.values())


def test_the_same_frames_give_the_same_states(synthetic: Adapter, data_dir: Path) -> None:
    """判别只取决于依次处理的帧：同样的帧序列（回放）得到逐帧相同的结果。"""

    def states() -> list[SceneState]:
        walk = Walk(synthetic, data_dir, (1280, 720), ocr=True)
        result = [walk.observe(), walk.wait(0.3), walk.click("menu.start")]
        result += [walk.wait(FRAME_INTERVAL) for _ in range(5)]
        return result

    assert states() == states()
