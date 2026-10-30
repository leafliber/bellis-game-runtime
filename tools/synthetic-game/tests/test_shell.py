"""窗口外壳：键名与滚轮的转换、参数解析；有显示时建一个真实的 Tk 窗口核对客户区与事件转换。

没有 tk 或没有显示（如 Linux 容器）时跳过要建窗的测试。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

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
    KEYSYMS,
    TK_BUTTONS,
    WINDOW_CLASS,
    WINDOW_TITLE,
    Shell,
    WheelAccumulator,
    parse_config,
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
    assert parse_config([]) == GameConfig()
    assert parse_config(
        ["--width", "800", "--height", "600", "--text-cps", "12", "--choice-seconds", "3"]
    ) == GameConfig(800, 600, 12.0, 3.0)


def test_invalid_command_line_is_rejected() -> None:
    with pytest.raises(SystemExit):
        parse_config(["--width", "10"])


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
