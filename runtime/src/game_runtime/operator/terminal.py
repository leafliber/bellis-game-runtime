"""操作员控制台的终端界面：按行读命令，逐行写出状态变化、确认项、决策点与释放。不加界面依赖。

命令见 `docs/modules/game-bridge.md` 的“操作员控制台”：

| 命令 | 作用 |
| --- | --- |
| `y`、`n` | 回答当前的确认项 |
| `start`、`s` | 开始或继续 |
| `pause`、`p` | 暂停 |
| `goal <文字>`、`goal` | 设定或清除当前目标 |
| `<序号>` | 给出当前决策点的选择 |
| `end`、`q` | 结束会话并退出；Ctrl+C 与 stdin 读到 EOF 同样 |

输入与输出由调用方给出（`lines`、`write`），测试注入；`stdin_lines` 是真实终端的输入。
"""

import asyncio
import contextlib
import sys
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Final

from game_runtime.operator.console import (
    ConsoleError,
    ConsoleEvent,
    EventKind,
    OperatorConsole,
)
from game_runtime.operator.messages import SessionState

HELP: Final = (
    "命令：y、n 回答确认项；start 或 s 开始、继续；pause 或 p 暂停；goal <文字> 设定目标，"
    "goal 清除；<序号> 给出决策点的选择；end 或 q 结束会话并退出（Ctrl+C 同样）"
)

_ROLES: Final = {"core": "实时核", "main": "主进程"}
_CLOSED_BY: Final = {
    "operator": "按操作员的选择执行",
    "default": "到余量线执行排第一的候选",
    "withdrawn": "画面已变，不再执行",
}
_END_REASONS: Final = {
    "operator": "操作员结束",
    "declined": "确认项答了 n",
    "time_cap": "到了时长上限",
    "eof": "输入已结束",
    "interrupted": "Ctrl+C",
}


async def stdin_lines() -> AsyncIterator[str]:
    """真实终端的输入：在后台线程上逐行读 stdin，读到 EOF 即结束。"""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def read() -> None:
        while True:
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError):
                line = ""
            loop.call_soon_threadsafe(queue.put_nowait, line or None)
            if not line:
                return

    threading.Thread(target=read, name="operator-stdin", daemon=True).start()
    while (line := await queue.get()) is not None:
        yield line


def _clock(nanoseconds: int) -> str:
    seconds = max(0, round(nanoseconds / 1e9))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


class TerminalConsole:
    """把 `OperatorConsole` 接到一个终端：`run` 读命令直到会话结束，返回结束的原因。"""

    def __init__(
        self,
        console: OperatorConsole,
        *,
        lines: AsyncIterator[str],
        write: Callable[[str], None],
    ) -> None:
        self._console = console
        self._lines = lines
        self._write = write

    async def run(self) -> str:
        """写出开始的几行，然后读命令，到会话结束（命令或到时）为止；输入读到 EOF 时结束会话。

        返回结束的原因。
        """
        console = self._console
        console.add_listener(self._on_event)
        self._banner()
        self._prompt()
        reader = asyncio.ensure_future(self._read())
        ended = asyncio.ensure_future(console.wait_ended())
        try:
            done, _ = await asyncio.wait({reader, ended}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (reader, ended):
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        if reader in done:
            reader.result()  # 读命令时出的错照常抛出
        if not console.ended:
            await console.end("eof")
        return console.end_reason or "eof"

    async def _read(self) -> None:
        async for line in self._lines:
            await self.command(line)
            if self._console.ended:
                return

    async def command(self, line: str) -> None:
        """执行一行命令；不能执行时写出原因。"""
        text = line.strip()
        if not text:
            return
        word, _, rest = text.partition(" ")
        console = self._console
        try:
            match word.lower():
                case "y" | "n" if not rest:
                    await console.answer(word.lower() == "y")
                    self._prompt()
                case "start" | "s" if not rest:
                    await console.start()
                case "pause" | "p" if not rest:
                    await console.pause()
                case "goal":
                    console.set_goal(rest)
                case "end" | "q" if not rest:
                    await console.end("operator")
                case "help" | "?":
                    self._write(HELP)
                case _ if word.isdigit() and not rest:
                    console.choose(int(word))
                case _:
                    self._write(f"不认识的命令：{text}")
                    self._write(HELP)
        except ConsoleError as error:
            self._write(f"不能执行：{error}")

    def _banner(self) -> None:
        console = self._console
        adapter = console.adapter
        self._write(f"操作员控制台：{adapter.name}（{adapter.id}），独立运行的受监督测试")
        self._write(f"会话时长上限 {console.options.max_minutes:g} 分钟，从第一次开始算起")
        if console.log_path is not None:
            self._write(f"会话记录：{console.log_path}")
        self._write(HELP)

    def _prompt(self) -> None:
        console = self._console
        item = console.pending_confirmation
        items = console.confirmations
        if item is not None:
            self._write(f"确认 {items.index(item) + 1}/{len(items)}：{item.text}（y/n）")
        elif console.state is SessionState.WAITING:
            self._write("确认项已全部回答。输入 start 开始。")

    def _remaining(self, deadline_ns: int | None) -> str:
        if deadline_ns is None:
            return "无计时"
        return f"剩余 {_clock(deadline_ns - self._console.now_ns())}"

    def _on_event(self, event: ConsoleEvent) -> None:
        for line in self.describe(event):
            self._write(line)

    def describe(self, event: ConsoleEvent) -> list[str]:
        """一个事件写成的几行。"""
        fields = event.fields
        match event.kind:
            case EventKind.COUNTDOWN:
                seconds = fields["seconds"]
                if seconds <= 0:
                    return ["开始……"]
                return [f"倒数 {seconds:g} 秒后授予许可，请把游戏窗口切到前台……"]
            case EventKind.COUNTDOWN_CANCELLED:
                return ["已取消倒数。"]
            case EventKind.STARTED | EventKind.RESUMED:
                verb = "开始" if event.kind is EventKind.STARTED else "继续"
                ends_at = fields.get("ends_at_ns")
                tail = "" if ends_at is None else f"，会话{self._remaining(ends_at)}"
                core = "" if self._console.core_connected else "；实时核还没连上，连上后授予许可"
                return [f"{verb}：运行中{tail}{core}"]
            case EventKind.PAUSED:
                return [f"暂停（{fields['reason']}）：令牌已吊销。输入 start 继续。"]
            case EventKind.LEASE_GRANTED:
                interval = self._console.options.renew_interval_ns / 1e9
                return [f"已授予实时核令牌 {fields['token']}，每 {interval:g} 秒续约"]
            case EventKind.LEASE_REFUSED:
                return [f"实时核拒绝了令牌 {fields['token']}：{fields['message']}；转为暂停"]
            case EventKind.GOAL:
                goal = fields["goal"]
                return ["目标已清除" if goal is None else f"目标：{goal}"]
            case EventKind.CONNECTED:
                return [f"{_ROLES[fields['role']]}已连上"]
            case EventKind.DISCONNECTED:
                return [f"{_ROLES[fields['role']]}已断开"]
            case EventKind.REJECTED:
                return [f"拒绝了一个连接（{fields['role']}）：{fields['reason']}"]
            case EventKind.INPUT_RELEASED:
                return [
                    f"释放回执 #{fields['release_id']} {fields['reason']}"
                    f"（{fields['verification']}）"
                ]
            case EventKind.INPUT_RESIDUAL:
                accepted = "已提交" if fields["repair_accepted"] else "有事件被拒收"
                return [
                    f"释放 #{fields['release_id']} 检测到 {fields['items']} 项残留，"
                    f"补发抬起{accepted}"
                ]
            case EventKind.INPUT_VERIFICATION:
                return [f"释放 #{fields['release_id']} 物理核验 {fields['result']}"]
            case EventKind.DECISION_OPENED:
                return self._decision(fields)
            case EventKind.DECISION_CHOSEN:
                return [f"已给出决策点 {fields['id']} 的选择 {fields['index']}"]
            case EventKind.DECISION_CLOSED:
                index = fields["index"]
                chosen = "" if index is None else f" {index}"
                return [f"决策点 {fields['id']} 已关闭：{_CLOSED_BY[fields['by']]}{chosen}"]
            case EventKind.ACTION:
                return [f"动作（{fields['category']}）：{fields['summary']}"]
            case EventKind.SESSION_ENDED:
                return self._ended(fields)
            case EventKind.SESSION_STARTED | EventKind.CONFIRMATION:
                return []

    def _decision(self, fields: Mapping[str, Any]) -> list[str]:
        candidates: list[int] = list(fields["candidates"])
        lines = [f"决策点 {fields['id']}：{fields['prompt']}"]
        for option in fields["options"]:
            index: int = option["index"]
            rank = f"（候选第 {candidates.index(index) + 1}）" if index in candidates else ""
            lines.append(f"  {index}. {option['text']}{rank}")
        lines.append(f"{self._remaining(fields['deadline_ns'])}。输入序号给出选择。")
        return lines

    def _ended(self, fields: Mapping[str, Any]) -> list[str]:
        reason: str = fields["reason"]
        counts: Mapping[str, int] = fields["counts"]
        shown = (
            ("确认", counts.get(EventKind.CONFIRMATION, 0)),
            ("开始与继续", counts.get(EventKind.STARTED, 0) + counts.get(EventKind.RESUMED, 0)),
            ("暂停", counts.get(EventKind.PAUSED, 0)),
            ("释放", counts.get(EventKind.INPUT_RELEASED, 0)),
            ("决策点", counts.get(EventKind.DECISION_OPENED, 0)),
            ("动作", counts.get(EventKind.ACTION, 0)),
        )
        summary = "，".join(f"{name} {count}" for name, count in shown)
        return [f"会话结束（{_END_REASONS.get(reason, reason)}）：{summary}"]
