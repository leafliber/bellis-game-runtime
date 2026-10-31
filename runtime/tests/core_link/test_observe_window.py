"""真机手动测试：合成游戏的窗口外壳经 WGC 只读运行，用真实的 OCR 与配音检测模型，打印终端输出。

只在 Windows 的交互桌面上运行，标 `manual`，不在 `uv run check` 中；运行命令见 AGENTS.md 的常用
命令，加 `-s` 看终端输出。实时核用 release 版（`build_realtime_core`）。先开始只读运行、再拉起
外壳，标题菜单因此出现在采集里，语言核对在它上面执行；外壳以 `--autoplay` 自己逐句推进，不发任何
真实输入。模型以硬链接放进测试的临时本地数据目录（先 `uv run fetch-models`），锚点模板也写在那里。
运行多久由环境变量 `OBSERVE_SECONDS` 给出，缺省 30 秒；`OBSERVE_RECORD=1` 时同时录制到临时本地
数据目录。先关掉已打开的合成游戏窗口。
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
    from game_runtime.narrative import Completeness, compare_key
    from game_runtime.observe import LanguageEvent, LanguageResult
    from game_runtime.observe.cli import ObserveMonitor, observe
    from game_runtime.recording import (
        LiveOptions,
        adapter_sha256,
        audio_config,
        capture_config,
    )
    from game_runtime.recording.cli import build_realtime_core, tool_info
    from game_runtime.scene import store_template
    from synthetic_game.anchors import anchor_templates
    from synthetic_game.shell import WINDOW_TITLE
    from synthetic_game.story import NODES, Dialogue

    SECONDS = float(os.environ.get("OBSERVE_SECONDS", "30"))
    RECORD = os.environ.get("OBSERVE_RECORD") == "1"

    _user32: Any = ctypes.WinDLL("user32", use_last_error=True)
    _user32.FindWindowW.restype = wintypes.HWND
    _user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    _user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    def find_window() -> int | None:
        return _user32.FindWindowW("TkTopLevel", WINDOW_TITLE) or None

    def link_models(target: Path) -> None:
        """把本地数据目录里的模型以硬链接放进测试的本地数据目录。"""
        source = local_data_dir() / MODELS_DIR
        assert source.is_dir(), f"{source} 没有模型，先 uv run fetch-models"
        shutil.copytree(source, target / MODELS_DIR, copy_function=os.link)

    def story_lines() -> set[str]:
        return {
            compare_key(line.text)
            for node in NODES.values()
            if isinstance(node, Dialogue)
            for line in node.lines
        }

    def test_observing_the_synthetic_game_window_through_wgc(tmp_path: Path) -> None:
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
            capture=capture_config(adapter),
            audio=audio_config(adapter),
            data_dir=data_dir,
            tool=tool_info(),
            name="observe-wgc" if RECORD else None,
            record=RECORD,
        )
        lines: list[str] = []

        def write(line: str) -> None:
            lines.append(line)
            print(line, flush=True)

        monitor = ObserveMonitor(write)

        async def main() -> int:
            observing = asyncio.create_task(
                observe(
                    options,
                    quota=10**10 if RECORD else None,
                    seconds=SECONDS,
                    monitor=monitor,
                )
            )
            shell: subprocess.Popen[bytes] | None = None
            try:
                deadline = time.monotonic() + 60
                while monitor.session is None:
                    assert not observing.done(), "只读运行没有开始"
                    assert time.monotonic() < deadline, "只读运行没有开始"
                    await asyncio.sleep(0.1)
                # 先开始只读运行再拉起外壳：采集从标题菜单开始。
                shell = subprocess.Popen([str(launcher), "--autoplay", "1.5"])
                return await observing
            finally:
                window = find_window()
                if window is not None:
                    _user32.PostMessageW(window, 0x0010, 0, 0)  # WM_CLOSE
                if shell is not None:
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        shell.wait(10)
                    if shell.poll() is None:
                        shell.kill()

        assert asyncio.run(main()) == 0
        languages = [e for e in monitor.events if isinstance(e, LanguageEvent)]
        assert [e.result for e in languages][:1] == [LanguageResult.MATCH]
        records = monitor.hub.records()
        complete = [r for r in records if r.completeness is Completeness.COMPLETE]
        exact = [r for r in complete if compare_key(r.text) in story_lines()]
        print(
            f"\n{len(records)} sentences, {len(complete)} complete, "
            f"{len(exact)} of them exactly a line of the story"
        )
        assert exact
