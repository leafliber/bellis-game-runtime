"""真机手动测试：经 stdin 管道运行真实的 `uv run operate synthetic-game`，用真实的凭据管理器。

`uv run pytest -m manual runtime/tests/operator/test_operate_window.py -s`（只在 Windows 上，不在
`uv run check` 中）。它写入当前用户凭据管理器里真实的 `operator-session` 条目（服务名
`bellis-game-runtime`），结束时控制台自己删除，测试再确认已删除；条目事先已存在（另有控制台在运行）时
跳过，不覆盖。会话记录写进临时的本地数据目录。
"""

import asyncio
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

import pytest

from game_runtime.adapter import GAMES_DIR, load_adapter
from game_runtime.local_data import DATA_DIR_ENV
from game_runtime.operator import OperatorClient, SessionState, sessions_dir
from game_runtime.secret_store import (
    OPERATOR_SESSION_ENTRY,
    CredentialManagerStore,
    read_session_info,
)

pytestmark = [
    pytest.mark.manual,
    pytest.mark.skipif(sys.platform != "win32", reason="凭据管理器只在 Windows 上可用"),
]

REPO = Path(__file__).resolve().parents[3]
WAIT = 60.0


class Output:
    """在后台线程上逐行收下子进程的输出。"""

    def __init__(self, stream: IO[str]) -> None:
        self.lines: list[str] = []
        self._queue: queue.Queue[str] = queue.Queue()
        self.thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self.thread.start()

    def _read(self, stream: IO[str]) -> None:
        for line in stream:
            print(line, end="", flush=True)
            self._queue.put(line.rstrip("\n"))

    def wait_for(self, text: str, timeout: float = WAIT) -> str:
        deadline = time.monotonic() + timeout
        while True:
            for line in self.lines:
                if text in line:
                    return line
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"没有等到“{text}”：{self.lines}"
            try:
                self.lines.append(self._queue.get(timeout=remaining))
            except queue.Empty:
                continue


async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.02)
    return value


def test_operate_runs_a_session_over_a_stdin_pipe(tmp_path: Path) -> None:
    store = CredentialManagerStore()
    if read_session_info(store) is not None:
        pytest.skip("凭据管理器里已有 operator-session 条目：先结束正在运行的操作员控制台")
    uv = shutil.which("uv")
    assert uv is not None, "PATH 上没有 uv"
    data_dir = Path("\\\\?\\" + str(tmp_path.resolve())) / "data"
    env = {**os.environ, DATA_DIR_ENV: str(data_dir), "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen(
        [uv, "run", "operate", "synthetic-game", "--max-minutes", "5"],
        cwd=REPO,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    stdin = process.stdin
    output = Output(process.stdout)

    def send(line: str) -> None:
        stdin.write(line + "\n")
        stdin.flush()

    adapter = load_adapter(GAMES_DIR / "synthetic-game")

    async def as_main_process() -> None:
        client = OperatorClient(adapter, store=store)
        await client.connect()
        session = client.session
        assert session is not None
        assert session.state is SessionState.WAITING
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, send, "y")
        await loop.run_in_executor(None, send, "start")
        await until(lambda: True if client.may_autoplay else None)
        await loop.run_in_executor(None, send, "p")
        await until(
            lambda: (
                True
                if client.session is not None and client.session.state is SessionState.PAUSED
                else None
            )
        )
        await loop.run_in_executor(None, send, "q")
        await until(lambda: True if client.ended is not None else None)
        assert client.ended in {"ended", "disconnected"}
        await client.close()

    try:
        output.wait_for("确认 1/1：本次是受监督的测试，不直播（y/n）")
        info = read_session_info(store)
        assert info is not None
        assert info.launcher_pid > 0
        asyncio.run(as_main_process())
        output.wait_for("会话结束（操作员结束）")
        assert process.wait(WAIT) == 0
        output.wait_for("已退出")
        assert read_session_info(store) is None
        logs = list(sessions_dir(data_dir, "synthetic-game").glob("*.jsonl"))
        assert len(logs) == 1
        text = logs[0].read_text(encoding="utf-8")
        for event in ("session_started", "confirmation", "started", "paused", "session_ended"):
            assert f'"event":"{event}"' in text, event
        assert info.channel_key not in text
    finally:
        # 停止的顺序：先关 stdin（控制台读到 EOF 即结束会话并退出），超时再强制终止。
        stdin.close()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(10)
        output.thread.join(10)
        process.stdout.close()
        # 控制台没能收尾时，删掉它留下的条目（事先没有这个条目，见开头）。
        if read_session_info(store) is not None:
            store.delete(OPERATOR_SESSION_ENTRY)
