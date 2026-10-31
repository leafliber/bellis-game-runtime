"""录制的标注：按帧范围标的画面、每一句的真值与配音段。

格式与规则见 `docs/modules/offline-tools.md` 的“标注”。标注在标定与评测时由工具或手工写入；这里给出
读写与校验，校验要对照录制的帧索引与适配包。
"""

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

from game_runtime.adapter import Adapter
from game_runtime.recording.layout import ANNOTATIONS_FILE, write_atomic

type _FrameNumber = Annotated[StrictInt, Field(ge=1)]
type _Nanos = Annotated[StrictInt, Field(ge=0)]
type _Text = Annotated[str, StringConstraints(pattern=r"^\S(?:[\s\S]*\S)?$")]
"""非空，首尾不是空白。"""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScreenSpan(_Model):
    """一段帧上是适配包的哪个画面。"""

    screen: str
    """适配包 `screens` 的 `id`。"""
    first: _FrameNumber
    last: _FrameNumber

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.first > self.last:
            raise ValueError(f"first {self.first} 大于 last {self.last}")
        return self


class LineTruth(_Model):
    """一句的真值。"""

    speaker: _Text | None
    """说话人；旁白为 None。"""
    text: _Text
    """全文。"""
    first: _FrameNumber
    """出现的第一帧。"""
    complete: _FrameNumber | None
    """全文都显示出来的第一帧；没显示完就被推进时为 None。"""
    last: _FrameNumber
    """出现的最后一帧。"""

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        complete = self.complete
        if complete is None:
            if self.first > self.last:
                raise ValueError(f"first {self.first} 大于 last {self.last}")
        elif not self.first <= complete <= self.last:
            raise ValueError(f"须 first ≤ complete ≤ last：{self.first}、{complete}、{self.last}")
        return self


class VoiceSegment(_Model):
    """一段配音，按采集时刻。"""

    start_ns: _Nanos
    end_ns: _Nanos

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start_ns >= self.end_ns:
            raise ValueError(f"start_ns {self.start_ns} 不早于 end_ns {self.end_ns}")
        return self


class Annotations(_Model):
    """`annotations.json`。"""

    format: Literal[1] = 1
    screens: tuple[ScreenSpan, ...] = ()
    lines: tuple[LineTruth, ...] = ()
    voice: tuple[VoiceSegment, ...] = ()

    @model_validator(mode="after")
    def _sorted(self) -> Self:
        problems: list[str] = []
        for index in range(1, len(self.screens)):
            before, span = self.screens[index - 1], self.screens[index]
            if span.first <= before.last:
                problems.append(f"screens[{index}]：与前一段重叠或没有按 first 排序")
        for index in range(1, len(self.lines)):
            if self.lines[index].first < self.lines[index - 1].first:
                problems.append(f"lines[{index}]：没有按 first 排序")
        for index in range(1, len(self.voice)):
            if self.voice[index].start_ns < self.voice[index - 1].end_ns:
                problems.append(f"voice[{index}]：与前一段重叠或没有按 start_ns 排序")
        if problems:
            raise ValueError("\n".join(problems))
        return self

    def to_json(self) -> bytes:
        return (self.model_dump_json(indent=2) + "\n").encode()


def load_annotations(directory: Path) -> Annotations:
    """读录制目录里的标注；没有文件时为空的标注。"""
    path = directory / ANNOTATIONS_FILE
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return Annotations()
    return Annotations.model_validate_json(data)


def save_annotations(directory: Path, annotations: Annotations) -> None:
    """写进录制目录：先写临时文件再改名。"""
    write_atomic(directory / ANNOTATIONS_FILE, annotations.to_json())


def check_annotations(
    annotations: Annotations,
    *,
    frame_numbers: Sequence[int],
    adapter: Adapter,
    audio_range: tuple[int, int] | None,
) -> list[str]:
    """对照录制核对标注：帧号在帧索引里、画面是适配包的画面、配音段落在录下的音频之内。

    `audio_range` 是第一块的采集时刻与最后一块的结束时刻，没有音频为 None。返回逐条的问题。
    """
    known = set(frame_numbers)
    screens = {screen.id for screen in adapter.screens}
    return list(_problems(annotations, known, screens, audio_range))


def _problems(
    annotations: Annotations,
    known: set[int],
    screens: set[str],
    audio_range: tuple[int, int] | None,
) -> Iterator[str]:
    for index, span in enumerate(annotations.screens):
        if span.screen not in screens:
            yield f"screens[{index}].screen：适配包没有画面 {span.screen}"
        yield from _frames(f"screens[{index}]", known, span.first, span.last)
    for index, line in enumerate(annotations.lines):
        yield from _frames(f"lines[{index}]", known, line.first, line.complete, line.last)
    for index, segment in enumerate(annotations.voice):
        if audio_range is None:
            yield f"voice[{index}]：录制里没有音频"
        elif segment.start_ns < audio_range[0] or segment.end_ns > audio_range[1]:
            yield f"voice[{index}]：不在录下的音频 {audio_range[0]}–{audio_range[1]} 之内"


def _frames(where: str, known: set[int], *numbers: int | None) -> Iterator[str]:
    for number in numbers:
        if number is not None and number not in known:
            yield f"{where}：帧 {number} 不在帧索引里"
