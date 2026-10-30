"""子进程的 R-06 停止顺序与孙进程清理：用替身子进程（`standin.py`）验证主进程一侧的回退路径。

真实实时核的启动与停止见 `test_core.py`。Job Object 只在 Windows 上有，相关测试在其它平台跳过。
"""

import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from game_runtime.core_link import (
    ManagedProcess,
    StopResult,
    StopStage,
    StopTimeouts,
    base_environment,
)

FAST = StopTimeouts(shutdown=0.5, stdin=0.5, kill=5.0)
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Job Object 只在 Windows 上有")


async def wait_state(path: Path, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, "替身子进程没有就绪"
        await asyncio.sleep(0.02)
    return json.loads(path.read_text())


async def run_standin(
    standin: Path,
    tmp_path: Path,
    flags: list[str],
    act: Callable[[ManagedProcess, dict[str, Any]], Awaitable[StopResult]],
) -> tuple[StopResult, dict[str, Any], float]:
    """拉起替身子进程，就绪后执行 `act`，返回结果、状态文件与 `act` 用时；任何失败都不留下进程。"""
    state = tmp_path / "state.json"
    argv = [sys.executable, str(standin), str(state), *flags]
    process = ManagedProcess.start("standin", argv, base_environment())
    try:
        info = await wait_state(state)
        started = time.monotonic()
        result = await act(process, info)
        return result, info, time.monotonic() - started
    finally:
        process.kill()
        await asyncio.wait_for(asyncio.shield(process.exited), 10)


def never_answers() -> Callable[[], Awaitable[object]]:
    async def shutdown() -> object:
        await asyncio.Event().wait()
        return None

    return shutdown


def test_unanswered_stop_request_falls_back_to_stdin(standin: Path, tmp_path: Path) -> None:
    async def main() -> None:
        result, _, elapsed = await run_standin(
            standin, tmp_path, [], lambda process, _: process.stop(never_answers(), FAST)
        )
        assert result == StopResult(StopStage.STDIN_EOF, 0)
        # 先等满第 1 步的时限，再关 stdin。
        assert FAST.shutdown <= elapsed < FAST.shutdown + 5

    asyncio.run(main())


def test_failed_stop_request_moves_on_without_waiting(standin: Path, tmp_path: Path) -> None:
    async def refuse() -> object:
        raise ConnectionError("核连接已断开")

    async def main() -> None:
        result, _, _ = await run_standin(
            standin, tmp_path, [], lambda process, _: process.stop(refuse, FAST)
        )
        assert result == StopResult(StopStage.STDIN_EOF, 0)

    asyncio.run(main())


def test_child_ignoring_stdin_is_killed(
    standin: Path, tmp_path: Path, gone: Callable[[int, float], bool]
) -> None:
    async def main() -> None:
        result, info, elapsed = await run_standin(
            standin,
            tmp_path,
            ["--ignore-stdin"],
            lambda process, _: process.stop(never_answers(), FAST),
        )
        assert result.stage is StopStage.KILLED
        assert result.returncode not in (None, 0)
        assert FAST.shutdown + FAST.stdin <= elapsed < FAST.shutdown + FAST.stdin + 5
        assert gone(info["pid"], 5)

    asyncio.run(main())


def test_child_that_already_exited(standin: Path, tmp_path: Path) -> None:
    async def act(process: ManagedProcess, _: dict[str, Any]) -> StopResult:
        process.kill()
        await asyncio.wait_for(asyncio.shield(process.exited), 10)
        return await process.stop(never_answers(), FAST)

    async def main() -> None:
        result, _, elapsed = await run_standin(standin, tmp_path, [], act)
        assert result.stage is StopStage.ALREADY_EXITED
        assert elapsed < FAST.shutdown

    asyncio.run(main())


@windows_only
def test_grandchildren_are_cleaned_up_through_the_job(
    standin: Path, tmp_path: Path, gone: Callable[[int, float], bool]
) -> None:
    async def act(process: ManagedProcess, info: dict[str, Any]) -> StopResult:
        # 孙进程由挂起创建、进 job 之后才运行的子进程拉起，所以也在 job 里。
        active = process.active_processes()
        assert (active or 0) >= 2
        assert not gone(info["grandchild"], 0)
        return await process.stop(never_answers(), FAST)

    async def main() -> None:
        result, info, _ = await run_standin(standin, tmp_path, ["--grandchild"], act)
        # 子进程自己读到 EOF 退出，孙进程不管；主进程收拾 job 里剩下的。
        assert result == StopResult(StopStage.STDIN_EOF, 0)
        assert gone(info["grandchild"], 5), "孙进程残留"
        assert gone(info["pid"], 5)

    asyncio.run(main())


@windows_only
def test_killing_the_child_kills_its_grandchildren(
    standin: Path, tmp_path: Path, gone: Callable[[int, float], bool]
) -> None:
    async def act(process: ManagedProcess, _: dict[str, Any]) -> StopResult:
        return await process.stop(never_answers(), FAST)

    async def main() -> None:
        result, info, _ = await run_standin(
            standin, tmp_path, ["--grandchild", "--ignore-stdin"], act
        )
        assert result.stage is StopStage.KILLED
        assert gone(info["grandchild"], 5), "孙进程残留"

    asyncio.run(main())


def test_missing_executable_fails_to_start(tmp_path: Path) -> None:
    async def main() -> None:
        with pytest.raises(FileNotFoundError):
            ManagedProcess.start("missing", [str(tmp_path / "no-such-program")], base_environment())

    asyncio.run(main())


def test_base_environment_keeps_only_system_variables() -> None:
    source = {"Path": "p", "SystemRoot": "r", "BELLIS_CHANNEL_KEY": "k", "HOME": "h", "LANG": "l"}
    assert base_environment(source, windows=True) == {"Path": "p", "SystemRoot": "r"}
    assert base_environment(source, windows=False) == {"HOME": "h", "LANG": "l"}
    assert "BELLIS_CHANNEL_KEY" not in base_environment(dict(os.environ))
