"""操作员控制台的入口：`uv run operate <适配包 id> [--max-minutes N]`。

用法见 `docs/modules/game-bridge.md` 的“操作员控制台”。这是操作员在另一个终端里运行的程序：
按行读命令，`end`、`q`、Ctrl+C 与 stdin 读到 EOF 都结束会话并退出。Ctrl+C 以取消主任务的方式到达，
照常收尾：吊销令牌、断开连接、删除 SecretStore 里的连接信息。
"""

import argparse
import asyncio
import io
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from game_runtime.adapter import GAMES_DIR, Adapter, AdapterError, load_adapter
from game_runtime.channel import ChannelError
from game_runtime.local_data import LocalDataError, local_data_dir
from game_runtime.operator.console import (
    DEFAULT_MAX_MINUTES,
    ConsoleOptions,
    OperatorConsole,
)
from game_runtime.operator.terminal import TerminalConsole, stdin_lines
from game_runtime.secret_store import CredentialManagerStore, SecretStore, SecretStoreError


def out(line: str = "") -> None:
    print(line, flush=True)


def err(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _setup_output(verbose: bool) -> None:
    """输出含中文：终端的代码页编不出来时转义而不失败；`verbose` 时日志到 INFO。"""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _minutes(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"须为分钟数：{text}") from None
    if not 0 < value <= 24 * 60:
        raise argparse.ArgumentTypeError(f"须在 0 到 1440 分钟之间：{text}")
    return value


async def operate(
    adapter: Adapter, *, store: SecretStore, data_dir: Path, options: ConsoleOptions
) -> int:
    """运行控制台到会话结束，返回退出码。"""
    console = OperatorConsole(adapter, store=store, data_dir=data_dir, options=options)
    try:
        await console.open()
    except (ChannelError, SecretStoreError, OSError) as error:
        err(f"操作员控制台没能开始：{error}")
        return 1
    terminal = TerminalConsole(console, lines=stdin_lines(), write=out)
    try:
        await terminal.run()
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        out("")
        await console.end("interrupted")
    finally:
        await console.close()
    out("已退出：连接信息已从 SecretStore 删除")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """`uv run operate <适配包 id>`。"""
    parser = argparse.ArgumentParser(
        prog="operate",
        description="Run the operator console for a supervised standalone test session: confirm "
        "the adapter's preconditions, then start, pause and end the session, which is the input "
        "lease of the realtime core. Commands are read line by line; end, q, Ctrl+C or end of "
        "input end the session.",
    )
    parser.add_argument("game", help="adapter id (a directory under games/)")
    parser.add_argument(
        "--max-minutes",
        type=_minutes,
        default=DEFAULT_MAX_MINUTES,
        help=f"session time cap from the first start (default {DEFAULT_MAX_MINUTES:g})",
    )
    parser.add_argument("--verbose", action="store_true", help="log at INFO level")
    args = parser.parse_args(argv)
    _setup_output(args.verbose)
    try:
        adapter = load_adapter(GAMES_DIR / args.game)
        data_dir = local_data_dir()
        store = CredentialManagerStore()
    except (AdapterError, LocalDataError, SecretStoreError) as error:
        err(f"不能运行操作员控制台：{error}")
        return 2
    options = ConsoleOptions(max_minutes=args.max_minutes)
    return asyncio.run(operate(adapter, store=store, data_dir=data_dir, options=options))
