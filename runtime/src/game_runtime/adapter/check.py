"""适配包校验：`uv run check-adapters`，也是 `uv run check` 的 `game adapters` 一项。

不给参数时校验 `games/` 下的每个子目录，给出目录时只校验这几个；规则见
`docs/modules/game-adapter.md` 的“校验”。尚未标定的项逐项列出，不算错误。
"""

import argparse
import io
import sys
from collections.abc import Sequence
from pathlib import Path

from game_runtime.adapter.load import ADAPTER_FILE, GAMES_DIR, AdapterError, load_adapter
from game_runtime.adapter.schema import Adapter


def _visible(path: Path) -> bool:
    return not path.name.startswith(".")


def package_dirs(games_dir: Path) -> list[Path]:
    """`games_dir` 下的适配包目录；那里的普通文件按 `check_games_dir` 报错。"""
    return sorted(path for path in games_dir.iterdir() if path.is_dir() and _visible(path))


def check_games_dir(games_dir: Path) -> list[str]:
    """`games/` 下只有适配包目录（以 `.` 开头的除外）。"""
    if not games_dir.is_dir():
        return [f"{games_dir} 不是目录"]
    return [
        f"{games_dir}：{path.name} 不是适配包目录，games/ 下只能有适配包目录"
        for path in sorted(games_dir.iterdir())
        if not path.is_dir() and _visible(path)
    ]


def check_package(directory: Path) -> Adapter:
    """校验一个适配包：目录里只有声明文件（以 `.` 开头的除外），声明合法。"""
    if not directory.is_dir():
        raise AdapterError(directory, ["不是目录"])
    stray = [
        f"{path.name}：适配包目录里只能有 {ADAPTER_FILE}，从游戏派生的东西放本地数据目录"
        for path in sorted(directory.iterdir())
        if path.name != ADAPTER_FILE and _visible(path)
    ]
    try:
        adapter = load_adapter(directory)
    except AdapterError as error:
        raise AdapterError(error.directory, [*stray, *error.problems]) from None
    if stray:
        raise AdapterError(directory.resolve(), stray)
    return adapter


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # 输出含中文；输出到管道而代码页编不出来时不让校验因此失败。
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(
        prog="check-adapters", description="Validate game adapter packages."
    )
    parser.add_argument(
        "packages",
        nargs="*",
        type=Path,
        help="adapter package directories (default: every directory under games/)",
    )
    args = parser.parse_args(argv)
    packages: list[Path] = args.packages
    problems: list[str] = []
    if not packages:
        problems = check_games_dir(GAMES_DIR)
        packages = package_dirs(GAMES_DIR) if GAMES_DIR.is_dir() else []
        if not packages:
            problems.append(f"{GAMES_DIR} 下没有适配包")
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    failed = len(problems)
    for directory in packages:
        try:
            adapter = check_package(directory)
        except AdapterError as error:
            failed += 1
            print(f"FAIL {error}", file=sys.stderr)
            continue
        print(f"ok   {adapter.id}：{adapter.name} {adapter.version}")
        for item in adapter.uncalibrated():
            print(f"     尚未标定：{item}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
