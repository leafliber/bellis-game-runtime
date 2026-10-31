"""录制的目录布局、元数据与索引的格式，见 `docs/modules/offline-tools.md` 的“样本格式”。

一段录制一个目录：`<本地数据目录>/recordings/<适配包 id>/<开始时刻>[-<名字>]/`。这里只描述格式；
写入在 `writer`，读取在 `reader`。
"""

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

FORMAT_VERSION: Final = 1
"""样本格式的版本；读取方只接受认识的版本。"""

RECORDINGS_DIR: Final = "recordings"
"""本地数据目录里存放录制的子目录。"""
EXPORTS_DIR: Final = "exports"
"""本地数据目录里存放导出的子目录。"""

META_FILE: Final = "recording.json"
FRAMES_INDEX: Final = "frames.jsonl"
FRAMES_DIR: Final = "frames"
AUDIO_INDEX: Final = "audio.jsonl"
AUDIO_SAMPLES: Final = "audio.f32"
ANNOTATIONS_FILE: Final = "annotations.json"
LIVE_DIR: Final = "live"
"""录制时实时运行的插件输出。"""
REPLAY_DIR: Final = "replay"
"""最近一次回放的输出。"""

SAMPLE_FORMAT: Final = "f32le"

NAME_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{1,40}")
"""录制的名字。"""
DIR_PATTERN: Final = re.compile(r"\d{8}T\d{6}Z(?:-[A-Za-z0-9_-]+)?")
"""录制目录的名字：UTC 开始时刻，加可选的名字与同一秒里的序号。"""
_GAME = re.compile(r"[a-z][a-z0-9-]*")

type State = Literal["recording", "complete", "failed"]
type Encoding = Literal["png", "delta", "same"]

_U32 = 0xFFFF_FFFF
_U64 = 0xFFFF_FFFF_FFFF_FFFF
type _Count = Annotated[int, Field(ge=0)]
type _Nanos = Annotated[int, Field(ge=0, le=_U64)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def recordings_root(data_dir: Path, game: str) -> Path:
    """一款游戏的录制所在的目录。"""
    return data_dir / RECORDINGS_DIR / _game(game)


def exports_root(data_dir: Path, game: str, recording: str) -> Path:
    """一段录制的导出所在的目录。"""
    return data_dir / EXPORTS_DIR / _game(game) / recording


def _game(game: str) -> str:
    if _GAME.fullmatch(game) is None:
        raise ValueError(f"适配包 id 不合法：{game}")
    return game


def check_name(name: str) -> str:
    if NAME_PATTERN.fullmatch(name) is None:
        raise ValueError(f"录制的名字只能有字母、数字、- 与 _，至多 40 个：{name!r}")
    return name


def directory_name(started_at: datetime, name: str | None) -> str:
    """录制目录的名字（不含同一秒里的序号）。"""
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    return stamp if name is None else f"{stamp}-{check_name(name)}"


def frame_file(frame_number: int, encoding: Encoding) -> str:
    """像素文件相对录制目录的路径。"""
    suffix = "png" if encoding == "png" else "delta"
    return f"{FRAMES_DIR}/{frame_number:08d}.{suffix}"


# 索引


class FrameEntry(_Model):
    """帧索引的一行。"""

    frame_number: Annotated[int, Field(ge=1, le=_U64)]
    capture_ns: _Nanos
    transform_version: Annotated[int, Field(ge=0, le=_U64)]
    width: Annotated[int, Field(ge=1, le=_U32)]
    height: Annotated[int, Field(ge=1, le=_U32)]
    encoding: Encoding
    file: str | None
    """像素文件相对录制目录的路径；`same` 为 None。"""
    crc32: Annotated[int, Field(ge=0, le=_U32)]
    """解码出的像素（BGRA8，行间无填充）的 CRC-32。"""


class AudioEntry(_Model):
    """块索引的一行。"""

    block_number: Annotated[int, Field(ge=1, le=_U64)]
    capture_ns: _Nanos
    sample_count: Annotated[int, Field(ge=1, le=_U32)]
    discontinuity: bool
    offset: _Count
    """样本值在 `audio.f32` 里的字节偏移。"""


# 元数据


class Resolution(_Model):
    width: Annotated[int, Field(ge=1)]
    height: Annotated[int, Field(ge=1)]


class GameInfo(_Model):
    id: str
    name: str
    version: str
    adapter_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    """录制时 `adapter.toml` 的 SHA-256。"""
    resolution: Resolution
    """适配包的期望分辨率。"""


class ToolInfo(_Model):
    commit: str | None
    """本仓库的提交；取不到为 None。"""
    dirty: bool | None
    """工作树有无改动；取不到为 None。"""


class AdmissionInfo(_Model):
    max_rate: float
    idle_interval: float
    backlog: int


class FramesInfo(_Model):
    count: _Count = 0
    keyframes: _Count = 0
    bytes: _Count = 0
    """像素文件的字节数。"""
    first_capture_ns: _Nanos | None = None
    last_capture_ns: _Nanos | None = None


class AudioInfo(_Model):
    sample_rate: Annotated[int, Field(ge=1)]
    channels: Annotated[int, Field(ge=1)]
    sample_format: Literal["f32le"] = SAMPLE_FORMAT
    blocks: _Count = 0
    gaps: _Count = 0
    """块号的空缺数：录制落后于音频环，其间的块被覆盖。"""
    bytes: _Count = 0
    first_capture_ns: _Nanos | None = None
    last_capture_ns: _Nanos | None = None


class RecordingMeta(_Model):
    """`recording.json`。"""

    format: Literal[1] = FORMAT_VERSION
    state: State
    name: str | None
    started_at: datetime
    ended_at: datetime | None = None
    stop_reason: str | None = None
    game: GameInfo
    tool: ToolInfo
    admission: AdmissionInfo
    frames: FramesInfo = FramesInfo()
    audio: AudioInfo | None
    reader_overflows: _Count = 0
    """定序帧源积压超限的次数；不为 0 时实时结果与回放不一致。"""


def write_atomic(path: Path, data: bytes) -> None:
    """先写同目录的临时文件再改名，读者不会看到写了一半的文件。"""
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_meta(directory: Path, meta: RecordingMeta) -> None:
    write_atomic(directory / META_FILE, (meta.model_dump_json(indent=2) + "\n").encode())


def read_meta(directory: Path) -> RecordingMeta:
    return RecordingMeta.model_validate_json((directory / META_FILE).read_bytes())
