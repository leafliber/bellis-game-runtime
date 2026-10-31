"""从纯配音的录音按能量得出配音段的真值。

混成单声道，每 `frame` 秒一帧算均方根电平（dBFS）；高于阈值的帧为有声，阈值为第 `percentile`
百分位的电平减 `relative_db`、但不低于 `floor_db`；间隔短于 `merge_gap` 的有声段合并，短于
`min_segment` 的段丢掉。纯配音的录音里配音之外是静音，所以按能量就能定出配音段。
"""

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from gs06_eval.wav import Audio

SILENCE_DB = -200.0
"""全零的帧的电平。"""


@dataclass(frozen=True)
class Segment:
    """时间轴上的一段 [start, end)，以秒计，从录音的开头算起。"""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class TruthParams:
    frame: float = 0.01
    """帧长（秒）。"""
    percentile: float = 95.0
    relative_db: float = 30.0
    floor_db: float = -60.0
    merge_gap: float = 0.3
    """秒。"""
    min_segment: float = 0.1
    """秒。"""

    def __post_init__(self) -> None:
        if not self.frame > 0 or not 0 <= self.percentile <= 100:
            raise ValueError(f"帧长或百分位不合法：{self.frame}、{self.percentile}")
        if self.relative_db < 0 or self.merge_gap < 0 or self.min_segment < 0:
            raise ValueError("relative_db、merge_gap 与 min_segment 不能为负")


@dataclass(frozen=True)
class Truth:
    segments: tuple[Segment, ...]
    threshold_db: float
    """所用的阈值。"""
    params: TruthParams


def frame_levels(audio: Audio, frame: float) -> NDArray[np.float64]:
    """每帧的均方根电平（dBFS），最后不满一帧的部分不计。"""
    hop = max(1, round(frame * audio.sample_rate))
    mono = audio.mono()
    count = len(mono) // hop
    power = np.mean(mono[: count * hop].reshape(count, hop) ** 2, axis=1)
    floor = 10.0 ** (SILENCE_DB / 10.0)
    return 10.0 * np.log10(np.maximum(power, floor))


def merge(segments: list[Segment], gap: float) -> list[Segment]:
    """合并间隔短于 `gap` 的相邻段。"""
    merged: list[Segment] = []
    for segment in segments:
        if merged and segment.start - merged[-1].end < gap:
            merged[-1] = Segment(merged[-1].start, segment.end)
        else:
            merged.append(segment)
    return merged


def voice_truth(audio: Audio, params: TruthParams | None = None) -> Truth:
    params = params or TruthParams()
    hop = max(1, round(params.frame * audio.sample_rate))
    seconds = hop / audio.sample_rate
    levels = frame_levels(audio, params.frame)
    if len(levels) == 0:
        return Truth((), params.floor_db, params)
    threshold = max(
        params.floor_db, float(np.percentile(levels, params.percentile)) - params.relative_db
    )
    active = levels >= threshold
    segments: list[Segment] = []
    start: int | None = None
    for index, on in enumerate([*active.tolist(), False]):
        if on and start is None:
            start = index
        elif not on and start is not None:
            segments.append(Segment(start * seconds, index * seconds))
            start = None
    segments = merge(segments, params.merge_gap)
    kept = tuple(s for s in segments if s.duration >= params.min_segment - 1e-9)
    return Truth(kept, threshold, params)
