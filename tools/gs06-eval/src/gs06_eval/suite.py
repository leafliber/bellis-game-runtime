"""整组样本的清单 `suite.toml`：纯配音、正常混音与各段背景的文件名，以及每句台词的出处。

`uv run gs06 synth` 写出，`uv run gs06 eval --suite` 读取；格式见 `docs/modules/perception.md` 的
“GS-06 的评测”。Pydantic 模型是格式的源，任何一层有未知字段即不合法。
"""

import json
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    model_validator,
)

SUITE_FILE = "suite.toml"

Ident = Annotated[StrictStr, StringConstraints(pattern=r"^[a-z][a-z0-9-]*$", max_length=48)]
FileName = Annotated[StrictStr, StringConstraints(min_length=1)]


class SuiteError(Exception):
    """清单不合法或读不出来。"""


class SuiteBackground(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Ident
    file: FileName
    """相对 `suite.toml` 的 WAV。"""
    vocal: StrictBool = False
    """带人声的背景：只记录，不设门槛。"""


class SuiteLine(BaseModel):
    """纯配音里的一句：所用的语音与语速，以及这句的 WAV 在纯配音里的起止（秒）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    voice: StrictStr
    culture: StrictStr = ""
    rate: StrictInt
    start: float = Field(ge=0)
    end: float
    text: StrictStr

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.end > self.start:
            raise ValueError(f"end 须大于 start：{self.start}、{self.end}")
        return self


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    voice: FileName
    """纯配音的 WAV（背景静音）。"""
    mix: FileName | None = None
    """正常混音的 WAV：只给判定，不算准确率。"""
    backgrounds: tuple[SuiteBackground, ...] = ()
    lines: tuple[SuiteLine, ...] = ()
    """可选：有了即按语音与语速分组。"""

    @model_validator(mode="after")
    def _unique(self) -> Self:
        names = [background.name for background in self.backgrounds]
        if len(names) != len(set(names)):
            raise ValueError(f"背景的名字重复：{names}")
        return self


def read_suite(directory: Path) -> Suite:
    path = directory / SUITE_FILE
    try:
        data = tomllib.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        raise SuiteError(f"{directory} 里没有 {SUITE_FILE}") from None
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SuiteError(f"{path} 不是合法的 UTF-8 TOML：{error}") from None
    try:
        return Suite.model_validate(data)
    except ValueError as error:
        raise SuiteError(f"{path} 不合法：{error}") from None


def _string(value: str) -> str:
    """TOML 的基本字符串（JSON 的字符串转义是它的子集）。"""
    return json.dumps(value, ensure_ascii=False)


def _number(value: float) -> str:
    return f"{value:.4f}"


def write_suite(directory: Path, suite: Suite, header: Sequence[str] = ()) -> Path:
    """写 `suite.toml`；`header` 是开头的注释行（不带 `#`）。"""
    lines = [f"# {text}" for text in header]
    lines += [
        "# 格式见 docs/modules/perception.md 的“GS-06 的评测”。",
        "",
        f"voice = {_string(suite.voice)}",
    ]
    if suite.mix is not None:
        lines.append(f"mix = {_string(suite.mix)}")
    for background in suite.backgrounds:
        lines += ["", "[[backgrounds]]", f"name = {_string(background.name)}"]
        lines.append(f"file = {_string(background.file)}")
        if background.vocal:
            lines.append("vocal = true")
    for line in suite.lines:
        lines += ["", "[[lines]]", f"voice = {_string(line.voice)}"]
        lines.append(f"culture = {_string(line.culture)}")
        lines.append(f"rate = {line.rate}")
        lines.append(f"start = {_number(line.start)}")
        lines.append(f"end = {_number(line.end)}")
        lines.append(f"text = {_string(line.text)}")
    path = directory / SUITE_FILE
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path
