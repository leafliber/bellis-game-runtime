"""WAV 的读写：PCM 8、16、24、32 位整数与 32、64 位浮点，含 `WAVE_FORMAT_EXTENSIBLE`。

读出的样本值是 [-1, 1] 上的 float32（浮点 WAV 原样保留），形状为（采样点数, 声道数）。
写只写 32 位浮点与 16 位整数两种。
"""

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import numpy as np
from numpy.typing import NDArray

type Float32Array = NDArray[np.float32]

FORMAT_PCM: Final = 0x0001
FORMAT_FLOAT: Final = 0x0003
FORMAT_EXTENSIBLE: Final = 0xFFFE


class WavError(ValueError):
    """不是能读的 WAV。"""


@dataclass(frozen=True)
class Audio:
    """一段声音：形状为（采样点数, 声道数）的 float32 与采样率。"""

    samples: Float32Array
    sample_rate: int

    def __post_init__(self) -> None:
        if self.samples.ndim != 2 or self.samples.shape[1] < 1:
            raise ValueError(f"样本值的形状须为（采样点数, 声道数）：{self.samples.shape}")
        if self.sample_rate < 1:
            raise ValueError(f"采样率须为正数：{self.sample_rate}")

    @property
    def channels(self) -> int:
        return int(self.samples.shape[1])

    @property
    def length(self) -> int:
        """采样点数。"""
        return int(self.samples.shape[0])

    @property
    def duration(self) -> float:
        """时长（秒）。"""
        return self.length / self.sample_rate

    def mono(self) -> NDArray[np.float64]:
        """各声道取平均。"""
        return self.samples.mean(axis=1, dtype=np.float64)


def _chunks(data: bytes) -> dict[bytes, bytes]:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise WavError("不是 RIFF/WAVE 文件")
    chunks: dict[bytes, bytes] = {}
    offset = 12
    while offset + 8 <= len(data):
        name = data[offset : offset + 4]
        (size,) = struct.unpack_from("<I", data, offset + 4)
        body = data[offset + 8 : offset + 8 + size]
        if len(body) < size and name != b"data":
            raise WavError(f"块 {name!r} 被截断")
        chunks.setdefault(name, body)
        offset += 8 + size + (size & 1)
    return chunks


def decode_wav(data: bytes) -> Audio:
    """解开 WAV 文件的字节。"""
    chunks = _chunks(data)
    fmt = chunks.get(b"fmt ")
    body = chunks.get(b"data")
    if fmt is None or len(fmt) < 16:
        raise WavError("没有 fmt 块")
    if body is None:
        raise WavError("没有 data 块")
    tag, channels, rate, _, align, bits = struct.unpack_from("<HHIIHH", fmt)
    if tag == FORMAT_EXTENSIBLE:
        if len(fmt) < 40:
            raise WavError("WAVE_FORMAT_EXTENSIBLE 的 fmt 块太短")
        (tag,) = struct.unpack_from("<H", fmt, 24)
    if channels < 1 or rate < 1:
        raise WavError(f"声道数或采样率不合法：{channels}、{rate}")
    width = bits // 8
    if bits % 8 or align != width * channels:
        raise WavError(f"不支持的位深与块对齐：{bits} 位，{align} 字节")
    count = len(body) // align
    raw = body[: count * align]
    values: NDArray[np.float64] | Float32Array
    if tag == FORMAT_PCM and bits == 8:
        values = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif tag == FORMAT_PCM and bits == 16:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif tag == FORMAT_PCM and bits == 24:
        triples = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        joined = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        signed = np.where(joined >= 1 << 23, joined - (1 << 24), joined)
        values = signed.astype(np.float64) / float(1 << 23)
    elif tag == FORMAT_PCM and bits == 32:
        values = np.frombuffer(raw, dtype="<i4").astype(np.float64) / float(1 << 31)
    elif tag == FORMAT_FLOAT and bits == 32:
        values = np.frombuffer(raw, dtype="<f4")
    elif tag == FORMAT_FLOAT and bits == 64:
        values = np.frombuffer(raw, dtype="<f8")
    else:
        raise WavError(f"不支持的格式：格式码 {tag:#06x}，{bits} 位")
    samples = np.ascontiguousarray(values.astype(np.float32).reshape(count, channels))
    return Audio(samples, rate)


def read_wav(path: Path) -> Audio:
    return decode_wav(path.read_bytes())


def encode_wav(audio: Audio, *, sample_format: Literal["float32", "pcm16"] = "float32") -> bytes:
    """编码成 WAV 的字节：32 位浮点或 16 位整数（截到 [-1, 1]）。"""
    if sample_format == "float32":
        tag, bits = FORMAT_FLOAT, 32
        payload = audio.samples.astype("<f4").tobytes()
    else:
        tag, bits = FORMAT_PCM, 16
        clipped = np.clip(audio.samples, -1.0, 1.0) * 32767.0
        payload = np.round(clipped).astype("<i2").tobytes()
    align = audio.channels * bits // 8
    fmt = struct.pack(
        "<HHIIHH", tag, audio.channels, audio.sample_rate, audio.sample_rate * align, align, bits
    )
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", len(payload)) + payload
    if len(payload) & 1:
        chunks += b"\0"
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def write_wav(
    path: Path, audio: Audio, *, sample_format: Literal["float32", "pcm16"] = "float32"
) -> None:
    path.write_bytes(encode_wav(audio, sample_format=sample_format))
