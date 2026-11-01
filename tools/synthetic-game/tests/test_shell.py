"""窗口外壳：键名与滚轮的转换、参数解析、重画的排定；有显示时建一个真实的 Tk 窗口核对客户区、
事件转换与重画。

没有 tk 或没有显示（如 Linux 容器）时跳过要建窗的测试。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from PIL import Image

from synthetic_game import (
    EffectKind,
    GameConfig,
    Key,
    ManualClock,
    MouseButton,
    SceneType,
    SyntheticGame,
)
from synthetic_game.shell import (
    DEFAULT_REDRAW_HZ,
    KEYSYMS,
    TK_BUTTONS,
    WINDOW_CLASS,
    WINDOW_TITLE,
    RedrawSchedule,
    Shell,
    ShellOptions,
    WheelAccumulator,
    parse_args,
)

if TYPE_CHECKING:
    import tkinter


def test_keysyms_map_to_the_game_keys() -> None:
    assert KEYSYMS["Return"] is Key.ENTER
    assert KEYSYMS["space"] is Key.SPACE
    assert KEYSYMS["Escape"] is Key.ESCAPE
    assert (KEYSYMS["Up"], KEYSYMS["Down"]) == (Key.UP, Key.DOWN)
    assert set(KEYSYMS.values()) == set(Key)


def test_tk_buttons_map_to_mouse_buttons() -> None:
    assert TK_BUTTONS == {1: MouseButton.LEFT, 2: MouseButton.MIDDLE, 3: MouseButton.RIGHT}


def test_wheel_deltas_accumulate_into_whole_notches() -> None:
    wheel = WheelAccumulator()

    assert wheel.add(120) == 1
    assert wheel.add(-240) == -2
    assert wheel.add(60) == 0
    assert wheel.add(60) == 1
    assert wheel.add(-30) == 0
    assert wheel.add(-90) == -1


def test_command_line_sets_the_configuration() -> None:
    assert parse_args([]) == ShellOptions(GameConfig(), 30.0)
    assert DEFAULT_REDRAW_HZ == 30.0
    assert parse_args(
        [
            *("--width", "800", "--height", "600", "--text-cps", "12"),
            *("--choice-seconds", "3", "--cutscene-seconds", "6", "--redraw-hz", "60"),
        ]
    ) == ShellOptions(GameConfig(800, 600, 12.0, 3.0, 6.0), 60.0)
    assert parse_args(["--autoplay", "1.5"]) == ShellOptions(GameConfig(), 30.0, 1.5)


@pytest.mark.parametrize(
    "argv",
    [
        ["--width", "10"],
        ["--cutscene-seconds", "0"],
        ["--redraw-hz", "0"],
        ["--redraw-hz", "1001"],
        ["--redraw-hz", "nan"],
        ["--autoplay", "0"],
        ["--autoplay", "nan"],
    ],
)
def test_invalid_command_line_is_rejected(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(argv)


def test_redraws_keep_a_fixed_rate_without_drifting() -> None:
    schedule = RedrawSchedule(30, start=0.0)
    # 每次重画耗时不同，下一次仍排在 1/30 秒的整数倍上。
    assert schedule.delay_ms(0.005) == 28
    assert schedule.delay_ms(0.040) == 27
    assert schedule.delay_ms(0.070) == 30
    # 落后超过一个周期：不追赶，尽快重画一次，再从那时起按周期排。
    assert schedule.delay_ms(1.0) == 1
    assert schedule.delay_ms(1.001) == 32


def test_redraw_rate_must_be_positive() -> None:
    with pytest.raises(ValueError, match="重画频率"):
        RedrawSchedule(0, start=0.0)


def test_window_identity_is_fixed() -> None:
    assert WINDOW_TITLE == "Bellis Synthetic Game"
    assert WINDOW_CLASS == "BellisSyntheticGame"


@pytest.fixture(scope="module")
def interpreter() -> Iterator[tkinter.Tk]:
    """整个模块只建一个 Tk 解释器。

    在 Windows 上，pytest 按文件描述符捕获输出时，同一进程里销毁 Tk 解释器后再建新的，偶尔会读不到
    Tk 自己的库文件；只建一个就不会。有显示却建不起来时测试失败，不跳过。
    """
    tk = pytest.importorskip("tkinter")
    if sys.platform != "win32" and not os.environ.get("DISPLAY"):
        pytest.skip("没有显示：DISPLAY 未设置")
    interpreter: tkinter.Tk = tk.Tk(className=WINDOW_CLASS)
    interpreter.withdraw()
    try:
        yield interpreter
    finally:
        interpreter.destroy()


@pytest.fixture
def root(interpreter: tkinter.Tk) -> Iterator[tkinter.Toplevel]:
    tk = pytest.importorskip("tkinter")
    window: tkinter.Toplevel = tk.Toplevel(interpreter)
    try:
        yield window
    finally:
        window.destroy()


def make_shell(root: tkinter.Toplevel) -> Shell:
    shell = Shell(root, SyntheticGame(GameConfig(320, 180), ManualClock()))
    root.update()
    return shell


def test_window_client_area_is_the_configured_resolution(root: tkinter.Toplevel) -> None:
    shell = make_shell(root)

    assert root.title() == WINDOW_TITLE
    assert (root.winfo_width(), root.winfo_height()) == (320, 180)
    assert (shell.screen.winfo_width(), shell.screen.winfo_height()) == (320, 180)
    assert (shell.screen.winfo_x(), shell.screen.winfo_y()) == (0, 0)


def test_mouse_press_in_the_window_reaches_the_state_machine(root: tkinter.Toplevel) -> None:
    shell = make_shell(root)
    shown = shell.game.frame().number
    target = shell.game.truth().targets[1]
    x, y = target.rect.center

    shell.screen.event_generate(
        "<ButtonPress-1>",
        x=x,
        y=y,
        rootx=shell.screen.winfo_rootx() + x,
        rooty=shell.screen.winfo_rooty() + y,
    )

    effect = shell.game.truth().last_effect
    assert effect is not None
    assert (effect.kind, effect.target) == (EffectKind.HIT, target.id)
    assert shell.game.truth().scene is SceneType.SHOP
    assert shell.game.frame().number == shown + 1


def test_key_press_reaches_the_state_machine(root: tkinter.Toplevel) -> None:
    shell = make_shell(root)

    shell.press_keysym("Down")
    shell.press_keysym("F1")  # 没有绑定的键直接丢弃

    effect = shell.game.truth().last_effect
    assert effect is not None
    assert (effect.kind, effect.target) == (EffectKind.KEY, "menu.shop")


def test_redraw_repaints_the_window_even_when_the_picture_is_unchanged(
    root: tkinter.Toplevel, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = make_shell(root)
    pasted: list[Image.Image] = []
    paste = shell.photo.paste

    def counting_paste(image: Image.Image) -> None:
        pasted.append(image)
        paste(image)

    monkeypatch.setattr(shell.photo, "paste", counting_paste)
    shown = shell.game.frame().number

    shell.redraw()
    shell.redraw()
    assert shell.refresh() is False, "refresh alone does not repaint an unchanged picture"
    assert shell.game.frame().number == shown
    assert len(pasted) == 2

    shell.press_keysym("Down")  # 光标移动，画面变化：立即更新，不等下一次重画
    assert shell.game.frame().number == shown + 1
    assert len(pasted) == 3


def test_shell_rejects_a_bad_redraw_rate(root: tkinter.Toplevel) -> None:
    with pytest.raises(ValueError, match="重画频率"):
        Shell(root, SyntheticGame(GameConfig(320, 180), ManualClock()), redraw_hz=-1)
