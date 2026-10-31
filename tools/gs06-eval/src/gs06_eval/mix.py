"""把纯配音与背景按增益离线混合，以及按音频环的样子把声音逐块喂给配音检测的核心。"""

import time
from dataclasses import dataclass, field

import numpy as np

from game_runtime.audio_ring import AudioBlock
from game_runtime.voice_activity import SpeechModel, VoiceActivityDetector, VoiceChange, VoiceParams
from gs06_eval.wav import Audio, Float32Array

BLOCK_SECONDS = 0.01
"""每块 10 ms，同实时核从 WASAPI 收到的包。"""


def _channels(samples: Float32Array, channels: int) -> Float32Array:
    if samples.shape[1] == channels:
        return samples
    if samples.shape[1] == 1:
        return np.repeat(samples, channels, axis=1)
    raise ValueError(f"声道数不同且都不是单声道：{samples.shape[1]} 与 {channels}")


def fit(audio: Audio, length: int) -> Float32Array:
    """循环或截断到 `length` 个采样点。"""
    if audio.length == 0:
        raise ValueError("背景是空的")
    repeats = -(-length // audio.length)
    return np.tile(audio.samples, (repeats, 1))[:length]


def mix(voice: Audio, background: Audio, gain_db: float) -> Audio:
    """配音加上按 `gain_db` 缩放的背景；背景比配音短时循环，长时截断。"""
    if voice.sample_rate != background.sample_rate:
        raise ValueError(
            f"采样率不同：配音 {voice.sample_rate} Hz，背景 {background.sample_rate} Hz"
        )
    channels = max(voice.channels, background.channels)
    gain = 10.0 ** (gain_db / 20.0)
    bed = _channels(fit(background, voice.length), channels) * np.float32(gain)
    total = _channels(voice.samples, channels) + bed
    return Audio(total.astype(np.float32), voice.sample_rate)


def rms_db(audio: Audio) -> float:
    """整段的均方根电平（dBFS）。"""
    power = float(np.mean(audio.mono() ** 2)) if audio.length else 0.0
    return 10.0 * float(np.log10(max(power, 1e-20)))


@dataclass
class Detection:
    """一段声音逐块喂给核心的结果。时刻以秒计，从这段声音的开头算起。"""

    changes: list[VoiceChange]
    duration: float
    probabilities: list[float] = field(default_factory=list[float])
    seconds: float = 0.0
    """处理的总耗时（墙钟）。"""

    @property
    def frames(self) -> int:
        return len(self.probabilities)


def blocks(audio: Audio) -> list[AudioBlock]:
    """按音频环的样子切块：每块 10 ms，块号从 1 起，采集时刻从 0 起按采样率推算。"""
    size = max(1, round(BLOCK_SECONDS * audio.sample_rate))
    result: list[AudioBlock] = []
    for number, start in enumerate(range(0, audio.length, size), start=1):
        chunk = np.ascontiguousarray(audio.samples[start : start + size], dtype=np.float32)
        result.append(
            AudioBlock(
                block_number=number,
                capture_ns=start * 1_000_000_000 // audio.sample_rate,
                sample_count=len(chunk),
                discontinuity=False,
                samples=chunk.tobytes(),
            )
        )
    return result


def detect(audio: Audio, model: SpeechModel, params: VoiceParams) -> Detection:
    """把声音逐块喂给一个新的核心（模型从零状态开始）。"""
    detection = Detection([], audio.duration)

    def observe(probability: float, start_ns: int, end_ns: int) -> None:
        del start_ns, end_ns
        detection.probabilities.append(probability)

    detector = VoiceActivityDetector(model, params, on_frame=observe)
    started = time.perf_counter()
    for block in blocks(audio):
        detection.changes.extend(
            detector.feed(block, sample_rate=audio.sample_rate, channels=audio.channels)
        )
    detection.seconds = time.perf_counter() - started
    return detection
