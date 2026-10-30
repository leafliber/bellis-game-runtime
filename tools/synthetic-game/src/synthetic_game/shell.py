"""tkinter 窗口外壳：把状态机的帧显示成真实窗口，把真实的鼠标键盘事件转成输入交给状态机。

入口是 console script `synthetic-game`（`uv run synthetic-game`）。在 Windows 上建窗之前先把进程
声明为按显示器感知 DPI v2，客户区就是物理像素，WGC 采到的尺寸等于缓冲尺寸。
tkinter 只在建窗时导入，没有 tk 的环境也能导入本模块并测试其中与 tk 无关的转换。

外壳按固定频率重画窗口（缺省 30 Hz），画面内容没变也重画：WGC 只在窗口内容变化时出帧，
静止的画面不重画就没有新帧，帧龄会一直增长。真实游戏一般也每帧都在绘制。
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from PIL import Image

from synthetic_game.clock import MonotonicClock
from synthetic_game.game import GameConfig, SyntheticGame
from synthetic_game.inputs import Click, Key, MouseButton, Press, Scroll

if TYPE_CHECKING:
    import tkinter as tk

    from PIL import ImageTk

WINDOW_TITLE = "Bellis Synthetic Game"
"""窗口标题，供窗口识别配置使用。"""

WINDOW_CLASS = "BellisSyntheticGame"
"""Tk 的类名（X11 的 WM_CLASS）。Windows 上的 Win32 窗口类由 Tk 固定为 `TkTopLevel`，不受它影响。"""

DEFAULT_REDRAW_HZ = 30.0
"""缺省的重画频率。"""

MAX_REDRAW_HZ = 1000.0
"""重画频率的上限：Tk 的定时器以毫秒计。"""

WHEEL_DELTA = 120
"""Windows 一格滚轮的增量。"""

KEYSYMS: dict[str, Key] = {
    "Return": Key.ENTER,
    "KP_Enter": Key.ENTER,
    "space": Key.SPACE,
    "Escape": Key.ESCAPE,
    "Up": Key.UP,
    "KP_Up": Key.UP,
    "Down": Key.DOWN,
    "KP_Down": Key.DOWN,
}

TK_BUTTONS: dict[int, MouseButton] = {
    1: MouseButton.LEFT,
    2: MouseButton.MIDDLE,
    3: MouseButton.RIGHT,
}
"""Tk 的鼠标键编号（Windows 与 X11）。"""

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4


def declare_per_monitor_dpi_awareness() -> None:
    """在 Windows 上把进程声明为按显示器感知 DPI v2；已经是 v2 也算成功，其它平台什么都不做。"""
    if sys.platform != "win32":
        return
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
    user32.GetDpiAwarenessContextForProcess.argtypes = [ctypes.c_void_p]
    user32.GetDpiAwarenessContextForProcess.restype = ctypes.c_void_p
    user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.AreDpiAwarenessContextsEqual.restype = ctypes.c_int
    v2 = ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    if user32.SetProcessDpiAwarenessContext(v2):
        return
    # 进程的 DPI 感知只能设定一次：已经是 v2（例如由清单声明）时视为成功。
    error = ctypes.get_last_error()
    if user32.AreDpiAwarenessContextsEqual(user32.GetDpiAwarenessContextForProcess(None), v2):
        return
    raise OSError(error, "无法把进程声明为按显示器感知 DPI v2")


class WheelAccumulator:
    """把滚轮增量累积成整格；高精度滚轮的零碎增量攒够一格才产生一次滚动。"""

    def __init__(self) -> None:
        self._remainder = 0

    def add(self, delta: int) -> int:
        self._remainder += delta
        notches = int(self._remainder / WHEEL_DELTA)
        self._remainder -= notches * WHEEL_DELTA
        return notches


def check_redraw_hz(hz: float) -> float:
    """重画频率须在 (0, `MAX_REDRAW_HZ`] 之内。"""
    if not 0 < hz <= MAX_REDRAW_HZ:
        raise ValueError(f"重画频率须大于 0、不超过 {MAX_REDRAW_HZ:g} Hz：{hz}")
    return hz


class RedrawSchedule:
    """按固定频率排定重画的时刻。

    下一次重画排在上一次排定的时刻加一个周期，不随每次重画的耗时累积漂移；落后超过一个周期时
    不追赶，从现在重新排。
    """

    def __init__(self, hz: float, start: float) -> None:
        self._period = 1.0 / check_redraw_hz(hz)
        self._next = start

    def delay_ms(self, now: float) -> int:
        """刚重画完一次（现在是 `now` 秒）：到下一次重画的毫秒数，至少 1。"""
        self._next += self._period
        if self._next < now:
            self._next = now
        return max(1, round((self._next - now) * 1000))


class Shell:
    """把一个 `SyntheticGame` 显示在一个 Tk 顶层窗口里。

    客户区里只有一个显示帧的标签，它的原点与客户区左上角重合、大小等于分辨率。
    `run` 之后按 `redraw_hz` 重画，见模块说明。
    """

    def __init__(
        self,
        root: tk.Tk | tk.Toplevel,
        game: SyntheticGame,
        redraw_hz: float = DEFAULT_REDRAW_HZ,
    ) -> None:
        import tkinter as tk

        from PIL import ImageTk

        self.root = root
        self.game = game
        self.redraw_hz = check_redraw_hz(redraw_hz)
        width, height = game.config.width, game.config.height
        root.title(WINDOW_TITLE)
        root.geometry(f"{width}x{height}")
        root.resizable(False, False)
        self.photo: ImageTk.PhotoImage = ImageTk.PhotoImage("RGBA", (width, height))
        # 无边框、无内边距的标签正好是图像大小，放在客户区原点。
        self.screen = tk.Label(
            root, image=self.photo, borderwidth=0, highlightthickness=0, padx=0, pady=0
        )
        self.screen.place(x=0, y=0, width=width, height=height)
        self._image = Image.new("RGBA", (width, height))
        self._shown = 0
        self._wheel = WheelAccumulator()
        # 绑定在根窗口上：标签的事件经绑定标签也会送到这里，Windows 把滚轮送给焦点窗口也能收到。
        for number in TK_BUTTONS:
            root.bind(f"<ButtonPress-{number}>", self._on_button)
        root.bind("<MouseWheel>", self._on_wheel)
        # X11 上的 Tk 8.6 用 4、5 号键表示滚轮。
        root.bind("<ButtonPress-4>", lambda event: self._scroll(event, 1))
        root.bind("<ButtonPress-5>", lambda event: self._scroll(event, -1))
        root.bind("<KeyPress>", self._on_key)
        self.refresh()

    def run(self) -> None:
        schedule = RedrawSchedule(self.redraw_hz, time.monotonic())

        def tick() -> None:
            self.redraw()
            self.root.after(schedule.delay_ms(time.monotonic()), tick)

        tick()
        self.root.mainloop()

    def refresh(self) -> bool:
        """取当前帧；帧号变了就更新窗口内容并返回真。输入处理完立即调用，不等下一次重画。"""
        frame = self.game.frame()
        if frame.number == self._shown:
            return False
        self._image = Image.frombuffer(
            "RGBA", (frame.width, frame.height), frame.data, "raw", "BGRA", frame.stride, 1
        )
        self.photo.paste(self._image)
        self._shown = frame.number
        return True

    def redraw(self) -> None:
        """重画一次：取当前帧，内容没变也把它重新贴进窗口，让窗口重绘。"""
        if not self.refresh():
            self.photo.paste(self._image)

    def press_keysym(self, keysym: str) -> None:
        key = KEYSYMS.get(keysym)
        if key is not None:
            self.game.handle(Press(key))
            self.refresh()

    def _client_point(self, event: tk.Event[tk.Misc]) -> tuple[int, int]:
        # 用屏幕坐标减去标签原点，不依赖事件送达的是哪个窗口。
        # 进程按显示器感知 DPI v2 时，两者都是物理像素。
        return event.x_root - self.screen.winfo_rootx(), event.y_root - self.screen.winfo_rooty()

    def _on_button(self, event: tk.Event[tk.Misc]) -> None:
        x, y = self._client_point(event)
        self.game.handle(Click(x, y, TK_BUTTONS[int(event.num)]))
        self.refresh()

    def _on_wheel(self, event: tk.Event[tk.Misc]) -> None:
        notches = self._wheel.add(event.delta)
        if notches:
            self._scroll(event, notches)

    def _scroll(self, event: tk.Event[tk.Misc], notches: int) -> None:
        x, y = self._client_point(event)
        self.game.handle(Scroll(x, y, notches))
        self.refresh()

    def _on_key(self, event: tk.Event[tk.Misc]) -> None:
        self.press_keysym(event.keysym)


@dataclass(frozen=True)
class ShellOptions:
    """命令行给出的游戏配置与外壳选项。"""

    game: GameConfig = field(default_factory=GameConfig)
    redraw_hz: float = DEFAULT_REDRAW_HZ


def parse_args(argv: Sequence[str] | None) -> ShellOptions:
    defaults = GameConfig()
    parser = argparse.ArgumentParser(
        prog="synthetic-game", description="Run the synthetic game fixture in a window."
    )
    parser.add_argument("--width", type=int, default=defaults.width)
    parser.add_argument("--height", type=int, default=defaults.height)
    parser.add_argument(
        "--text-cps", type=float, default=defaults.text_cps, help="characters per second"
    )
    parser.add_argument(
        "--choice-seconds",
        type=float,
        default=defaults.choice_seconds,
        help="duration of the timed choice",
    )
    parser.add_argument(
        "--redraw-hz",
        type=float,
        default=DEFAULT_REDRAW_HZ,
        help="how often the window is redrawn, even when the picture has not changed",
    )
    args = parser.parse_args(argv)
    try:
        game = GameConfig(args.width, args.height, args.text_cps, args.choice_seconds)
        return ShellOptions(game, check_redraw_hz(args.redraw_hz))
    except ValueError as error:
        parser.error(str(error))


def main(argv: Sequence[str] | None = None) -> int:
    import tkinter as tk

    options = parse_args(argv)
    declare_per_monitor_dpi_awareness()
    root = tk.Tk(className=WINDOW_CLASS)
    shell = Shell(root, SyntheticGame(options.game, MonotonicClock()), options.redraw_hz)
    shell.run()
    return 0
