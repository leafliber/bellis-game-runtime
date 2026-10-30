"""真机手动测试：合成游戏的窗口经 WGC 进入帧环（G0 出口“合成游戏：真机上其窗口经 WGC 进入帧环”）。

只在 Windows 的交互桌面上运行，标 `manual`，不在 `uv run check` 中；运行命令见 AGENTS.md 的常用
命令，加 `-s` 看打印的实测值。测试先让 `CoreLink` 拉起实时核、以窗口识别规则设定采集目标，再启动
合成游戏的 tkinter 外壳（`synthetic-game`），依次核对：

- 窗口出现之前实时核在 searching，出现之后进入 capturing；
- 帧环里的帧与外壳显示的缓冲逐像素一致，只有可解释的差异（Windows 11 给有边框窗口画的圆角）。
  外壳停在静止的标题菜单，缓冲由同一配置在本进程渲染；
- 帧率、帧龄与读帧时没见到的帧号（打印出来）；
- 移动窗口后变换版本加一；
- 最小化后没有新帧、仍是 capturing，还原后继续出帧；
- 关闭窗口后回到 searching。
"""

import sys

import pytest

pytestmark = pytest.mark.manual

if sys.platform == "win32":
    import asyncio
    import contextlib
    import ctypes
    import math
    import statistics
    import subprocess
    import time
    from collections.abc import Callable
    from ctypes import wintypes
    from pathlib import Path
    from typing import Any

    from core_protocol.models import CaptureState, CoreHealthParams
    from game_runtime.core_link import CaptureConfig, CoreLink, CoreLinkOptions, WindowRule
    from game_runtime.frame_ring import FrameRing, RingFrame, frame_age_ns
    from synthetic_game import GameConfig, SyntheticGame
    from synthetic_game.shell import WINDOW_TITLE

    WAIT = 10.0
    RULE = WindowRule(process_name="python.exe", window_class="TkTopLevel", title=WINDOW_TITLE)
    """合成游戏外壳的识别规则，见 `docs/modules/offline-tools.md` 的“窗口外壳”。"""
    CORNER_RADIUS = 8
    """Windows 11 有边框窗口的圆角半径（逻辑像素），见 `docs/modules/realtime-core.md` 的“边缘”。"""

    _user32: Any = ctypes.WinDLL("user32", use_last_error=True)
    _user32.FindWindowW.restype = wintypes.HWND
    _user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    _user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    _user32.SetWindowPos.argtypes = (
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    )
    _user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    _user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _user32.IsWindow.argtypes = (wintypes.HWND,)
    _user32.GetDpiForWindow.argtypes = (wintypes.HWND,)
    _user32.GetDpiForWindow.restype = wintypes.UINT
    _user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))

    class Window:
        """外壳窗口：找到它、移动、最小化、还原与关闭。只作用于测试自己启动的窗口。"""

        SW_SHOWNOACTIVATE = 4
        SW_SHOWMINNOACTIVE = 7
        SWP_FLAGS = 0x0001 | 0x0004 | 0x0010  # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
        WM_CLOSE = 0x0010

        def __init__(self, hwnd: int) -> None:
            self.hwnd = hwnd

        @staticmethod
        def find() -> "Window | None":
            hwnd = _user32.FindWindowW(RULE.window_class, RULE.title)
            return Window(hwnd) if hwnd else None

        def process_id(self) -> int:
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(pid))
            return int(pid.value)

        def dpi(self) -> int:
            return int(_user32.GetDpiForWindow(self.hwnd))

        def move_by(self, dx: int, dy: int) -> None:
            rect = wintypes.RECT()
            assert _user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
            x, y = int(rect.left) + dx, int(rect.top) + dy
            assert _user32.SetWindowPos(self.hwnd, None, x, y, 0, 0, self.SWP_FLAGS)

        def minimize(self) -> None:
            _user32.ShowWindow(self.hwnd, self.SW_SHOWMINNOACTIVE)

        def restore(self) -> None:
            _user32.ShowWindow(self.hwnd, self.SW_SHOWNOACTIVATE)

        def close(self) -> None:
            _user32.PostMessageW(self.hwnd, self.WM_CLOSE, 0, 0)

        def exists(self) -> bool:
            return bool(_user32.IsWindow(self.hwnd))

    async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
        deadline = time.monotonic() + timeout
        while (value := probe()) is None:
            assert time.monotonic() < deadline, "等待的条件没有成立"
            await asyncio.sleep(0.01)
        return value

    def compare(frame: RingFrame, expected: bytes, width: int, height: int, dpi: int) -> list[str]:
        """逐像素比较帧环里的帧与缓冲；返回无法解释的差异，打印全部差异的概况。"""
        assert (frame.width, frame.height, frame.row_stride) == (width, height, width * 4)
        radius = math.ceil(CORNER_RADIUS * dpi / 96) + 1
        corner_alphas: list[int] = []
        unexplained: list[str] = []
        row_bytes = width * 4
        for y in range(height):
            start = y * row_bytes
            got_row = frame.pixels[start : start + row_bytes]
            want_row = expected[start : start + row_bytes]
            if got_row == want_row:
                continue
            for x in range(width):
                got = got_row[x * 4 : x * 4 + 4]
                want = want_row[x * 4 : x * 4 + 4]
                if got == want:
                    continue
                if y >= height - radius and (x < radius or x >= width - radius):
                    corner_alphas.append(got[3])
                else:
                    unexplained.append(f"({x}, {y}): got {got.hex()}, want {want.hex()}")
        alphas = f"alpha {min(corner_alphas)}..{max(corner_alphas)}" if corner_alphas else "none"
        print(
            f"pixels: {width}x{height}; {len(corner_alphas)} differ inside the bottom corners "
            f"(radius {radius} px; {alphas}; buffer alpha {set(expected[3::4])}); "
            f"{len(unexplained)} differ elsewhere"
        )
        return unexplained[:20]

    async def measure(ring: FrameRing, seconds: float) -> None:
        """在 `seconds` 秒内尽快地读帧环，打印帧率、读帧时没见到的帧号与帧龄。"""
        first = ring.latest_frame_number()
        seen: set[int] = set()
        ages: list[int] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            frame = ring.read_latest()
            if frame is not None and frame.frame_number not in seen:
                seen.add(frame.frame_number)
                ages.append(frame_age_ns(frame.capture_ns))
            await asyncio.sleep(0.002)
        last = ring.latest_frame_number()
        assert last > first, "没有新帧"
        fps = (last - first) / seconds
        missed = sum(1 for n in range(first + 1, last + 1) if n not in seen)
        print(
            f"frames: {last - first} in {seconds:g} s ({fps:.1f} fps); read {len(seen)} distinct, "
            f"{missed} numbers not seen by the reader; age at first read: median "
            f"{statistics.median(ages) / 1e6:.1f} ms, max {max(ages) / 1e6:.1f} ms"
        )
        assert fps > 10, fps

    def test_synthetic_game_window_reaches_the_frame_ring_through_wgc(core_exe: Path) -> None:
        config = GameConfig()
        expected = SyntheticGame(config).frame()
        assert expected.stride == config.width * 4
        launcher = Path(sys.executable).parent / "synthetic-game.exe"
        assert launcher.is_file(), launcher
        assert Window.find() is None, "已经有一个合成游戏窗口，先关掉它"

        async def main() -> None:
            healths: list[CoreHealthParams] = []
            capture = CaptureConfig(target=RULE)
            options = CoreLinkOptions(core_exe, health_interval=0.1, capture=capture)
            link = CoreLink(options, on_health=healths.append)

            def state_is(state: CaptureState) -> bool | None:
                return True if healths and healths[-1].capture is state else None

            await link.start()
            shell: subprocess.Popen[bytes] | None = None
            window: Window | None = None
            try:
                ring = link.frames
                assert ring is not None
                # 窗口还没出现：定期重新识别，没有帧。
                await until(lambda: state_is(CaptureState.SEARCHING))
                assert ring.latest_frame_number() == 0

                shell = subprocess.Popen([str(launcher)])
                window = await until(Window.find)
                await until(lambda: state_is(CaptureState.CAPTURING))
                dpi = window.dpi()
                print(f"\nwindow: pid {window.process_id()}, dpi {dpi}")
                await asyncio.sleep(1.0)  # 等窗口画完

                frame = await until(ring.read_latest)
                assert compare(frame, expected.data, config.width, config.height, dpi) == []

                await measure(ring, seconds=3.0)

                # 移动窗口：客户区原点变了，变换版本加一。
                before = await until(ring.read_latest)
                window.move_by(40, 30)

                def moved() -> RingFrame | None:
                    frame = ring.read_latest()
                    changed = (
                        frame is not None and frame.transform_version != before.transform_version
                    )
                    return frame if changed else None

                after = await until(moved)
                print(f"transform version: {before.transform_version} -> {after.transform_version}")
                assert after.transform_version == before.transform_version + 1
                assert after.pixels == before.pixels

                # 最小化：可恢复，采集源保留，没有新帧；还原后继续出帧。
                window.minimize()
                await asyncio.sleep(0.5)
                minimized = ring.latest_frame_number()
                await asyncio.sleep(1.0)
                assert ring.latest_frame_number() == minimized
                assert state_is(CaptureState.CAPTURING)
                print(f"minimized: no new frame for 1 s after frame {minimized}, still capturing")
                window.restore()
                await until(lambda: True if ring.latest_frame_number() > minimized else None)

                # 关闭窗口：采集源失效，回到 searching。
                window.close()
                await asyncio.wait_for(asyncio.to_thread(shell.wait), WAIT)
                await until(lambda: state_is(CaptureState.SEARCHING))
                print(f"closed: searching again; the shell exited with {shell.returncode}")
            finally:
                await link.stop()
                if window is not None and window.exists():
                    window.close()
                if shell is not None:
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        shell.wait(5)
                    if shell.poll() is None:
                        shell.kill()
                        shell.wait(5)
            assert Window.find() is None

        asyncio.run(main())
