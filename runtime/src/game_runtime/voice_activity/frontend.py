"""配音检测的预处理：混成单声道，流式重采样到模型的采样率，按模型的帧长分帧，并推算每帧的采集时刻。

设计见 `docs/modules/perception.md` 的“配音检测”。结果只取决于依次喂入的块，不读墙钟。
"""

import math
from dataclasses import dataclass
from typing import Final

import numpy as np
from numpy.typing import NDArray

from game_runtime.audio_ring import AudioBlock

ZERO_CROSSINGS: Final = 16
"""重采样核每侧的 sinc 过零点数。"""
ROLLOFF: Final = 0.9
"""低通的截止频率占两边较低的奈奎斯特频率的比例。"""
KAISER_BETA: Final = 8.0
"""Kaiser 窗的 β。"""

type Float32Array = NDArray[np.float32]
type Float64Array = NDArray[np.float64]


def _kaiser(x: Float64Array) -> Float64Array:
    """Kaiser 窗在 x ∈ [−1, 1] 上的取值，之外为 0。"""
    inside = np.clip(1.0 - x * x, 0.0, None)
    window = np.i0(KAISER_BETA * np.sqrt(inside)) / np.i0(KAISER_BETA)
    return np.where(np.abs(x) <= 1.0, window, 0.0)


class Resampler:
    """有理数比的流式多相重采样（采样率不同时用）。

    约分后输出与输入的采样率之比为 L/M，第 k 个输出采样点在输入的位置 k·M/L 上，取它两侧各 `half`
    个输入采样点，按 Kaiser 窗的 sinc 低通核加权；每个相位的权重归一化到和为 1。
    段的开头之前按零填充。
    第 k 个输出要等到输入的位置 ⌊k·M/L⌋ + `half` 到了才算出。
    """

    def __init__(self, rate_in: int, rate_out: int) -> None:
        if rate_in < 1 or rate_out < 1:
            raise ValueError(f"采样率须为正数：{rate_in}、{rate_out}")
        divisor = math.gcd(rate_in, rate_out)
        self.up = rate_out // divisor
        self.down = rate_in // divisor
        cutoff = ROLLOFF * min(rate_in, rate_out) / 2 / rate_in
        """截止频率，单位为每个输入采样点的周数。"""
        self.half = math.ceil(ZERO_CROSSINGS / (2 * cutoff))
        self._offsets = np.arange(-self.half + 1, self.half + 1, dtype=np.int64)
        phases = np.arange(self.up, dtype=np.float64)[:, None] / self.up
        tau = self._offsets[None, :].astype(np.float64) - phases
        kernel = 2 * cutoff * np.sinc(2 * cutoff * tau) * _kaiser(tau / self.half)
        self._table: Float64Array = kernel / kernel.sum(axis=1, keepdims=True)
        self._buffer: Float64Array = np.zeros(self.half, dtype=np.float64)
        self._start = -self.half
        """`_buffer[0]` 的输入位置。"""
        self._next = 0
        """下一个输出采样点的序号。"""

    def push(self, samples: Float64Array) -> Float64Array:
        """喂入一段输入，返回因此能算出的输出。"""
        buffer = np.concatenate((self._buffer, samples))
        end = self._start + len(buffer)
        last = ((end - self.half) * self.up - 1) // self.down
        if last < self._next:
            self._buffer = buffer
            return np.zeros(0, dtype=np.float64)
        positions = np.arange(self._next, last + 1, dtype=np.int64) * self.down
        base = positions // self.up
        index = (base - self._start)[:, None] + self._offsets[None, :]
        out: Float64Array = np.einsum("ij,ij->i", buffer[index], self._table[positions % self.up])
        self._next = last + 1
        keep = (self._next * self.down) // self.up - self.half + 1
        self._buffer = buffer[keep - self._start :]
        self._start = keep
        return out


@dataclass(frozen=True, slots=True)
class Frame:
    """交给模型的一帧：模型采样率下的单声道样本值与采集时刻。"""

    samples: Float32Array
    start_ns: int
    """第一个采样点的采集时刻。"""
    end_ns: int
    """帧末尾（下一帧第一个采样点）的采集时刻。"""


class Frontend:
    """一段连续声音的预处理：块进，帧出。格式在一段之内不变，断开时换一个新的。"""

    def __init__(self, sample_rate: int, channels: int, *, rate: int, frame_size: int) -> None:
        if channels < 1:
            raise ValueError(f"声道数须为正数：{channels}")
        if frame_size < 1:
            raise ValueError(f"帧长须为正数：{frame_size}")
        self.sample_rate = sample_rate
        self.channels = channels
        self._rate = rate
        self._frame_size = frame_size
        self._resampler = None if sample_rate == rate else Resampler(sample_rate, rate)
        self._pending: Float32Array = np.zeros(0, dtype=np.float32)
        self._frames = 0
        """本段已交出的帧数。"""
        self._received = 0
        """本段已收到的输入采样点数。"""
        self._anchors: list[tuple[int, int]] = []
        """各块第一个采样点的输入位置与采集时刻，用来推算帧的采集时刻。"""

    def push(self, block: AudioBlock) -> list[Frame]:
        """喂入一块，返回因此凑齐的帧。"""
        values = np.frombuffer(block.samples, dtype=np.float32)
        if block.sample_count < 1 or len(values) != block.sample_count * self.channels:
            raise ValueError(
                f"块 {block.block_number} 有 {len(values)} 个样本值，"
                f"不是 {block.sample_count} × {self.channels}"
            )
        mono = values.reshape(-1, self.channels).mean(axis=1, dtype=np.float64)
        mono = np.nan_to_num(mono, nan=0.0, posinf=0.0, neginf=0.0)
        self._anchors.append((self._received, block.capture_ns))
        self._received += block.sample_count
        out = mono if self._resampler is None else self._resampler.push(mono)
        pending = np.concatenate((self._pending, out.astype(np.float32)))
        size = self._frame_size
        count = len(pending) // size
        frames: list[Frame] = []
        for i in range(count):
            first = (self._frames + i) * size
            frames.append(
                Frame(
                    np.ascontiguousarray(pending[i * size : (i + 1) * size]),
                    self._time(first),
                    self._time(first + size),
                )
            )
        self._frames += count
        self._pending = pending[count * size :]
        self._prune()
        return frames

    def _time(self, k: int) -> int:
        """模型采样率下第 k 个采样点（在输入的位置 k × 输入采样率 / 模型采样率）的采集时刻。"""
        scaled = k * self.sample_rate
        """位置乘以模型采样率。"""
        start, ns = self._anchors[0]
        for anchor in reversed(self._anchors):
            if anchor[0] * self._rate <= scaled:
                start, ns = anchor
                break
        return ns + (scaled - start * self._rate) * 1_000_000_000 // (self._rate * self.sample_rate)

    def _prune(self) -> None:
        """丢掉推算下一帧开头已用不到的锚点。"""
        scaled = self._frames * self._frame_size * self.sample_rate
        while len(self._anchors) >= 2 and self._anchors[1][0] * self._rate <= scaled:
            del self._anchors[0]
