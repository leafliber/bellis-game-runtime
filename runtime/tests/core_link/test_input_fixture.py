"""只检查 GS-02 夹具 ABI 与失败处理；SendInput 全部替换，不启动窗口或注入输入。"""

# 测试刻意核对手工夹具内部的 C ABI，不把这些结构变成公共接口。
# pyright: reportPrivateUsage=false

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows INPUT ABI")

if sys.platform == "win32":
    import ctypes
    from typing import Any

    from . import test_input_window as fixture

    class FakeUser32:
        def __init__(self, counts: list[int]) -> None:
            self.counts = iter(counts)
            self.calls: list[list[tuple[int, int, int | None]]] = []

        def SendInput(self, count: int, events: Any, size: int) -> int:  # noqa: N802
            assert size == ctypes.sizeof(fixture._INPUT)
            self.calls.append(
                [
                    (events[i].ki.wScan, events[i].ki.dwFlags, events[i].ki.dwExtraInfo)
                    for i in range(count)
                ]
            )
            return next(self.counts)

    def test_windows_input_abi() -> None:
        x64 = ctypes.sizeof(ctypes.c_void_p) == 8
        assert ctypes.sizeof(fixture._INPUT) == (40 if x64 else 28)
        assert fixture._INPUT.u.offset == (8 if x64 else 4)
        assert ctypes.sizeof(fixture._MOUSEINPUT) == (32 if x64 else 24)
        assert ctypes.sizeof(fixture._KEYBDINPUT) == (24 if x64 else 16)
        assert ctypes.sizeof(fixture._HARDWAREINPUT) == 8

    def test_injection_order_and_unmarked_events(monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeUser32([6])
        monkeypatch.setattr(fixture, "_user32", fake)
        fixture.third_party_keys(0x1D, 0x2A, 0x58)
        assert fake.calls == [
            [
                (0x1D, 8, None),
                (0x2A, 8, None),
                (0x58, 8, None),
                (0x58, 10, None),
                (0x2A, 10, None),
                (0x1D, 10, None),
            ]
        ]

    @pytest.mark.parametrize("accepted", [0, 1, 3, 4, 5])
    def test_short_write_fails_and_only_releases_pending_keys(
        monkeypatch: pytest.MonkeyPatch, accepted: int
    ) -> None:
        remaining = {0: [], 1: [0x1D], 3: [0x58, 0x2A, 0x1D], 4: [0x2A, 0x1D], 5: [0x1D]}[accepted]
        fake = FakeUser32([accepted, len(remaining)])
        monkeypatch.setattr(fixture, "_user32", fake)
        with pytest.raises(OSError, match=f"accepted {accepted}/6 events"):
            fixture.third_party_keys(0x1D, 0x2A, 0x58)
        if remaining:
            assert fake.calls[1] == [(scan, 10, None) for scan in remaining]
        assert len(fake.calls) == (2 if remaining else 1)

    def test_failed_cleanup_remains_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeUser32([2, 0])
        monkeypatch.setattr(fixture, "_user32", fake)
        with pytest.raises(OSError, match="cleanup accepted 0/2 key-up events"):
            fixture.third_party_keys(0x1D, 0x2A, 0x58)
        assert fake.calls[1] == [(0x2A, 10, None), (0x1D, 10, None)]
