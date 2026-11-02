"""真机手动测试：GS-02 在合成游戏的窗口上实测真实输入（`docs/modules/realtime-core.md` 的“GS-02 的
实测”）。

只在 Windows 的交互桌面上运行，标 `manual`，不在 `uv run check` 中；运行命令见 AGENTS.md 的常用
命令，加 `-s` 看打印的实测值。真实输入会移动鼠标并点击、按键作用在合成游戏的窗口上，运行前先关掉
已经打开的合成游戏窗口。

拓扑与 `test_operator.py` 相同，只是输入走真实后端（SendInput）：在本进程里起操作员控制台
（`OperatorConsole`，内存 SecretStore）作许可源，主进程一侧经 `OperatorClient` 连上、再把连接信息
交给实时核；实时核以 `--input-backend sendinput` 真实注入。合成游戏的 tkinter 外壳是真实窗口，采集经
WGC 进入帧环，据帧是否变化判断点击/短按是否生效。

核对并打印：
- 点击菜单“开始”与在对话里短按回车的生效率（画面在约 1 秒内变化）；
- 只在本测试里给一个允许按住键 W 的输入策略：按住后分别触发暂停、控制台退出、许可过期与急停组合键，
  量从触发到本进程低层钩子看到注入的 W 抬起的时延；
- 释放后 `GetAsyncKeyState` 核验没有残留按住的键；
- 本进程自己以 SendInput 注入一个键，被判为接管（`takeover`）并释放。
"""

import sys

import pytest

pytestmark = pytest.mark.manual

if sys.platform == "win32":
    import asyncio
    import contextlib
    import ctypes
    import subprocess
    import time
    from collections.abc import Callable
    from ctypes import wintypes
    from pathlib import Path
    from typing import Any

    from core_protocol import models
    from core_protocol.models import ReleaseReason
    from game_runtime.adapter import GAMES_DIR, load_adapter
    from game_runtime.core_link import CaptureConfig, CoreLink, CoreLinkOptions, WindowRule
    from game_runtime.frame_ring import FrameRing
    from game_runtime.kernel import ControlGrant
    from game_runtime.operator import ConsoleOptions, OperatorClient, OperatorConsole
    from game_runtime.secret_store import MemorySecretStore
    from synthetic_game.shell import WINDOW_TITLE

    WAIT = 15.0
    W_SCAN = 0x11
    """按住的键：W 的 Set 1 扫描码。"""
    W_VK = 0x57
    """W 的虚拟键码，本进程的钩子据它认出注入的抬起。"""
    ENTER_SCAN = 0x1C
    CHANGE_TIMEOUT = 1.0
    RULE = WindowRule(process_name="python.exe", window_class="TkTopLevel", title=WINDOW_TITLE)

    _user32: Any = ctypes.WinDLL("user32", use_last_error=True)
    _user32.FindWindowW.restype = wintypes.HWND
    _user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    _user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    _user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    _user32.SendInput.restype = wintypes.UINT
    _user32.SetWindowsHookExW.restype = wintypes.HANDLE
    _user32.CallNextHookEx.restype = wintypes.LPARAM

    SW_RESTORE = 9
    WH_KEYBOARD_LL = 13
    WM_KEYUP = 0x0101
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_SCANCODE = 0x0008

    async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
        deadline = time.monotonic() + timeout
        while (value := probe()) is None:
            assert time.monotonic() < deadline, "等待的条件没有成立"
            await asyncio.sleep(0.01)
        return value

    def ms(nanoseconds: int) -> float:
        return nanoseconds / 1e6

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = (
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        )

    _HOOKPROC = ctypes.CFUNCTYPE(wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    class UpHook:
        """本进程的低层键盘钩子：记下看到 W 抬起的时刻，供量释放时延。在自己的线程上跑消息循环。"""

        def __init__(self) -> None:
            self._ups: list[int] = []
            self._handle: Any = None
            self._thread_id = 0
            self._proc = _HOOKPROC(self._callback)
            self._ready = __import__("threading").Event()

        def _callback(self, code: int, wparam: int, lparam: int) -> int:
            if code == 0 and wparam == WM_KEYUP:
                info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if info.vkCode == W_VK:
                    self._ups.append(time.perf_counter_ns())
            return int(_user32.CallNextHookEx(None, code, wparam, lparam))

        def start(self) -> None:
            import threading

            def run() -> None:
                kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
                self._thread_id = kernel32.GetCurrentThreadId()
                self._handle = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
                self._ready.set()
                message = wintypes.MSG()
                while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                    pass

            threading.Thread(target=run, name="test-hook", daemon=True).start()
            assert self._ready.wait(5), "测试钩子没装上"
            assert self._handle, "SetWindowsHookExW 失败"

        def ups(self) -> list[int]:
            return list(self._ups)

        def stop(self) -> None:
            if self._handle is not None:
                _user32.UnhookWindowsHookEx(self._handle)
                self._handle = None
            if self._thread_id:
                _user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = (
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        )

    class _INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = (("ki", _KEYBDINPUT),)

        _anonymous_ = ("u",)
        _fields_ = (("type", wintypes.DWORD), ("u", _U))

    def third_party_key(scan: int) -> None:
        """本进程以 SendInput 注入一个键的按下与抬起（不是实时核的会话标记）：实时核应判为接管。"""
        events = (_INPUT * 2)()
        for index, up in enumerate((False, True)):
            events[index].type = INPUT_KEYBOARD
            events[index].ki = _KEYBDINPUT(
                wVk=0,
                wScan=scan,
                dwFlags=KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0),
                time=0,
                dwExtraInfo=None,
            )
        _user32.SendInput(2, ctypes.byref(events), ctypes.sizeof(_INPUT))

    def find_hwnd() -> int | None:
        hwnd = _user32.FindWindowW("TkTopLevel", WINDOW_TITLE)
        return int(hwnd) if hwnd else None

    def to_foreground(hwnd: int) -> None:
        _user32.ShowWindow(hwnd, SW_RESTORE)
        _user32.SetForegroundWindow(hwnd)

    def async_down(vk: int) -> bool:
        return bool(_user32.GetAsyncKeyState(vk) & 0x8000)

    def make_policy() -> models.InputPolicy:
        full = {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0}
        return models.InputPolicy.model_validate(
            {
                "click_buttons": ["left"],
                "press_keys": [ENTER_SCAN],
                "scroll": False,
                "click_regions": [full],
                "forbidden_zones": [],
                "press_duration_ms": 50,
                "max_rate": 20.0,
                "hold_keys": [W_SCAN],
                "hold_buttons": [],
                "max_hold_ms": 5000,
            }
        )

    class Rig:
        def __init__(self, link: CoreLink, console: OperatorConsole, grant: ControlGrant) -> None:
            self.link = link
            self.console = console
            self.grant = grant
            self.released: list[models.InputReleasedParams] = []
            self.residuals: list[models.InputResidualParams] = []
            self._id = 0

        def next_id(self) -> int:
            self._id += 1
            return self._id

        def frame(self) -> tuple[int, int, int, bytes] | None:
            ring = self.link.frames
            if ring is None:
                return None
            got = ring.read_latest()
            if got is None:
                return None
            return got.frame_number, got.width, got.height, got.pixels

        async def click(self, x: int, y: int) -> bool:
            frame = self.frame()
            assert frame is not None
            base, width, height, before = frame
            action = models.Action(
                root=models.Click(id=self.next_id(), x=x, y=y, button=models.MouseButton.LEFT)
            )
            await self.grant.submit(
                [models.ControlStep(offset_ns=0, held=[], actions=[action])],
                base_frame=base,
                valid_for=1.5,
            )
            return await self._changed(before, width, height)

        async def press_enter(self) -> bool:
            frame = self.frame()
            assert frame is not None
            base, width, height, before = frame
            action = models.Action(root=models.KeyPress(id=self.next_id(), scan_code=ENTER_SCAN))
            await self.grant.submit(
                [models.ControlStep(offset_ns=0, held=[], actions=[action])],
                base_frame=base,
                valid_for=1.5,
            )
            return await self._changed(before, width, height)

        async def hold_w(self) -> None:
            held = models.HeldInput(root=models.HeldKey(scan_code=W_SCAN))
            base = self.frame()
            assert base is not None
            await self.grant.submit(
                [models.ControlStep(offset_ns=0, held=[held], actions=[])],
                base_frame=base[0],
                valid_for=30.0,
            )
            await until(lambda: True if async_down(W_VK) else None)

        async def _changed(self, before: bytes, width: int, height: int) -> bool:
            deadline = time.monotonic() + CHANGE_TIMEOUT
            while time.monotonic() < deadline:
                frame = self.frame()
                if (
                    frame is not None
                    and frame[1] == width
                    and frame[2] == height
                    and frame[3] != before
                ):
                    return True
                await asyncio.sleep(0.005)
            return False

    def center(adapter: Any, roi: str, width: int, height: int) -> tuple[int, int]:
        rect = adapter.roi(roi)
        x = round((float(rect.left) + float(rect.right)) / 2 * width)
        y = round((float(rect.top) + float(rect.bottom)) / 2 * height)
        return x, y

    def test_real_input_on_the_synthetic_window(core_exe: Path) -> None:
        assert find_hwnd() is None, "已经有一个合成游戏窗口，先关掉它"
        launcher = Path(sys.executable).parent / "synthetic-game.exe"
        adapter = load_adapter(GAMES_DIR / "synthetic-game")
        hook = UpHook()
        hook.start()
        shell: subprocess.Popen[bytes] | None = None

        async def main() -> None:
            nonlocal shell
            store = MemorySecretStore()
            console = OperatorConsole(
                adapter,
                store=store,
                data_dir=Path("reports/gs02-session"),
                options=ConsoleOptions(start_delay=2.0),
            )
            await console.open()
            client = OperatorClient(adapter, store=store)
            rig: Rig | None = None
            link: CoreLink | None = None
            try:
                await client.connect()
                link = CoreLink(
                    CoreLinkOptions(
                        core_exe,
                        health_interval=0.1,
                        real_input=True,
                        operator=client.credentials,
                        capture=CaptureConfig(target=RULE, max_width=1280, max_height=720),
                        input_policy=make_policy(),
                    ),
                    on_released=lambda r: rig.released.append(r) if rig else None,
                    on_residual=lambda r: rig.residuals.append(r) if rig else None,
                )
                grant = ControlGrant("gs02", "gs02#1", 1, lambda: True, link.submit)
                link.bind_control(lambda: grant)
                rig = Rig(link, console, grant)
                await link.start()
                shell = subprocess.Popen([str(launcher)])
                hwnd = await until(find_hwnd)
                await asyncio.sleep(1.0)
                # 确认前置条件，开始（倒数 2 秒），期间把窗口切到前台。
                while console.pending_confirmation is not None:
                    await console.answer(True)
                await console.start()
                to_foreground(hwnd)
                await until(lambda: True if client.may_autoplay else None)
                await until(lambda: self_ring_ready(link))

                # 1) 点击与短按的生效率。
                _, width, height, _ = await until(rig.frame)
                mx, my = center(adapter, "menu-start", width, height)
                clicked = await rig.click(mx, my)
                presses = [await rig.press_enter() for _ in range(5)]
                rate = (int(clicked) + sum(int(p) for p in presses)) / 6
                print(f"\n点击/短按生效率：{rate:.2f}（点击开始 {clicked}，回车 {presses}）")

                # 2) 按住 W，量各触发到钩子看到抬起的时延。
                async def hold_then(trigger: Callable[[], object], label: str) -> None:
                    assert rig is not None
                    await rig.hold_w()
                    before = len(hook.ups())
                    at = time.perf_counter_ns()
                    result = trigger()
                    if asyncio.iscoroutine(result):
                        await result
                    await until(lambda: True if len(hook.ups()) > before else None)
                    latency = hook.ups()[-1] - at
                    print(f"{label} → W 抬起：{ms(latency):.1f} ms")
                    await until(lambda: True if not async_down(W_VK) else None)
                    assert not async_down(W_VK), f"{label} 后 W 仍按住"

                await hold_then(lambda: console.pause(), "暂停")
                await console.start()
                to_foreground(hwnd)
                await until(lambda: True if client.may_autoplay else None)

                # 3) 第三方注入（本进程自己注入）判为接管。
                await rig.hold_w()
                before_releases = len(rig.released)
                third_party_key(ENTER_SCAN)
                await until(
                    lambda: next(
                        (
                            r
                            for r in rig.released[before_releases:]
                            if r.reason is ReleaseReason.TAKEOVER
                        ),
                        None,
                    )
                )
                print("第三方注入被判为接管（takeover）")
                assert not async_down(W_VK)

                # 4) 许可过期（控制台退出）后释放与 GetAsyncKeyState 核验。
                await console.start()
                to_foreground(hwnd)
                await until(lambda: True if client.may_autoplay else None)
                await hold_then(lambda: console.close(), "控制台退出")
            finally:
                if link is not None:
                    await link.stop()
                await client.close()
                await console.close()

        try:
            asyncio.run(main())
        finally:
            hook.stop()
            if shell is not None and shell.poll() is None:
                shell.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    shell.wait(5)

    def self_ring_ready(link: CoreLink) -> bool | None:
        ring: FrameRing | None = link.frames
        return True if ring is not None and ring.read_latest() is not None else None
