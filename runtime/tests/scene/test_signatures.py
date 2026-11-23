"""画面签名的判别：组成项、叠加优先级与不可判、分辨率、尚未标定、模板问题与 OCR 接口。

规则见 `docs/modules/perception.md` 的“画面签名的判别”。帧都由合成游戏渲染，签名或取自合成游戏的
适配包，或在测试里按它的 ROI 另写。
"""

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from game_runtime.adapter import Adapter, PixelRect
from game_runtime.frame_ring import RingFrame
from game_runtime.scene import (
    Outcome,
    SignatureJudge,
    SignatureResult,
    Verdict,
    bgra_view,
    decide,
    resolution_problem,
    store_template,
)
from game_runtime.scene.templates import template_path
from synthetic_game import Click, GameConfig, Key, ManualClock, Press, SyntheticGame
from synthetic_game.anchors import anchor_templates, encode_png


def ring_frame(game: SyntheticGame, number: int = 1) -> RingFrame:
    frame = game.frame()
    return RingFrame(number, number, 1, frame.width, frame.height, frame.stride, frame.data)


def game_at(width: int = 1280, height: int = 720, *, target: str | None = None) -> SyntheticGame:
    """停在标题菜单的合成游戏；给出 `target` 时先点它。"""
    game = SyntheticGame(GameConfig(width, height), ManualClock())
    if target is not None:
        rect = next(t.rect for t in game.truth().targets if t.id == target)
        game.handle(Click(*rect.center))
    return game


def with_signatures(adapter: Adapter, *signatures: dict[str, Any]) -> Adapter:
    """按 `adapter` 的 ROI 与画面另写签名，去掉引用原签名的对白框与重来入口。"""
    data = adapter.model_dump(mode="python")
    data["signatures"] = list(signatures)
    data["dialogue_boxes"] = []
    data["restarts"] = {}
    return Adapter.model_validate(data)


def judge(
    adapter: Adapter,
    game: SyntheticGame,
    data_dir: Path,
    reader: "Reader | None" = None,
) -> tuple[Verdict, str | None, tuple[SignatureResult, ...]]:
    frame = ring_frame(game)
    result = SignatureJudge.load(adapter, data_dir).judge(frame, bgra_view(frame), reader)
    return result.verdict, result.screen, result.signatures


class Reader:
    """OCR 的替身：给出固定的各行，记下被调用的 ROI。"""

    def __init__(self, lines: Sequence[str] = (), error: Exception | None = None) -> None:
        self.lines = tuple(lines)
        self.error = error
        self.calls: list[PixelRect] = []

    def read_lines(self, frame: RingFrame, rect: PixelRect) -> Sequence[str]:
        self.calls.append(rect)
        if self.error is not None:
            raise self.error
        return self.lines


# 叠加优先级与不可判


def result(signature: str, screen: str, outcome: Outcome, priority: int = 0) -> SignatureResult:
    return SignatureResult(signature, screen, priority, outcome, None, ())


MATCH, MISS, UNKNOWN = Outcome.MATCH, Outcome.NO_MATCH, Outcome.UNDETERMINED


@pytest.mark.parametrize(
    ("results", "verdict", "screen", "matched"),
    [
        ([], Verdict.NONE, None, ()),
        ([result("a", "x", MISS)], Verdict.NONE, None, ()),
        ([result("a", "x", MATCH), result("b", "y", MISS)], Verdict.SCREEN, "x", ("a",)),
        # 同一画面的两个签名都命中仍是恰好一个画面。
        ([result("a", "x", MATCH), result("b", "x", MATCH)], Verdict.SCREEN, "x", ("a", "b")),
        ([result("a", "x", MATCH), result("b", "y", MATCH)], Verdict.AMBIGUOUS, None, ("a", "b")),
        # 叠加优先级：只取最高的一档。
        (
            [result("a", "x", MATCH), result("b", "y", MATCH, priority=1)],
            Verdict.SCREEN,
            "y",
            ("b",),
        ),
        (
            [
                result("a", "x", MATCH),
                result("b", "y", MATCH, priority=1),
                result("c", "z", MATCH, priority=1),
            ],
            Verdict.AMBIGUOUS,
            None,
            ("b", "c"),
        ),
        ([result("a", "x", UNKNOWN)], Verdict.UNDETERMINED, None, ()),
        ([result("a", "x", UNKNOWN), result("b", "y", MISS)], Verdict.UNDETERMINED, None, ()),
        # 优先级更低的不可判签名无论命中与否都被叠加排除。
        (
            [result("a", "x", MATCH, priority=1), result("b", "y", UNKNOWN)],
            Verdict.SCREEN,
            "x",
            ("a",),
        ),
        # 同级、指向同一画面的不可判不改变结果；指向别的画面的可能造成歧义。
        ([result("a", "x", MATCH), result("b", "x", UNKNOWN)], Verdict.SCREEN, "x", ("a",)),
        ([result("a", "x", MATCH), result("b", "y", UNKNOWN)], Verdict.UNDETERMINED, None, ()),
        # 优先级更高的不可判可能把结果换掉。
        (
            [result("a", "x", MATCH), result("b", "x", UNKNOWN, priority=1)],
            Verdict.UNDETERMINED,
            None,
            (),
        ),
        # 已经歧义时，同级的不可判改变不了；更高的可以。
        (
            [result("a", "x", MATCH), result("b", "y", MATCH), result("c", "z", UNKNOWN)],
            Verdict.AMBIGUOUS,
            None,
            ("a", "b"),
        ),
        (
            [
                result("a", "x", MATCH),
                result("b", "y", MATCH),
                result("c", "x", UNKNOWN, priority=1),
            ],
            Verdict.UNDETERMINED,
            None,
            (),
        ),
    ],
)
def test_exactly_one_screen_after_overlay_priority(
    results: list[SignatureResult], verdict: Verdict, screen: str | None, matched: tuple[str, ...]
) -> None:
    judgement = decide(results)

    assert (judgement.verdict, judgement.screen, judgement.matched) == (verdict, screen, matched)
    assert judgement.signatures == tuple(results)


# 组成项


def test_pixel_checks_compare_each_channel_within_the_tolerance(
    synthetic: Adapter, data_dir: Path
) -> None:
    # 标题菜单左上角的底色是 #1A2440。
    def menu(color: str, tolerance: int, at: tuple[float, float] = (0.03125, 0.055556)) -> Adapter:
        pixel = {"at": at, "color": color, "tolerance": tolerance}
        return with_signatures(synthetic, {"id": "s", "screen": "title-menu", "pixels": [pixel]})

    game = game_at()
    assert judge(menu("#1A2440", 0), game, data_dir)[0] is Verdict.SCREEN
    assert judge(menu("#202A46", 6), game, data_dir)[0] is Verdict.SCREEN
    verdict, _, [miss] = judge(menu("#212A46", 6), game, data_dir)
    assert verdict is Verdict.NONE
    assert miss.detail == "像素检查 [0]：#1A2440 与 #212A46 之差超过 6"
    # 落在最后一列、最后一行。
    assert judge(menu("#1A2440", 0, (1.0, 1.0)), game, data_dir)[0] is Verdict.SCREEN


@pytest.mark.parametrize(("width", "height"), [(1280, 720), (2560, 1440), (1920, 1080), (640, 360)])
def test_anchor_templates_are_scaled_to_the_client_size(
    synthetic: Adapter, data_dir: Path, width: int, height: int
) -> None:
    [template] = anchor_templates()
    anchor = {"roi": "clock-tower", "template_sha256": template.sha256, "min_score": 0.9}
    adapter = with_signatures(synthetic, {"id": "s", "screen": "dialogue", "anchors": [anchor]})

    verdict, screen, [hit] = judge(adapter, game_at(width, height, target="menu.start"), data_dir)
    assert (verdict, screen) == (Verdict.SCREEN, "dialogue")
    [score] = hit.anchors
    assert score.score is not None
    assert score.score >= 0.95
    # 位置在客户区物理像素里，就是钟面模板缩放后所在的地方。
    sx, sy = width / 1280, height / 720
    location = score.location
    assert location is not None
    assert abs(location.left - 580 * sx) <= 1
    assert abs(location.top - 90 * sy) <= 1
    assert (location.right - location.left, location.bottom - location.top) == (
        round(120 * sx),
        round(120 * sy),
    )

    # 选项画面的钟楼位置是提问面板：得分低，不命中。
    choice_game = game_at(width, height, target="menu.start")
    for _ in range(3):  # 推进三句开场对白：每句先补全再进入下一句
        box = next(t.rect for t in choice_game.truth().targets if t.id == "dialogue.box")
        choice_game.handle(Click(*box.center))
        choice_game.handle(Click(*box.center))
    assert choice_game.truth().scene.value == "选择"
    verdict, _, [miss] = judge(adapter, choice_game, data_dir)
    assert verdict is Verdict.NONE
    assert miss.outcome is Outcome.NO_MATCH
    assert miss.detail is not None
    assert miss.detail.startswith("锚点 [0]：得分")


def test_an_anchor_that_cannot_be_judged_makes_the_signature_undetermined(
    synthetic: Adapter, root: Path
) -> None:
    [template] = anchor_templates()
    data_dir = root / "data"
    pixel = {"at": [0.5, 0.208333], "color": "#F5F5F0", "tolerance": 6}
    anchor = {"roi": "clock-tower", "template_sha256": template.sha256, "min_score": 0.9}
    adapter = with_signatures(
        synthetic, {"id": "s", "screen": "dialogue", "pixels": [pixel], "anchors": [anchor]}
    )
    dialogue = game_at(target="menu.start")

    # 模板不在：读不出的模板记为问题，锚点不可判。
    loaded = SignatureJudge.load(adapter, data_dir)
    assert len(loaded.problems) == 1
    assert "没有模板文件" in loaded.problems[0]
    verdict, _, [unknown] = judge(adapter, dialogue, data_dir)
    assert (verdict, unknown.outcome) == (Verdict.UNDETERMINED, Outcome.UNDETERMINED)
    assert unknown.detail is not None
    assert unknown.detail.startswith("锚点 [0]：没有模板文件")
    # 前面的像素检查不通过时照样不命中，不可判不掩盖不命中。
    assert judge(adapter, game_at(), data_dir)[0] is Verdict.NONE

    # 摘要不符：文件内容被换掉。
    store_template(data_dir, "synthetic-game", template.png)
    path = template_path(data_dir, "synthetic-game", template.sha256)
    path.write_bytes(encode_png(2, 1, [bytes([1, 2, 3, 4, 5, 6])]))
    verdict, _, [unknown] = judge(adapter, dialogue, data_dir)
    assert verdict is Verdict.UNDETERMINED
    assert unknown.detail is not None
    assert "摘要不符" in unknown.detail


def test_a_template_larger_than_its_roi_cannot_be_judged(synthetic: Adapter, root: Path) -> None:
    data_dir = root / "data"
    rows = [bytes([(x * 7 + y) % 256 for x in range(400 * 3)]) for y in range(40)]
    sha256 = store_template(data_dir, "synthetic-game", encode_png(400, 40, rows))
    anchor = {"roi": "clock-tower", "template_sha256": sha256, "min_score": 0.5}
    adapter = with_signatures(synthetic, {"id": "s", "screen": "dialogue", "anchors": [anchor]})

    verdict, _, [unknown] = judge(adapter, game_at(target="menu.start"), data_dir)
    assert verdict is Verdict.UNDETERMINED
    assert unknown.detail == "锚点 [0]：缩放后的模板 400×40 比 ROI clock-tower 的 160×360 大"


# 固定文字


def menu_with_text(adapter: Adapter, text: str = "开始") -> Adapter:
    pixel = {"at": [0.03125, 0.055556], "color": "#1A2440", "tolerance": 6}
    return with_signatures(
        adapter,
        {
            "id": "s",
            "screen": "title-menu",
            "pixels": [pixel],
            "texts": [{"roi": "menu-start", "text": text}],
        },
    )


def test_fixed_texts_go_through_the_ocr_interface(synthetic: Adapter, data_dir: Path) -> None:
    adapter = menu_with_text(synthetic, "开 始")
    menu = game_at()

    # 各行依次连接、去掉空白后比对。
    reader = Reader(["开", "始　"])
    assert judge(adapter, menu, data_dir, reader)[:2] == (Verdict.SCREEN, "title-menu")
    assert reader.calls == [synthetic.roi("menu-start").to_pixels(1280, 720)]

    verdict, _, [miss] = judge(adapter, menu, data_dir, Reader(["开场"]))
    assert verdict is Verdict.NONE
    assert miss.detail == "固定文字 [0]：ROI menu-start 里没有“开 始”"


def test_without_ocr_fixed_texts_are_undetermined_never_passed(
    synthetic: Adapter, data_dir: Path
) -> None:
    adapter = menu_with_text(synthetic)
    menu = game_at()

    verdict, _, [unknown] = judge(adapter, menu, data_dir, None)
    assert (verdict, unknown.outcome, unknown.detail) == (
        Verdict.UNDETERMINED,
        Outcome.UNDETERMINED,
        "固定文字 [0]：OCR 不可用",
    )
    verdict, _, [unknown] = judge(adapter, menu, data_dir, Reader(error=RuntimeError("模型没加载")))
    assert verdict is Verdict.UNDETERMINED
    assert unknown.detail == "固定文字 [0]：OCR 出错：模型没加载"
    # 前面的项不通过时不调用 OCR。
    reader = Reader(["开始"])
    assert judge(adapter, game_at(target="menu.shop"), data_dir, reader)[0] is Verdict.NONE
    assert reader.calls == []


def test_ocr_results_are_reused_only_with_the_same_reader_and_pixels(
    synthetic: Adapter, data_dir: Path
) -> None:
    adapter = menu_with_text(synthetic)
    signature_judge = SignatureJudge.load(adapter, data_dir)
    reader = Reader(["开始"])
    game = game_at()

    def run(number: int, current: Reader | None) -> Verdict:
        frame = ring_frame(game, number)
        return signature_judge.judge(frame, bgra_view(frame), current).verdict

    assert run(1, reader) is Verdict.SCREEN
    assert run(2, reader) is Verdict.SCREEN
    assert len(reader.calls) == 1
    # 即使像素未变，OCR 失效后也不能沿用原来的签名。
    assert run(3, None) is Verdict.UNDETERMINED
    assert run(4, reader) is Verdict.SCREEN
    assert len(reader.calls) == 2
    # 光标移到“商店”，“开始”按钮的像素变了：重新读。
    game.handle(Press(Key.DOWN))
    assert game.truth().selected == "menu.shop"
    assert run(5, reader) is Verdict.SCREEN
    assert len(reader.calls) == 3


# 分辨率与尚未标定


def test_a_mismatched_aspect_ratio_disables_signatures(
    synthetic: Adapter, data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    signature_judge = SignatureJudge.load(synthetic, data_dir)
    game = game_at(1280, 800)

    with caplog.at_level(logging.WARNING, logger="game_runtime.scene.signatures"):
        for number in (1, 2):
            frame = ring_frame(game, number)
            judgement = signature_judge.judge(frame, bgra_view(frame), Reader(["开始"]))
            assert (judgement.verdict, judgement.screen, judgement.signatures) == (
                Verdict.DISABLED,
                None,
                (),
            )
            assert judgement.detail == (
                "分辨率不符：帧为 1280×800，适配包期望 1280×720，宽高比相差超过 1%"
            )
    # 每次尺寸变化只告警一次。
    assert len(caplog.records) == 1

    assert resolution_problem(1366, 768, synthetic.resolution) is None
    assert resolution_problem(2560, 1440, synthetic.resolution) is None
    assert resolution_problem(1280, 740, synthetic.resolution) is not None


def test_uncalibrated_signatures_take_no_part(partial_adapter: Adapter, data_dir: Path) -> None:
    signature_judge = SignatureJudge.load(partial_adapter, data_dir)
    assert signature_judge.uncalibrated == ("marker",)
    assert [problem.split("：")[0] for problem in signature_judge.problems] == [
        "签名 icon 的锚点 [0]"
    ]
    frame = ring_frame(game_at(2560, 1440))
    judgement = signature_judge.judge(frame, bgra_view(frame), None)
    assert judgement.verdict is Verdict.UNDETERMINED
    assert [(result.id, result.outcome) for result in judgement.signatures] == [
        ("icon", Outcome.UNDETERMINED)
    ]
