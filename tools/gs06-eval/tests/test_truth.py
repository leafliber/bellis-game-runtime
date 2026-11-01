"""按能量得出真值、电平与相对增益、离线混合：合成的正弦段，真值已知。"""

import numpy as np
import pytest
from numpy.typing import NDArray

from gs06_eval.mix import (
    background_level_db,
    blocks,
    fit,
    fitted,
    mix,
    relative_gain_db,
    rms_db,
    voice_level_db,
)
from gs06_eval.truth import Segment, TruthParams, merge, voice_truth
from gs06_eval.wav import Audio

RATE = 48_000


def bursts(*parts: tuple[float, float]) -> Audio:
    """（秒数, 幅度）的段接成单声道；幅度为 0 是静音。"""
    pieces: list[NDArray[np.float64]] = []
    for seconds, amplitude in parts:
        t = np.arange(round(seconds * RATE)) / RATE
        pieces.append(amplitude * np.sin(2 * np.pi * 220 * t))
    return Audio(np.concatenate(pieces).astype(np.float32)[:, None], RATE)


def test_segments_follow_the_bursts() -> None:
    audio = bursts((1.0, 0), (1.5, 0.3), (1.0, 0), (0.5, 0.1), (1.0, 0))
    truth = voice_truth(audio)
    assert [(s.start, s.end) for s in truth.segments] == [
        pytest.approx((1.0, 2.5), abs=0.011),
        pytest.approx((3.5, 4.0), abs=0.011),
    ]
    # 阈值：第 95 百分位（约 −13.5 dBFS）减 30 dB。
    assert truth.threshold_db == pytest.approx(20 * np.log10(0.3 / np.sqrt(2)) - 30, abs=0.5)


def test_short_gaps_merge_and_short_segments_are_dropped() -> None:
    audio = bursts((1.0, 0), (0.5, 0.3), (0.2, 0), (0.5, 0.3), (1.0, 0), (0.05, 0.3), (1.0, 0))
    truth = voice_truth(audio)
    assert len(truth.segments) == 1
    assert truth.segments[0].duration == pytest.approx(1.2, abs=0.02)
    kept = voice_truth(audio, TruthParams(merge_gap=0.1, min_segment=0.01))
    assert len(kept.segments) == 3


def test_silence_gives_no_segments() -> None:
    truth = voice_truth(bursts((2.0, 0)))
    assert truth.segments == ()
    assert truth.threshold_db == -60.0
    assert merge([Segment(0, 1), Segment(1.1, 2)], 0.2) == [Segment(0, 2)]


def test_mixing_loops_the_background_and_applies_the_gain() -> None:
    voice = Audio(np.zeros((10, 2), dtype=np.float32), RATE)
    background = Audio(np.array([[1.0], [2.0], [3.0]], dtype=np.float32), RATE)
    mixed = mix(voice, background, -6.0)
    gain = 10 ** (-6 / 20)
    assert mixed.channels == 2
    assert mixed.samples[:, 0].tolist() == pytest.approx([gain * v for v in [1, 2, 3] * 3 + [1]])
    assert fit(background, 2).tolist() == [[1.0], [2.0]]
    with pytest.raises(ValueError, match="采样率"):
        mix(voice, Audio(background.samples, 16_000), 0.0)
    assert rms_db(Audio(np.full((4, 1), 0.5, dtype=np.float32), RATE)) == pytest.approx(
        -6.02, abs=0.01
    )


def test_blocks_are_cut_like_the_audio_ring() -> None:
    audio = Audio(np.zeros((RATE // 10 + 7, 2), dtype=np.float32), RATE)
    items = blocks(audio)
    assert [b.block_number for b in items] == list(range(1, 12))
    assert [b.capture_ns for b in items[:3]] == [0, 10_000_000, 20_000_000]
    assert items[-1].sample_count == 7
    assert not any(b.discontinuity for b in items)


def test_levels_and_relative_gains() -> None:
    # 配音段内幅度 0.3 的正弦（约 −13.5 dBFS），段外静音：配音的电平只算段内。
    voice = bursts((1.0, 0), (1.0, 0.3), (1.0, 0))
    level = voice_level_db(voice, voice_truth(voice).segments)
    assert level == pytest.approx(20 * np.log10(0.3 / np.sqrt(2)), abs=0.1)
    assert rms_db(voice) == pytest.approx(level - 10 * np.log10(3), abs=0.2)
    # 背景先循环到配音的长度再算整段的电平。
    background = Audio(np.full((RATE // 2, 2), 0.1, dtype=np.float32), RATE)
    assert background_level_db(background, voice.length) == pytest.approx(-20.0, abs=0.01)
    gain = relative_gain_db(level, -20.0, -6.0)
    scaled = fitted(background, voice.length, gain)
    assert scaled.length == voice.length
    assert rms_db(scaled) == pytest.approx(level - 6.0, abs=0.01)
    assert rms_db(mix(voice, background, gain)) > rms_db(voice)
    with pytest.raises(ValueError, match="没有配音段"):
        voice_level_db(voice, [])
    with pytest.raises(ValueError, match="静音"):
        background_level_db(Audio(np.zeros((10, 1), dtype=np.float32), RATE), 10)
