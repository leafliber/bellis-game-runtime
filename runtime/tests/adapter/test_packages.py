"""逐个校验随仓适配包，并核对合成游戏声明与其渲染和输入。"""

import pytest

from core_protocol.models import MouseButton
from game_runtime.adapter import (
    GAMES_DIR,
    GENERIC_NAMES,
    Adapter,
    load_adapter,
)
from game_runtime.adapter.check import check_games_dir, check_package, package_dirs
from game_runtime.core_link import WindowRule
from game_runtime.ocr import SCRIPT_MIN_RATIO, Script, check_script, script_of
from synthetic_game import GameConfig, Key, SceneType
from synthetic_game.layout import Layout, Rect
from synthetic_game.shell import WINDOW_TITLE
from synthetic_game.story import NODES, UI, Dialogue

SET1 = {Key.ENTER: 0x1C, Key.SPACE: 0x39, Key.ESCAPE: 0x01, Key.UP: 0xE048, Key.DOWN: 0xE050}
"""合成游戏用到的键的 Set 1 扫描码。"""


@pytest.fixture(scope="module")
def synthetic() -> Adapter:
    return load_adapter(GAMES_DIR / "synthetic-game")


def test_every_package_in_the_repository_passes_the_check() -> None:
    directories = package_dirs(GAMES_DIR)

    assert GAMES_DIR / "synthetic-game" in directories
    assert check_games_dir(GAMES_DIR) == []
    for directory in directories:
        assert check_package(directory).id == directory.name


# 合成游戏


def test_synthetic_game_is_fully_declared(synthetic: Adapter) -> None:
    assert synthetic.uncalibrated() == ()


def test_synthetic_window_rule_matches_the_window_shell(synthetic: Adapter) -> None:
    assert synthetic.window_rule() == WindowRule(
        process_name="python.exe", window_class="TkTopLevel", title=WINDOW_TITLE
    )


def test_synthetic_resolution_is_the_default_config(synthetic: Adapter) -> None:
    config = GameConfig()

    assert (synthetic.resolution.width, synthetic.resolution.height) == (
        config.width,
        config.height,
    )


def _layout_rects(layout: Layout) -> dict[str, Rect]:
    options = [layout.choice_option(index) for index in range(3)]
    return {
        "client": layout.full,
        "clock-tower": layout.clock_tower(),
        "speaker": layout.speaker_box(),
        "dialogue": layout.dialogue_text(),
        "continue-marker": layout.continue_marker(),
        "choice-prompt": layout.choice_prompt(),
        "choices": Rect(options[0].left, options[0].top, options[-1].right, options[-1].bottom),
        "timer-bar": layout.timer_bar(),
        "timer-text": layout.rect(460, 629, 820, 670),
        "menu-start": layout.menu_button(0),
        "menu-shop": layout.menu_button(1),
        "menu-help": layout.menu_button(2),
        "shop-list": layout.shop_list(),
        "shop-leave": layout.shop_leave(),
        "popup-text": layout.popup_text(),
        "popup-ok": layout.popup_ok(),
        "ending-text": layout.ending_text(),
        "ending-retry": layout.ending_retry(),
    }


@pytest.mark.parametrize(("width", "height"), [(1280, 720), (2560, 1440)])
def test_synthetic_rois_are_the_render_layout(synthetic: Adapter, width: int, height: int) -> None:
    expected = {
        name: (rect.left, rect.top, rect.right, rect.bottom)
        for name, rect in _layout_rects(Layout(width, height)).items()
    }

    actual = {name: tuple(synthetic.roi(name).to_pixels(width, height)) for name in synthetic.rois}

    assert actual == expected


def test_synthetic_screens_map_to_the_truth_scene_types(synthetic: Adapter) -> None:
    # 合成游戏的真值直接用通用画面类型的名称。
    assert {scene.value for scene in SceneType} <= GENERIC_NAMES
    assert {screen.id: screen.type for screen in synthetic.screens} == {
        "title-menu": SceneType.MENU,
        "dialogue": SceneType.DIALOGUE,
        "choice": SceneType.CHOICE,
        "timed-choice": SceneType.TIMED_CHOICE,
        "shop": SceneType.SHOP,
        "bad-ending": SceneType.BAD_ENDING,
    }


def test_synthetic_language_check_reads_the_title_menu_buttons(synthetic: Adapter) -> None:
    check = synthetic.language.fixed_texts

    assert synthetic.language.tag == "zh-Hans"
    assert script_of(synthetic.language.tag) is Script.HAN
    assert [(item.screen, item.roi, item.text) for item in check] == [
        ("title-menu", "menu-start", UI.menu_start),
        ("title-menu", "menu-shop", UI.menu_shop),
        ("title-menu", "menu-help", UI.menu_help),
    ]


def test_synthetic_dialogue_box_reads_the_name_and_the_line(synthetic: Adapter) -> None:
    [box] = synthetic.require_dialogue_boxes()
    assert (box.screen, box.speaker, box.text, box.continue_signature) == (
        "dialogue",
        "speaker",
        "dialogue",
        "dialogue-continue",
    )
    # 继续标记的签名是对话签名的像素检查加上继续标记里的一点，与对话同级、指向对话。
    signatures = {signature.id: signature for signature in synthetic.signatures}
    dialogue, marker = signatures["dialogue"], signatures["dialogue-continue"]
    assert (marker.screen, marker.priority) == (dialogue.screen, dialogue.priority)
    assert marker.pixels[: len(dialogue.pixels)] == dialogue.pixels
    [point] = marker.pixels[len(dialogue.pixels) :]
    for width, height in [(1280, 720), (2560, 1440), (640, 360)]:
        triangle = Layout(width, height).continue_marker()
        column, row = int(point.at.x * width), int(point.at.y * height)
        assert triangle.left < column < triangle.right - 1
        assert triangle.top < row < triangle.center[1]  # 三角形朝下，上半部分最宽


def test_every_synthetic_line_is_in_the_declared_script(synthetic: Adapter) -> None:
    """文字系统一层在合成游戏的每句台词上都相符（台词是原创的中文）。"""
    lines = [
        line.text for node in NODES.values() if isinstance(node, Dialogue) for line in node.lines
    ]
    assert lines
    for text in lines:
        check = check_script(text, synthetic.language.tag)
        assert check.matches, (text, check.ratio)
        assert check.ratio is not None
        assert check.ratio >= SCRIPT_MIN_RATIO


def test_synthetic_whitelist_covers_the_inputs_the_game_handles(synthetic: Adapter) -> None:
    whitelist = synthetic.whitelist

    assert set(whitelist.press_keys) == set(SET1.values())
    assert set(SET1) == set(Key)
    assert whitelist.click_buttons == (MouseButton.LEFT,)
    assert whitelist.scroll
    assert not whitelist.allows_hold
