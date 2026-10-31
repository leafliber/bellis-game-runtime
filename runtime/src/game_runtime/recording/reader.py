"""读一段录制：元数据、帧索引与块索引，逐帧解码像素，读块的样本值。

格式见 `docs/modules/offline-tools.md` 的“样本格式”。`Recording` 可以在多个线程里同时解码：解码与读
样本值都在一把锁里进行，最近解出的几帧留在缓存里，依次读时每帧只解一次差分。
"""

import bisect
import threading
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Self

from pydantic import BaseModel, ValidationError

from game_runtime.audio_ring import AudioBlock
from game_runtime.frame_ring import RingFrame
from game_runtime.frame_ring.ring import BYTES_PER_PIXEL
from game_runtime.recording.codec import CodecError, apply_delta, crc32, decode_png
from game_runtime.recording.layout import (
    AUDIO_INDEX,
    AUDIO_SAMPLES,
    FRAMES_INDEX,
    META_FILE,
    AudioEntry,
    FrameEntry,
    RecordingMeta,
)

CACHE_FRAMES: Final = 4
"""解出的像素留在缓存里的帧数。"""


class RecordingError(Exception):
    """录制读不出来、不完整或与索引不符。"""


def _lines[T: BaseModel](path: Path, model: type[T]) -> list[T]:
    """读 JSON Lines；异常中断的录制最后一行可能只写了一半（没有换行），丢掉它。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    lines = text.split("\n")
    lines.pop()  # 最后一个换行之后的部分：完整的文件里是空串，写了一半时是残行
    entries: list[T] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entries.append(model.model_validate_json(line))
        except ValidationError as error:
            raise RecordingError(f"{path} 第 {number} 行不合格式：{error}") from None
    return entries


class Recording:
    """打开的一段录制。用 `open` 打开，`frame(i)` 解出第 i 帧（按接纳顺序，从 0 起）。"""

    def __init__(
        self,
        path: Path,
        meta: RecordingMeta,
        frames: Sequence[FrameEntry],
        audio: Sequence[AudioEntry],
    ) -> None:
        self.path = path
        self.meta = meta
        self.frames = tuple(frames)
        self.audio = tuple(audio)
        self.frame_numbers = tuple(entry.frame_number for entry in self.frames)
        self.block_numbers = tuple(entry.block_number for entry in self.audio)
        self._lock = threading.Lock()
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._check()

    @classmethod
    def open(cls, path: Path) -> Self:
        """读元数据与两个索引并核对；不完整的录制也能打开（`complete` 为假）。"""
        try:
            meta = RecordingMeta.model_validate_json((path / META_FILE).read_bytes())
        except FileNotFoundError:
            raise RecordingError(f"{path} 没有 {META_FILE}，不是录制") from None
        except ValidationError as error:
            raise RecordingError(f"{path / META_FILE} 不合格式：{error}") from None
        frames = _lines(path / FRAMES_INDEX, FrameEntry)
        audio = _lines(path / AUDIO_INDEX, AudioEntry) if meta.audio is not None else []
        return cls(path, meta, frames, audio)

    @property
    def complete(self) -> bool:
        return self.meta.state == "complete"

    @property
    def name(self) -> str:
        """录制目录的名字。"""
        return self.path.name

    def _check(self) -> None:
        if self.complete:
            audio = self.meta.audio
            if len(self.frames) != self.meta.frames.count or (
                audio is not None and len(self.audio) != audio.blocks
            ):
                raise RecordingError(f"{self.path} 的索引与元数据记的帧数或块数不符")
        previous: FrameEntry | None = None
        for index, entry in enumerate(self.frames):
            where = f"{self.path / FRAMES_INDEX} 第 {index + 1} 帧"
            if previous is not None and entry.frame_number <= previous.frame_number:
                raise RecordingError(f"{where}：帧号 {entry.frame_number} 没有递增")
            if entry.encoding == "png" or entry.encoding == "delta":
                if entry.file is None:
                    raise RecordingError(f"{where}：{entry.encoding} 没有文件")
            elif entry.file is not None:
                raise RecordingError(f"{where}：same 不应有文件")
            if entry.encoding != "png" and (
                previous is None or (previous.width, previous.height) != (entry.width, entry.height)
            ):
                raise RecordingError(f"{where}：{entry.encoding} 前面没有同尺寸的帧")
            previous = entry
        offset = 0
        for index, block in enumerate(self.audio):
            if index and block.block_number <= self.audio[index - 1].block_number:
                raise RecordingError(f"{self.path / AUDIO_INDEX} 第 {index + 1} 块：块号没有递增")
            if block.offset != offset:
                raise RecordingError(f"{self.path / AUDIO_INDEX} 第 {index + 1} 块：偏移不连续")
            offset += block.sample_count * self._bytes_per_sample

    @property
    def _bytes_per_sample(self) -> int:
        audio = self.meta.audio
        return 0 if audio is None else audio.channels * 4

    # 帧

    def frame(self, index: int) -> RingFrame:
        """解出第 `index` 帧，校验 CRC；行跨度为宽 × 4。"""
        entry = self.frames[index]
        with self._lock:
            pixels = self._pixels(index)
        return RingFrame(
            frame_number=entry.frame_number,
            capture_ns=entry.capture_ns,
            transform_version=entry.transform_version,
            width=entry.width,
            height=entry.height,
            row_stride=entry.width * BYTES_PER_PIXEL,
            pixels=pixels,
        )

    def _pixels(self, index: int) -> bytes:
        cached = self._cache.get(index)
        if cached is not None:
            self._cache.move_to_end(index)
            return cached
        start = index
        while start not in self._cache and self.frames[start].encoding != "png":
            start -= 1
        pixels = self._cache.get(start)
        if pixels is None:
            pixels = self._decode(start, None)
        for step in range(start + 1, index + 1):
            pixels = self._decode(step, pixels)
        if crc32(pixels) != self.frames[index].crc32:
            raise RecordingError(f"{self.path} 第 {index + 1} 帧解出的像素与 CRC 不符")
        self._cache[index] = pixels
        while len(self._cache) > CACHE_FRAMES:
            self._cache.popitem(last=False)
        return pixels

    def _decode(self, index: int, previous: bytes | None) -> bytes:
        entry = self.frames[index]
        if entry.encoding == "same":
            if previous is None:
                raise RecordingError(f"{self.path} 第 {index + 1} 帧：same 前面没有帧")
            return previous
        if entry.file is None:
            raise RecordingError(f"{self.path} 第 {index + 1} 帧：{entry.encoding} 没有文件")
        try:
            data = (self.path / entry.file).read_bytes()
        except OSError as error:
            raise RecordingError(f"读不出 {self.path / entry.file}：{error}") from None
        try:
            if entry.encoding == "png":
                return decode_png(data, entry.width, entry.height)
            if previous is None:
                raise RecordingError(f"{self.path} 第 {index + 1} 帧：delta 前面没有帧")
            return apply_delta(data, previous)
        except CodecError as error:
            raise RecordingError(f"{self.path / entry.file}：{error}") from None

    def index_of(self, frame_number: int) -> int:
        """帧号在接纳序列里的位置；不在索引里即 KeyError。"""
        index = bisect.bisect_left(self.frame_numbers, frame_number)
        if index == len(self.frame_numbers) or self.frame_numbers[index] != frame_number:
            raise KeyError(f"帧 {frame_number} 不在帧索引里")
        return index

    # 声音

    def blocks(self, start: int, stop: int) -> list[AudioBlock]:
        """读第 `start` 到 `stop`（不含）块的样本值。"""
        entries = self.audio[start:stop]
        if not entries:
            return []
        first = entries[0].offset
        last = entries[-1]
        end = last.offset + last.sample_count * self._bytes_per_sample
        with self._lock:
            try:
                with (self.path / AUDIO_SAMPLES).open("rb") as file:
                    file.seek(first)
                    data = file.read(end - first)
            except OSError as error:
                raise RecordingError(f"读不出 {self.path / AUDIO_SAMPLES}：{error}") from None
        if len(data) != end - first:
            raise RecordingError(f"{self.path / AUDIO_SAMPLES} 比块索引短")
        blocks: list[AudioBlock] = []
        for entry in entries:
            begin = entry.offset - first
            length = entry.sample_count * self._bytes_per_sample
            blocks.append(
                AudioBlock(
                    block_number=entry.block_number,
                    capture_ns=entry.capture_ns,
                    sample_count=entry.sample_count,
                    discontinuity=entry.discontinuity,
                    samples=data[begin : begin + length],
                )
            )
        return blocks

    def audio_end_ns(self) -> int | None:
        """最后一块的结束时刻（第一个采样点的采集时刻加时长），没有块为 None。"""
        audio = self.meta.audio
        if audio is None or not self.audio:
            return None
        last = self.audio[-1]
        return last.capture_ns + last.sample_count * 1_000_000_000 // audio.sample_rate
