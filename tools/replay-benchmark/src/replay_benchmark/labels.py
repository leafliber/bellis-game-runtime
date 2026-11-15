"""独立评分文件。坐标为录制帧的客户区像素。"""

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt, model_validator

from game_runtime.recording.reader import Recording


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FrameSpan(_Model):
    first: PositiveInt
    last: PositiveInt

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.last < self.first:
            raise ValueError("清单末帧早于首帧")
        return self

    def contains(self, frame: int) -> bool:
        return self.first <= frame <= self.last


class Box(_Model):
    left: NonNegativeInt
    top: NonNegativeInt
    right: PositiveInt
    bottom: PositiveInt

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.left >= self.right or self.top >= self.bottom:
            raise ValueError("目标框必须有面积")
        return self

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


class ClickTarget(FrameSpan):
    box: Box


class Labels(_Model):
    format: Literal[1] = 1
    targets: tuple[ClickTarget, ...]
    completion: FrameSpan | None = None
    adaptation_hours: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None

    def check(self, recording: Recording) -> None:
        known = set(recording.frame_numbers)
        spans: tuple[FrameSpan, ...] = self.targets
        if self.completion is not None:
            spans = (*spans, self.completion)
        for span in spans:
            if span.first not in known or span.last not in known:
                raise ValueError("评分范围引用的帧不在录制索引中")
        for target in self.targets:
            if any(
                target.contains(frame.frame_number)
                and (target.box.right > frame.width or target.box.bottom > frame.height)
                for frame in recording.frames
            ):
                raise ValueError("评分目标框超出录制客户区")


class ModelPrice(_Model):
    vendor: str
    model: str
    input_per_million: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    output_per_million: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class Prices(_Model):
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    models: tuple[ModelPrice, ...]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len({(item.vendor, item.model) for item in self.models}) != len(self.models):
            raise ValueError("每个厂商和型号只可配置一个单价")
        return self


def read_labels(path: Path) -> Labels:
    return Labels.model_validate_json(path.read_bytes())


def read_prices(path: Path) -> Prices:
    return Prices.model_validate_json(path.read_bytes())
