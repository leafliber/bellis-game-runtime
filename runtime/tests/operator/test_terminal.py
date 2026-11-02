"""操作员控制台的终端界面：经注入的输入与输出核对命令、确认项、状态变化、决策点与结束。

命令见 `docs/modules/game-bridge.md` 的“操作员控制台”。控制台用真实时钟，不连实时核；决策点由经真实
通道连上的扮演主进程的一端打开。
"""

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from game_runtime.adapter import GAMES_DIR, Precondition, Risk, load_adapter
from game_runtime.channel import RpcPeer, connect
from game_runtime.operator import MAIN_ROLE, ConsoleOptions, OperatorConsole, messages
from game_runtime.operator.terminal import TerminalConsole
from game_runtime.secret_store import MemorySecretStore

WAIT = 5.0


class Lines:
    """注入的输入：`send` 一行，`eof` 结束。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()

    def __aiter__(self) -> "Lines":
        return self

    async def __anext__(self) -> str:
        line = await self._queue.get()
        if line is None:
            raise StopAsyncIteration
        return line

    def send(self, line: str) -> None:
        self._queue.put_nowait(line + "\n")

    def eof(self) -> None:
        self._queue.put_nowait(None)


async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.002)
    return value


def console(data_dir: Path, *, max_minutes: float = 60) -> OperatorConsole:
    adapter = load_adapter(GAMES_DIR / "synthetic-game")
    adapter = adapter.model_copy(
        update={"risk": Risk(preconditions=(Precondition(id="window-visible", text="窗口在前台"),))}
    )
    return OperatorConsole(
        adapter,
        store=MemorySecretStore(),
        data_dir=data_dir,
        options=ConsoleOptions(max_minutes=max_minutes, start_delay=0.0),
    )


class Screen:
    """收下写出的每一行；`after(text)` 等到某行含 `text`。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)

    def has(self, text: str) -> bool:
        return any(text in line for line in self.lines)

    async def shows(self, text: str) -> None:
        await until(lambda: True if self.has(text) else None)


def test_a_whole_session_from_the_terminal(data_dir: Path) -> None:
    async def main() -> None:
        operator = console(data_dir)
        await operator.open()
        lines, screen = Lines(), Screen()
        terminal = TerminalConsole(operator, lines=lines, write=screen.write)
        run = asyncio.ensure_future(terminal.run())
        try:
            await screen.shows("确认 1/2：窗口在前台（y/n）")
            assert screen.has("操作员控制台：")
            assert screen.has("会话时长上限 60 分钟")
            assert screen.has(f"会话记录：{operator.log_path}")
            lines.send("start")
            await screen.shows("不能执行：还有确认项没有回答")
            lines.send("y")
            await screen.shows("确认 2/2：本次是受监督的测试，不直播（y/n）")
            lines.send("Y")
            await screen.shows("确认项已全部回答。输入 start 开始。")
            lines.send("s")
            await screen.shows("开始：运行中，会话剩余 1:00:00；实时核还没连上，连上后授予许可")
            lines.send("goal   推进到第一次选择  ")
            await screen.shows("目标：推进到第一次选择")
            lines.send("goal")
            await screen.shows("目标已清除")
            lines.send("jump")
            await screen.shows("不认识的命令：jump")
            lines.send("3")
            await screen.shows("不能执行：没有打开的决策点")

            # 主进程打开一个有计时的决策点。
            credentials = operator.credentials
            peer = RpcPeer(
                await connect(credentials.path, MAIN_ROLE, credentials.key), timeout=WAIT
            )
            resolved: list[messages.DecisionResolved] = []
            peer.on_notification(
                messages.DECISION_RESOLVED,
                lambda params: resolved.append(messages.DecisionResolved.model_validate(params)),
            )
            await screen.shows("主进程已连上")
            decision = messages.DecisionOpened(
                id="choice-1",
                prompt="去哪里？",
                options=(
                    messages.DecisionOption(index=0, text="钟楼"),
                    messages.DecisionOption(index=1, text="商店"),
                ),
                candidates=(1,),
                deadline_ns=time.perf_counter_ns() + 30_000_000_000,
            )
            peer.notify(messages.DECISION_OPENED, decision.params())
            await screen.shows("决策点 choice-1：去哪里？")
            assert screen.has("  0. 钟楼")
            assert screen.has("  1. 商店（候选第 1）")
            assert any(line.startswith("剩余 0:") for line in screen.lines), screen.lines
            lines.send("1")
            await screen.shows("已给出决策点 choice-1 的选择 1")
            await until(lambda: resolved or None)
            assert resolved == [messages.DecisionResolved(id="choice-1", index=1)]
            peer.notify(
                messages.DECISION_CLOSED,
                messages.DecisionClosed(id="choice-1", index=1, by="operator").params(),
            )
            await screen.shows("决策点 choice-1 已关闭：按操作员的选择执行 1")
            peer.notify(
                messages.ACTION, messages.Action(category="select", summary="点击“商店”").params()
            )
            await screen.shows("动作（select）：点击“商店”")

            lines.send("p")
            await screen.shows("暂停（operator）：令牌已吊销。输入 start 继续。")
            lines.send("p")
            await screen.shows("不能执行：没有在运行")
            lines.send("start")
            await screen.shows("继续：运行中")
            lines.send("q")
            assert await asyncio.wait_for(run, WAIT) == "operator"
            assert screen.has("会话结束（操作员结束）：确认 2，开始与继续 2，暂停 1")
            await peer.close()
        finally:
            run.cancel()
            await operator.close()

    asyncio.run(main())


def test_end_of_input_ends_the_session(data_dir: Path) -> None:
    async def main() -> None:
        operator = console(data_dir)
        await operator.open()
        lines, screen = Lines(), Screen()
        try:
            lines.send("y")
            lines.eof()
            reason = await asyncio.wait_for(
                TerminalConsole(operator, lines=lines, write=screen.write).run(), WAIT
            )
            assert reason == "eof"
            assert operator.ended
            assert screen.has("会话结束（输入已结束）")
        finally:
            await operator.close()

    asyncio.run(main())


def test_answering_n_ends_the_session_from_the_terminal(data_dir: Path) -> None:
    async def main() -> None:
        operator = console(data_dir)
        await operator.open()
        lines, screen = Lines(), Screen()
        try:
            lines.send("n")
            reason = await asyncio.wait_for(
                TerminalConsole(operator, lines=lines, write=screen.write).run(), WAIT
            )
            assert reason == "declined"
            assert screen.has("会话结束（确认项答了 n）")
        finally:
            await operator.close()

    asyncio.run(main())


def test_the_time_cap_ends_the_terminal_while_it_waits_for_input(data_dir: Path) -> None:
    async def main() -> None:
        operator = console(data_dir, max_minutes=0.002)  # 0.12 秒
        await operator.open()
        lines, screen = Lines(), Screen()
        try:
            lines.send("y")
            lines.send("y")
            lines.send("start")
            reason = await asyncio.wait_for(
                TerminalConsole(operator, lines=lines, write=screen.write).run(), WAIT
            )
            assert reason == "time_cap"
            assert screen.has("会话结束（到了时长上限）")
        finally:
            await operator.close()

    asyncio.run(main())
