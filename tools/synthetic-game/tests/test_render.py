"""渲染：帧的尺寸与像素格式（BGRA8，行跨度为宽乘 4）。

另核对帧号只在画面变化时增加、按钮画在可点区域上、同一画面像素相同。
"""

import pytest
from synthetic_game import (
    PIXEL_FORMAT,
    Click,
    Frame,
    GameConfig,
    Key,
    ManualClock,
    Press,
    SyntheticGame,
)
from synthetic_game.render import BUTTON, BUTTON_SELECTED, MENU_BACKGROUND, Color


def pixel(frame: Frame, x: int, y: int) -> tuple[int, int, int, int]:
    offset = y * frame.stride + x * 4
    blue, green, red, alpha = frame.data[offset : offset + 4]
    return blue, green, red, alpha


def bgra(color: Color) -> tuple[int, int, int, int]:
    red, green, blue = color
    return blue, green, red, 255


@pytest.mark.parametrize(("width", "height"), [(1280, 720), (640, 360), (800, 600), (1920, 1080)])
def test_frame_is_bgra8_at_the_configured_resolution(width: int, height: int) -> None:
    frame = SyntheticGame(GameConfig(width, height), ManualClock()).frame()

    assert PIXEL_FORMAT == "BGRA8"
    assert (frame.width, frame.height, frame.stride) == (width, height, width * 4)
    assert len(frame.data) == frame.stride * height
    assert frame.data[3::4] == b"\xff" * (width * height)  # 不透明


def test_default_resolution_is_1280x720() -> None:
    frame = SyntheticGame(clock=ManualClock()).frame()

    assert (frame.width, frame.height) == (1280, 720)


def test_channels_are_in_bgra_order() -> None:
    frame = SyntheticGame(GameConfig(), ManualClock()).frame()

    assert pixel(frame, 5, 5) == bgra(MENU_BACKGROUND)


def test_buttons_are_drawn_on_their_click_targets() -> None:
    game = SyntheticGame(GameConfig(), ManualClock())
    frame = game.frame()
    targets = {target.id: target.rect for target in game.truth().targets}

    for target_id, expected in (("menu.start", BUTTON_SELECTED), ("menu.shop", BUTTON)):
        rect = targets[target_id]
        # 按钮左侧的竖直中线上没有文字，也避开了圆角。
        assert pixel(frame, rect.left + rect.width // 10, rect.center[1]) == bgra(expected)
        assert pixel(frame, rect.left - 2, rect.center[1]) == bgra(MENU_BACKGROUND)


def test_frame_number_increases_only_when_the_picture_changes() -> None:
    game = SyntheticGame(GameConfig(), ManualClock())
    first = game.frame()

    assert first.number == 1
    assert game.frame() is first
    game.handle(Click(0, 0))  # 未命中，画面不变
    assert game.frame() is first

    game.handle(Press(Key.DOWN))
    second = game.frame()
    assert second.number == 2
    assert second.data != first.data


def test_typing_produces_a_new_frame_per_character() -> None:
    clock = ManualClock()
    game = SyntheticGame(GameConfig(text_cps=8.0), clock)
    game.handle(Press(Key.ENTER))
    typing = game.frame()

    clock.advance(0.125)  # 第一个字出现
    assert game.frame().number == typing.number + 1
    clock.advance(0.0625)  # 不到下一个字
    assert game.frame().number == typing.number + 1


def test_the_same_picture_renders_to_the_same_pixels() -> None:
    first = SyntheticGame(GameConfig(640, 360), ManualClock())
    second = SyntheticGame(GameConfig(640, 360), ManualClock())
    for game in (first, second):
        game.handle(Press(Key.ENTER))

    assert first.frame().data == second.frame().data
