"""GS-04 的样本：一个目录里的 `samples.toml` 与它引用的 PNG。

格式见 `docs/modules/perception.md` 的“GS-04 的评测工具”。Pydantic 模型是格式的源，任何一层有未知
字段即不合法。
"""

import json
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Self

import cv2
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, StrictStr, StringConstraints, model_validator

from game_runtime.adapter import GAMES_DIR, Adapter, PixelRect, load_adapter

SAMPLES_FILE = "samples.toml"

Ident = Annotated[StrictStr, StringConstraints(pattern=r"^[a-z][a-z0-9-]*$", max_length=48)]
TagValue = Annotated[StrictStr, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.-]*$", max_length=48)]


class SampleError(Exception):
    """样本目录不合法或读不出来。"""


class Sample(BaseModel):
    """一个样本：整个客户区的截图里的一个区域及其真值。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    image: Annotated[StrictStr, StringConstraints(min_length=1)]
    """相对 `samples.toml` 的 PNG 路径。"""
    category: Ident
    """类别：对白 dialogue、名字 name、选项 choice、审判发言 trial，或其它标识。"""
    roi: Ident | None = None
    """适配包里的 ROI 名；与 `rect` 恰好写一个。"""
    rect: tuple[int, int, int, int] | None = None
    """客户区物理像素的矩形 `[左, 上, 右, 下]`。"""
    text: StrictStr
    """真值；多行以换行分隔。"""
    tags: dict[Ident, TagValue] = Field(default_factory=dict[Ident, TagValue])
    """可选的标签（如字体 `font`、字号 `size`），评测另按每个标签的每个取值分组。"""

    @model_validator(mode="after")
    def _one_region(self) -> Self:
        if (self.roi is None) == (self.rect is None):
            raise ValueError("roi 与 rect 恰好写一个")
        return self


class SampleSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    game: Ident | None = None
    """适配包 `id`；有样本写 `roi` 时必需。"""
    samples: tuple[Sample, ...] = Field(min_length=1)


@dataclass(frozen=True)
class SampleFrame:
    """从 PNG 读出的一帧，满足 `game_runtime.ocr.FrameLike`。"""

    frame_number: int
    transform_version: int
    width: int
    height: int
    row_stride: int
    pixels: bytes


@dataclass(frozen=True)
class LoadedSample:
    index: int
    sample: Sample
    frame: SampleFrame
    rect: PixelRect


def read_png(path: Path, frame_number: int = 1) -> SampleFrame:
    """读 PNG（灰度、BGR 或 BGRA）为 BGRA8 的帧。路径可含非 ASCII 字符。"""
    try:
        data = np.fromfile(path, np.uint8)
    except OSError as error:
        raise SampleError(f"读不出 {path}：{error}") from None
    decoded = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if decoded is None:
        raise SampleError(f"{path} 不是能解码的图像")
    image = np.asarray(decoded, np.uint8)
    if image.ndim == 2:
        image = np.asarray(cv2.cvtColor(image, cv2.COLOR_GRAY2BGRA), np.uint8)
    elif image.shape[2] == 3:
        image = np.asarray(cv2.cvtColor(image, cv2.COLOR_BGR2BGRA), np.uint8)
    height, width = image.shape[:2]
    return SampleFrame(frame_number, 1, width, height, width * 4, image.tobytes())


def write_png(path: Path, bgra: bytes, width: int, height: int) -> None:
    image = np.frombuffer(bgra, np.uint8).reshape(height, width, 4)
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise SampleError(f"编码 {path} 失败")
    path.write_bytes(np.asarray(encoded, np.uint8).tobytes())


def read_sample_set(directory: Path) -> SampleSet:
    path = directory / SAMPLES_FILE
    try:
        data = tomllib.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        raise SampleError(f"{directory} 里没有 {SAMPLES_FILE}") from None
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SampleError(f"{path} 不是合法的 UTF-8 TOML：{error}") from None
    try:
        return SampleSet.model_validate(data)
    except ValueError as error:
        raise SampleError(f"{path} 不合法：{error}") from None


def load_samples(directory: Path, games_dir: Path = GAMES_DIR) -> list[LoadedSample]:
    """读样本目录：解码每张图，把 ROI 换算为这张图的物理像素矩形。"""
    sample_set = read_sample_set(directory)
    adapter: Adapter | None = None
    if any(sample.roi is not None for sample in sample_set.samples):
        if sample_set.game is None:
            raise SampleError("有样本写了 roi，须在顶层写 game")
        adapter = load_adapter(games_dir / sample_set.game)
    loaded: list[LoadedSample] = []
    for index, sample in enumerate(sample_set.samples):
        frame = read_png(directory / sample.image, index + 1)
        if sample.rect is not None:
            rect = PixelRect(*sample.rect)
        elif sample.roi is not None and adapter is not None:
            rect = adapter.roi(sample.roi).to_pixels(frame.width, frame.height)
        else:
            raise SampleError(f"第 {index + 1} 个样本既没有 rect 也没有可换算的 roi")
        left, top, right, bottom = rect
        if not (0 <= left < right <= frame.width and 0 <= top < bottom <= frame.height):
            raise SampleError(f"第 {index + 1} 个样本的区域 {tuple(rect)} 为空或超出图像")
        loaded.append(LoadedSample(index, sample, frame, rect))
    return loaded


def toml_string(value: str) -> str:
    """TOML 的基本字符串（JSON 的字符串转义是它的子集）。"""
    return json.dumps(value, ensure_ascii=False)


def write_sample_set(directory: Path, game: str | None, samples: Sequence[Sample]) -> Path:
    """写 `samples.toml`；图像由调用方写好。"""
    lines = ["# GS-04 的样本，格式见 docs/modules/perception.md 的“GS-04 的评测工具”。", ""]
    if game is not None:
        lines += [f"game = {toml_string(game)}", ""]
    for sample in samples:
        lines.append("[[samples]]")
        lines.append(f"image = {toml_string(sample.image)}")
        lines.append(f"category = {toml_string(sample.category)}")
        if sample.roi is not None:
            lines.append(f"roi = {toml_string(sample.roi)}")
        if sample.rect is not None:
            lines.append(f"rect = [{', '.join(str(v) for v in sample.rect)}]")
        lines.append(f"text = {toml_string(sample.text)}")
        if sample.tags:
            pairs = ", ".join(f"{key} = {toml_string(value)}" for key, value in sample.tags.items())
            lines.append(f"tags = {{ {pairs} }}")
        lines.append("")
    path = directory / SAMPLES_FILE
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return path
