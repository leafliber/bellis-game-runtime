"""自测用的合成样本：SAPI 合成的原创台词作纯配音，合成的和弦与旋律作“背景音乐”，短促的敲击、
铃声与噪声作“音效”。不用任何游戏音频；结果只能检验工具与模型，不能代替 GS-06。
"""

import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from gs06_eval import sapi
from gs06_eval.wav import Audio, read_wav

LINES = (
    "今天的风有点大，我们早点回去吧。",
    "你看，钟楼上的灯又亮了。",
    "这件事我想了很久，还是决定告诉你。",
    "好的。",
    "如果明天下雨，我们就改在图书馆见面，记得带上那本蓝色的笔记本。",
    "嗯，我知道了，谢谢你。",
    "走廊尽头的那扇门，好像一直没有人打开过。",
    "别担心，一切都会好起来的。",
)
"""为自测原创的台词。"""
GAPS = (1.2, 0.8, 1.5, 0.9, 2.0, 1.0, 1.3, 1.6)
"""每句之前的静音（秒）。"""
TAIL = 1.5
"""最后一句之后的静音（秒）。"""


type Float64Array = NDArray[np.float64]


@dataclass(frozen=True)
class VoiceTrack:
    audio: Audio
    voice: str
    """所用语音的名字。"""


def voice_track(sample_rate: int = 48_000) -> VoiceTrack:
    """逐句合成台词，按 `GAPS` 插入静音后接成一条单声道的纯配音。要 SAPI。"""
    with tempfile.TemporaryDirectory(prefix="gs06-voice-") as directory:
        paths = [Path(directory) / f"line-{i}.wav" for i in range(len(LINES))]
        name = sapi.synthesize(list(zip(LINES, paths, strict=True)), sample_rate=sample_rate)
        pieces: list[Float64Array] = []
        for gap, path in zip(GAPS, paths, strict=True):
            pieces.append(np.zeros(round(gap * sample_rate)))
            line = read_wav(path)
            if line.sample_rate != sample_rate:
                raise sapi.SapiError(f"SAPI 给出 {line.sample_rate} Hz，要的是 {sample_rate} Hz")
            pieces.append(line.mono())
        pieces.append(np.zeros(round(TAIL * sample_rate)))
    samples = np.concatenate(pieces).astype(np.float32)[:, None]
    return VoiceTrack(Audio(samples, sample_rate), name)


def _envelope(length: int, sample_rate: int, attack: float, decay: float) -> Float64Array:
    t = np.arange(length) / sample_rate
    return np.minimum(1.0, t / attack) * np.exp(-t / decay)


def music(duration: float, sample_rate: int = 48_000, *, seed: int = 0) -> Audio:
    """音乐样的背景：每拍 0.5 秒，四拍一个和弦（带谐波的正弦），上面一条随机的旋律，立体声。"""
    rng = np.random.default_rng(seed)
    length = round(duration * sample_rate)
    out = np.zeros((length, 2))
    beat = round(0.5 * sample_rate)
    chords = ((220.0, 277.18, 329.63), (196.0, 246.94, 293.66), (174.61, 220.0, 261.63))
    scale = (440.0, 493.88, 554.37, 587.33, 659.25, 739.99)
    harmonics = (1.0, 0.5, 0.25, 0.12)
    for index, start in enumerate(range(0, length, beat)):
        span = min(beat * 2, length - start)
        t = np.arange(span) / sample_rate
        if index % 4 == 0:
            chord_span = min(beat * 4, length - start)
            tc = np.arange(chord_span) / sample_rate
            chord = sum(
                a * np.sin(2 * np.pi * f * k * tc)
                for f in chords[(index // 4) % len(chords)]
                for k, a in enumerate(harmonics, start=1)
            )
            out[start : start + chord_span] += (
                0.04 * chord * _envelope(chord_span, sample_rate, 0.05, 2.0)
            )[:, None] * [1.0, 0.8]
        note = scale[int(rng.integers(len(scale)))]
        tone = sum(a * np.sin(2 * np.pi * note * k * t) for k, a in enumerate(harmonics, start=1))
        out[start : start + span] += (0.06 * tone * _envelope(span, sample_rate, 0.01, 0.35))[
            :, None
        ] * [0.8, 1.0]
    return Audio(out.astype(np.float32), sample_rate)


def effects(duration: float, sample_rate: int = 48_000, *, seed: int = 1) -> Audio:
    """音效样的背景：随机时刻的敲击（衰减的噪声）、铃声（衰减的高频正弦）与风声（缓起缓落的噪声）。"""
    rng = np.random.default_rng(seed)
    length = round(duration * sample_rate)
    out = np.zeros(length)
    at = 0.3
    while at < duration:
        start = round(at * sample_rate)
        kind = int(rng.integers(3))
        if kind == 0:
            span = min(round(0.08 * sample_rate), length - start)
            burst = rng.standard_normal(span) * _envelope(span, sample_rate, 0.001, 0.015)
            out[start : start + span] += 0.3 * burst
        elif kind == 1:
            span = min(round(0.6 * sample_rate), length - start)
            t = np.arange(span) / sample_rate
            freq = float(rng.uniform(1500, 3000))
            ring = np.sin(2 * np.pi * freq * t) + 0.4 * np.sin(2 * np.pi * freq * 2.76 * t)
            out[start : start + span] += 0.12 * ring * _envelope(span, sample_rate, 0.002, 0.15)
        else:
            span = min(round(1.2 * sample_rate), length - start)
            noise = np.convolve(rng.standard_normal(span), np.ones(16) / 16, mode="same")
            swell = np.sin(np.pi * np.arange(span) / max(1, span)) ** 2
            out[start : start + span] += 0.15 * noise * swell
        at += float(rng.uniform(0.7, 2.5))
    return Audio(np.stack([out, out], axis=1).astype(np.float32), sample_rate)
