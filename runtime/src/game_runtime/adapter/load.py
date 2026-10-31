"""读取适配包的声明：`<目录>/adapter.toml` 经 `tomllib` 读出，按 `Adapter` 校验。

规则见 `docs/modules/game-adapter.md` 的“格式”。读不出或不合法时抛 `AdapterError`，逐条给出问题。
"""

import tomllib
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError
from pydantic_core import ErrorDetails

from game_runtime.adapter.schema import Adapter

ADAPTER_FILE = "adapter.toml"
"""适配包目录里的声明文件名。"""

# 本文件位于 runtime/src/game_runtime/adapter/，工作区成员以可编辑方式安装。
GAMES_DIR = Path(__file__).resolve().parents[4] / "games"
"""本仓库的适配包目录 `games/`。"""


class AdapterError(Exception):
    """适配包读不出来或不合法；`problems` 逐条给出问题。"""

    def __init__(self, directory: Path, problems: Sequence[str]) -> None:
        self.directory = directory
        self.problems = tuple(problems)
        lines = "\n".join(f"  {problem}" for problem in self.problems)
        super().__init__(f"适配包 {directory} 不合法：\n{lines}")


def load_adapter(directory: Path) -> Adapter:
    """读取并校验 `directory` 下的声明；适配包的 `id` 须与目录名相同。"""
    directory = directory.resolve()
    path = directory / ADAPTER_FILE
    try:
        data = tomllib.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        raise AdapterError(directory, [f"没有 {ADAPTER_FILE}"]) from None
    except UnicodeDecodeError as error:
        raise AdapterError(directory, [f"{ADAPTER_FILE} 不是 UTF-8：{error}"]) from None
    except tomllib.TOMLDecodeError as error:
        raise AdapterError(directory, [f"{ADAPTER_FILE} 不是合法的 TOML：{error}"]) from None
    try:
        adapter = Adapter.model_validate(data)
    except ValidationError as error:
        raise AdapterError(directory, describe(error)) from None
    if adapter.id != directory.name:
        raise AdapterError(directory, [f"id：{adapter.id} 与目录名 {directory.name} 不同"])
    return adapter


def describe(error: ValidationError) -> list[str]:
    """把校验错误拆成逐条的问题，每条前面是出错的位置。"""
    problems: list[str] = []
    for detail in error.errors():
        where = _location(detail)
        for line in _message(detail).splitlines():
            problems.append(f"{where}：{line}" if where else line)
    return problems


def _location(detail: ErrorDetails) -> str:
    where = ""
    for part in detail["loc"]:
        if isinstance(part, int):
            where += f"[{part}]"
        elif part == "[key]":
            where += "（键）"
        else:
            where += f".{part}" if where else part
    return where


def _message(detail: ErrorDetails) -> str:
    # 校验函数抛出的 ValueError 原样给出，不带 pydantic 的 "Value error, " 前缀。
    context = detail.get("ctx") or {}
    if detail["type"] == "value_error" and "error" in context:
        return str(context["error"])
    return detail["msg"]
