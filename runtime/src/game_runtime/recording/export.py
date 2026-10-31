"""把录制的帧导出成 PNG、把一段声音导出成 WAV，见 `docs/modules/offline-tools.md` 的“导出”。

导出到 `<本地数据目录>/exports/<适配包 id>/<录制目录名>/`。时间一律以距录制第一帧（没有帧时
为第一块）的采集时刻的秒数表示，帧与声音在同一时间轴上。
"""

import json
import math
import struct
import wave
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from game_runtime.recording.codec import encode_png
from game_runtime.recording.layout import write_atomic
from game_runtime.recording.reader import Recording, RecordingError
from game_runtime.scene import ChangeDetector, ChangeParams, Stability, bgra_view

WAVE_FORMAT_IEEE_FLOAT = 3


def origin_ns(recording: Recording) -> int:
    """时间轴的原点：第一帧的采集时刻，没有帧时为第一块的。"""
    if recording.frames:
        return recording.frames[0].capture_ns
    if recording.audio:
        return recording.audio[0].capture_ns
    raise RecordingError(f"{recording.path} 里没有帧也没有声音")


def _seconds(recording: Recording, capture_ns: int) -> float:
    return (capture_ns - origin_ns(recording)) / 1e9


# 帧


@dataclass(frozen=True, slots=True)
class FrameSelection:
    """选哪些帧导出；`frames`、`every`、`stable`、`all` 恰好给出一项。"""

    frames: tuple[int, ...] = ()
    """帧号。"""
    every: float | None = None
    """按采集时刻每隔几秒取一帧。"""
    stable: bool = False
    """每段稳定下来的第一帧。"""
    all: bool = False
    start: float | None = None
    """距第一帧的秒数，含。"""
    end: float | None = None
    """距第一帧的秒数，含。"""

    def __post_init__(self) -> None:
        chosen = sum((bool(self.frames), self.every is not None, self.stable, self.all))
        if chosen != 1:
            raise ValueError("frames、every、stable、all 须恰好给出一项")
        if self.every is not None and not (math.isfinite(self.every) and self.every > 0):
            raise ValueError(f"every 须为正数：{self.every}")


def select_frames(recording: Recording, selection: FrameSelection) -> list[int]:
    """按选法给出要导出的帧在接纳序列里的位置，从小到大。"""
    if selection.frames:
        try:
            chosen = sorted({recording.index_of(number) for number in selection.frames})
        except KeyError as error:
            raise RecordingError(str(error.args[0])) from None
        return [index for index in chosen if _in_range(recording, index, selection)]
    candidates = [i for i in range(len(recording.frames)) if _in_range(recording, i, selection)]
    if selection.all:
        return candidates
    if selection.every is not None:
        return _every(recording, candidates, selection.every)
    return list(_stable(recording, candidates))


def _in_range(recording: Recording, index: int, selection: FrameSelection) -> bool:
    seconds = _seconds(recording, recording.frames[index].capture_ns)
    if selection.start is not None and seconds < selection.start:
        return False
    return selection.end is None or seconds <= selection.end


def _every(recording: Recording, candidates: Sequence[int], every: float) -> list[int]:
    """从范围里的第一帧起，每过 `every` 秒取到达之后的第一帧；按整数纳秒算，不累积舍入误差。"""
    chosen: list[int] = []
    if not candidates:
        return chosen
    step = max(1, round(every * 1e9))
    base = recording.frames[candidates[0]].capture_ns
    threshold = base
    for index in candidates:
        capture = recording.frames[index].capture_ns
        if capture >= threshold:
            chosen.append(index)
            threshold = base + ((capture - base) // step + 1) * step
    return chosen


def _stable(recording: Recording, candidates: Sequence[int]) -> Iterator[int]:
    """依次判别变化与稳定（`scene@1` 的缺省参数），给出每段稳定下来的第一帧。"""
    detector = ChangeDetector(ChangeParams())
    stable = False
    for index in candidates:
        frame = recording.frame(index)
        change = detector.update(bgra_view(frame), frame.capture_ns)
        now = change.stability is Stability.STABLE
        if now and not stable:
            yield index
        stable = now


def export_frames(recording: Recording, directory: Path, indices: Sequence[int]) -> list[Path]:
    """把这些帧写成 `directory/frames/<帧号>.png`，另写 `frames.jsonl` 列出导出的帧。"""
    frames_dir = directory / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    lines: list[str] = []
    for index in indices:
        entry = recording.frames[index]
        frame = recording.frame(index)
        path = frames_dir / f"{entry.frame_number:08d}.png"
        write_atomic(path, encode_png(frame.pixels, frame.width, frame.height))
        written.append(path)
        record = {
            "frame_number": entry.frame_number,
            "capture_ns": entry.capture_ns,
            "seconds": round(_seconds(recording, entry.capture_ns), 6),
            "file": f"frames/{path.name}",
        }
        lines.append(json.dumps(record, ensure_ascii=False))
    write_atomic(directory / "frames.jsonl", "".join(f"{line}\n" for line in lines).encode())
    return written


# 声音


@dataclass(frozen=True, slots=True)
class AudioExport:
    path: Path
    samples: int
    """每声道的采样点数，含补的零。"""
    first_capture_ns: int
    inserted: tuple[tuple[int, int], ...]
    """补零的位置：（从第几个采样点起，补了几个）。"""


def export_audio(
    recording: Recording,
    directory: Path,
    *,
    start: float | None = None,
    end: float | None = None,
    pcm16: bool = False,
) -> AudioExport:
    """把与 [`start`, `end`]（距第一帧的秒数）有重叠的块写成 WAV；时间轴断开处按时差补零。"""
    audio = recording.meta.audio
    if audio is None or not recording.audio:
        raise RecordingError(f"{recording.path} 里没有声音")
    rate, channels = audio.sample_rate, audio.channels
    origin = origin_ns(recording)
    start_ns = None if start is None else origin + round(start * 1e9)
    end_ns = None if end is None else origin + round(end * 1e9)
    chosen = [
        index
        for index, entry in enumerate(recording.audio)
        if (end_ns is None or entry.capture_ns <= end_ns)
        and (
            start_ns is None or entry.capture_ns + _duration_ns(entry.sample_count, rate) > start_ns
        )
    ]
    if not chosen:
        raise RecordingError("这段时间里没有声音")
    blocks = recording.blocks(chosen[0], chosen[-1] + 1)
    parts: list[bytes] = []
    inserted: list[tuple[int, int]] = []
    samples = 0
    previous_end: int | None = None
    previous_number: int | None = None
    for block in blocks:
        broken = block.discontinuity or (
            previous_number is not None and block.block_number != previous_number + 1
        )
        if previous_end is not None and broken and block.capture_ns > previous_end:
            missing = round((block.capture_ns - previous_end) * rate / 1e9)
            if missing > 0:
                parts.append(bytes(missing * channels * 4))
                inserted.append((samples, missing))
                samples += missing
        parts.append(block.samples)
        samples += block.sample_count
        previous_end = block.capture_ns + _duration_ns(block.sample_count, rate)
        previous_number = block.block_number
    data = b"".join(parts)
    first = blocks[0].capture_ns
    begin_ms = round((first - origin) / 1e6)
    end_ms = round(((previous_end or first) - origin) / 1e6)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"audio-{begin_ms}-{end_ms}ms.wav"
    if pcm16:
        _write_pcm16(path, data, rate, channels)
    else:
        write_atomic(path, _float_wav(data, rate, channels))
    sidecar: dict[str, Any] = {
        "first_capture_ns": first,
        "origin_capture_ns": origin,
        "sample_rate": rate,
        "channels": channels,
        "format": "pcm16" if pcm16 else "f32",
        "samples": samples,
        "inserted": [{"at_sample": at, "samples": count} for at, count in inserted],
    }
    write_atomic(path.with_suffix(".json"), (json.dumps(sidecar, indent=2) + "\n").encode())
    return AudioExport(path, samples, first, tuple(inserted))


def _duration_ns(sample_count: int, rate: int) -> int:
    return sample_count * 1_000_000_000 // rate


def _float_wav(data: bytes, rate: int, channels: int) -> bytes:
    """32 位浮点的 WAV：fmt 块的格式码为 3，另有 fact 块写每声道的采样点数。"""
    block_align = channels * 4
    frames = len(data) // block_align
    fmt = struct.pack(
        "<HHIIHHH", WAVE_FORMAT_IEEE_FLOAT, channels, rate, rate * block_align, block_align, 32, 0
    )
    chunks = b"".join(
        (
            b"fmt " + struct.pack("<I", len(fmt)) + fmt,
            b"fact" + struct.pack("<II", 4, frames),
            b"data" + struct.pack("<I", len(data)) + data,
        )
    )
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def _write_pcm16(path: Path, data: bytes, rate: int, channels: int) -> None:
    values = np.frombuffer(data, "<f4")
    scaled = np.clip(np.rint(np.clip(values, -1.0, 1.0) * 32767.0), -32768, 32767)
    pcm = scaled.astype("<i2").tobytes()
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with wave.open(str(temporary), "wb") as file:
            file.setnchannels(channels)
            file.setsampwidth(2)
            file.setframerate(rate)
            file.writeframes(pcm)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
