"""真机手动测试：合成游戏 2560×1440 的窗口经 WGC 录制，量编码跟不跟得上、每分钟的体积与 CPU，
再在录制上回放，与实时运行逐条比对。

只在 Windows 的交互桌面上运行，标 `manual`，不在 `uv run check` 中；运行命令见 AGENTS.md 的
常用命令，加 `-s` 看打印的实测值。实时核用 release 版（`build_realtime_core`）。合成游戏的外壳以
`--autoplay` 自己逐句推进，不发任何真实输入。感知插件用本地数据目录里真实的模型（先
`uv run fetch-models`），模型以硬链接放进测试的临时本地数据目录，录制也写在那里。
录多久由环境变量 `RECORD_SECONDS` 给出，缺省 60 秒。先关掉已打开的合成游戏窗口。
"""

import sys

import pytest

pytestmark = pytest.mark.manual

if sys.platform == "win32":
    import asyncio
    import contextlib
    import ctypes
    import os
    import shutil
    import subprocess
    import time
    from ctypes import wintypes
    from pathlib import Path
    from typing import Any

    from game_runtime.adapter import GAMES_DIR, load_adapter
    from game_runtime.artifacts import MODELS_DIR
    from game_runtime.core_link import CoreLinkOptions
    from game_runtime.local_data import local_data_dir
    from game_runtime.recording import (
        LiveOptions,
        LiveSession,
        Recording,
        adapter_sha256,
        audio_config,
        capture_config,
        directory_size,
        replay,
    )
    from game_runtime.recording.cli import build_realtime_core, tool_info
    from game_runtime.recording.codec import encode_delta, encode_png
    from game_runtime.scene import store_template
    from synthetic_game.anchors import anchor_templates
    from synthetic_game.shell import WINDOW_TITLE

    WIDTH, HEIGHT = 2560, 1440
    SECONDS = float(os.environ.get("RECORD_SECONDS", "60"))

    _user32: Any = ctypes.WinDLL("user32", use_last_error=True)
    _user32.FindWindowW.restype = wintypes.HWND
    _user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    _user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        *(ctypes.POINTER(wintypes.FILETIME),) * 4,
    )

    def find_window() -> int | None:
        return _user32.FindWindowW("TkTopLevel", WINDOW_TITLE) or None

    def process_cpu(pid: int) -> float:
        """进程至今用掉的 CPU 时间（秒，用户态加内核态）。"""
        handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        assert handle, ctypes.get_last_error()
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            assert _kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times))
            kernel, user = times[2], times[3]
            ticks = sum((t.dwHighDateTime << 32) | t.dwLowDateTime for t in (kernel, user))
            return ticks / 1e7
        finally:
            _kernel32.CloseHandle(handle)

    def link_models(target: Path) -> None:
        """把本地数据目录里的模型以硬链接放进测试的本地数据目录。"""
        source = local_data_dir() / MODELS_DIR
        assert source.is_dir(), f"{source} 没有模型，先 uv run fetch-models"
        shutil.copytree(source, target / MODELS_DIR, copy_function=os.link)

    def test_recording_the_synthetic_game_at_2560x1440_through_wgc(tmp_path: Path) -> None:
        assert find_window() is None, "已经有一个合成游戏窗口，先关掉它"
        core = build_realtime_core(release=True)
        data_dir = Path("\\\\?\\" + str(tmp_path.resolve())) / "data"
        for template in anchor_templates():
            store_template(data_dir, "synthetic-game", template.png)
        link_models(data_dir)
        adapter = load_adapter(GAMES_DIR / "synthetic-game")
        launcher = Path(sys.executable).parent / "synthetic-game.exe"
        options = LiveOptions(
            link=CoreLinkOptions(core),
            adapter=adapter,
            adapter_sha256=adapter_sha256(adapter.id),
            capture=capture_config(adapter, (WIDTH, HEIGHT)),
            audio=audio_config(adapter),
            data_dir=data_dir,
            tool=tool_info(),
            name="wgc-2560",
            perception=os.environ.get("RECORD_ONLY") != "1",
        )

        async def main() -> Path:
            shell = subprocess.Popen(
                [str(launcher), "--width", str(WIDTH), "--height", str(HEIGHT), "--autoplay", "1"]
            )
            session = LiveSession(options)
            try:
                deadline = time.monotonic() + 15
                while find_window() is None:
                    assert time.monotonic() < deadline, "合成游戏的窗口没有出现"
                    await asyncio.sleep(0.1)
                await asyncio.sleep(1.0)
                await session.start()
                cpu_main, cpu_core = time.process_time(), process_cpu(session.link.pid or 0)
                started = time.monotonic()
                first = session.status()
                peak_pending = 0
                while time.monotonic() - started < SECONDS:
                    await asyncio.sleep(1.0)
                    status = session.status()
                    peak_pending = max(peak_pending, status.pending)
                elapsed = time.monotonic() - started
                last = session.status()
                used_main = time.process_time() - cpu_main
                used_core = process_cpu(session.link.pid or 0) - cpu_core
                result = await session.stop("seconds")
            finally:
                window = find_window()
                if window is not None:
                    _user32.PostMessageW(window, 0x0010, 0, 0)  # WM_CLOSE
                with contextlib.suppress(subprocess.TimeoutExpired):
                    shell.wait(10)
                if shell.poll() is None:
                    shell.kill()
            meta = result.meta
            size = directory_size(result.path)
            minutes = elapsed / 60
            audio = meta.audio
            print(
                f"\nrecorded {elapsed:.1f} s at {last.capture} / {last.audio}, core {result.core}"
            )
            print(
                f"admitted {(last.admitted - first.admitted) / elapsed:.2f} fps; "
                f"ring {(last.ring_latest - first.ring_latest) / elapsed:.2f} fps; "
                f"frames {meta.frames.count} ({meta.frames.keyframes} keyframes); "
                f"peak in-flight encodes {peak_pending}; reader overflows {meta.reader_overflows}"
            )
            print(
                f"size {size / 1e6:.1f} MB = {size / 1e6 / minutes:.1f} MB/min; frames "
                f"{meta.frames.bytes / 1e6 / minutes:.1f} MB/min; audio "
                f"{(audio.bytes if audio else 0) / 1e6 / minutes:.1f} MB/min"
            )
            print(
                f"CPU: main process {used_main / elapsed * 100:.0f}% of one core, "
                f"realtime core {used_core / elapsed * 100:.0f}%"
            )
            assert meta.state == "complete"
            assert meta.reader_overflows == 0
            return result.path

        path = asyncio.run(main())
        recording = Recording.open(path)
        kinds = [entry.encoding for entry in recording.frames]
        print(f"encodings: { ({k: kinds.count(k) for k in ('png', 'delta', 'same')}) }")
        # 单线程编码的耗时：按录下的编码方式各取若干帧重新编码。
        timings: dict[str, list[float]] = {"png": [], "delta": []}
        previous = recording.frame(0).pixels
        decode_started = time.perf_counter()
        for index in range(len(recording.frames)):
            pixels = recording.frame(index).pixels
            kind = kinds[index]
            if kind in timings and len(timings[kind]) < 20:
                began = time.perf_counter()
                if kind == "png":
                    encode_png(pixels, WIDTH, HEIGHT)
                else:
                    encode_delta(pixels, previous)
                timings[kind].append(time.perf_counter() - began)
            previous = pixels
        decode_seconds = time.perf_counter() - decode_started
        for kind, values in timings.items():
            if values:
                print(f"encode {kind}: median {sorted(values)[len(values) // 2] * 1000:.0f} ms")
        print(f"sequential decode: {len(kinds) / decode_seconds:.1f} fps")

        async def again() -> None:
            began = time.perf_counter()
            result = await replay(recording, data_dir=data_dir)
            print(f"replay: {result.frames} frames in {time.perf_counter() - began:.1f} s")
            for name, comparison in result.comparisons.items():
                print(f"  {name}: identical={comparison.identical}, {comparison.same} lines")
                assert comparison.identical, (name, comparison)

        asyncio.run(again())
