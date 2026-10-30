"""tkinter 窗口外壳：把状态机的帧显示成真实窗口，把真实的鼠标键盘事件转成输入交给状态机。

入口是 console script `synthetic-game`（`uv run synthetic-game`）。在 Windows 上建窗之前先把进程
声明为按显示器感知 DPI v2，客户区就是物理像素，WGC 采到的尺寸等于缓冲尺寸。
tkinter 只在建窗时导入，没有 tk 的环境也能导入本模块并测试其中与 tk 无关的转换。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
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

FRAME_INTERVAL_MS = 16

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


class Shell:
    """把一个 `SyntheticGame` 显示在一个 Tk 顶层窗口里。

    客户区里只有一个显示帧的标签，它的原点与客户区左上角重合、大小等于分辨率。
    """

    def __init__(self, root: tk.Tk | tk.Toplevel, game: SyntheticGame) -> None:
        import tkinter as tk

        from PIL import ImageTk

        self.root = root
        self.game = game
        width, height = game.config.width, game.config.height
        root.title(WINDOW_TITLE)
        root.geometry(f"{width}x{height}")
        root.resizable(False, False)
        self._photo: ImageTk.PhotoImage = ImageTk.PhotoImage("RGBA", (width, height))
        # 无边框、无内边距的标签正好是图像大小，放在客户区原点。
        self.screen = tk.Label(
            root, image=self._photo, borderwidth=0, highlightthickness=0, padx=0, pady=0
        )
        self.screen.place(x=0, y=0, width=width, height=height)
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
        self._schedule()
        self.root.mainloop()

    def refresh(self) -> None:
        """取当前帧；帧号变了才更新窗口内容。"""
        frame = self.game.frame()
        if frame.number != self._shown:
            image = Image.frombuffer(
                "RGBA", (frame.width, frame.height), frame.data, "raw", "BGRA", frame.stride, 1
            )
            self._photo.paste(image)
            self._shown = frame.number

    def press_keysym(self, keysym: str) -> None:
        key = KEYSYMS.get(keysym)
        if key is not None:
            self.game.handle(Press(key))
            self.refresh()

    def _schedule(self) -> None:
        self.refresh()
        self.root.after(FRAME_INTERVAL_MS, self._schedule)

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


def parse_config(argv: Sequence[str] | None) -> GameConfig:
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
    args = parser.parse_args(argv)
    try:
        return GameConfig(args.width, args.height, args.text_cps, args.choice_seconds)
    except ValueError as error:
        parser.error(str(error))


def main(argv: Sequence[str] | None = None) -> int:
    import tkinter as tk

    config = parse_config(argv)
    declare_per_monitor_dpi_awareness()
    root = tk.Tk(className=WINDOW_CLASS)
    shell = Shell(root, SyntheticGame(config, MonotonicClock()))
    shell.run()
    return 0
