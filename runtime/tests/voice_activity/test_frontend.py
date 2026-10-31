"""预处理：混音、重采样到 16 kHz、分帧与帧的采集时刻。只用合成的正弦与噪声。"""

import numpy as np
import pytest
from numpy.typing import NDArray

from game_runtime.audio_ring import AudioBlock
from game_runtime.voice_activity import Frame, Frontend, Resampler

RATE = 16_000
SIZE = 512
T0 = 5_000_000_000


def blocks(
    samples: NDArray[np.float32], rate: int, size: int, *, start_ns: int = T0
) -> list[AudioBlock]:
    """切成每块 `size` 个采样点的块，采集时刻从 `start_ns` 起按采样率推算。"""
    result: list[AudioBlock] = []
    for number, start in enumerate(range(0, len(samples), size), start=1):
        chunk = np.ascontiguousarray(samples[start : start + size], dtype=np.float32)
        result.append(
            AudioBlock(
                number,
                start_ns + start * 1_000_000_000 // rate,
                len(chunk),
                False,
                chunk.tobytes(),
            )
        )
    return result


def run(samples: NDArray[np.float32], rate: int, size: int) -> list[Frame]:
    channels = samples.shape[1]
    frontend = Frontend(rate, channels, rate=RATE, frame_size=SIZE)
    frames: list[Frame] = []
    for block in blocks(samples, rate, size):
        frames.extend(frontend.push(block))
    return frames


def tone(
    frequency: float, rate: int, seconds: float, amplitude: float = 0.5
) -> NDArray[np.float32]:
    t = np.arange(round(rate * seconds)) / rate
    return (amplitude * np.sin(2 * np.pi * frequency * t)).astype(np.float32)


def joined(frames: list[Frame]) -> NDArray[np.float32]:
    return np.concatenate([f.samples for f in frames])


def peak(signal: NDArray[np.float32]) -> tuple[float, float]:
    """主频（Hz）与它的幅度。"""
    window = np.hanning(len(signal))
    spectrum = np.abs(np.fft.rfft(signal * window)) / (window.sum() / 2)
    index = int(np.argmax(spectrum))
    return index * RATE / len(signal), float(spectrum[index])


@pytest.mark.parametrize("rate", [48_000, 44_100, 22_050, 8_000])
def test_a_tone_keeps_its_frequency_and_amplitude(rate: int) -> None:
    stereo = np.stack([tone(1000, rate, 2.0), tone(1000, rate, 2.0)], axis=1)
    out = joined(run(stereo, rate, rate // 100))
    assert len(out) == pytest.approx(2.0 * RATE, abs=SIZE + 100)
    # 去掉开头的零填充与边缘，看稳态。
    frequency, amplitude = peak(out[RATE // 4 : RATE // 4 + 16_384])
    assert frequency == pytest.approx(1000, abs=2)
    assert amplitude == pytest.approx(0.5, rel=0.02)


@pytest.mark.parametrize(("frequency", "limit"), [(12_000, 1e-3), (9_000, 1e-3), (7_900, 0.1)])
def test_frequencies_above_the_cutoff_are_removed_before_decimation(
    frequency: float, limit: float
) -> None:
    # 不滤波直接 3:1 抽取时 12 kHz 会折叠成 4 kHz。
    stereo = tone(frequency, 48_000, 2.0)[:, None]
    out = joined(run(stereo, 48_000, 480))
    rms = float(np.sqrt(np.mean(out[RATE // 4 : -RATE // 4] ** 2)))
    assert rms < limit * 0.5 / np.sqrt(2)


def test_channels_are_averaged() -> None:
    left = tone(440, 48_000, 1.0)
    out = joined(run(np.stack([left, -left], axis=1), 48_000, 480))
    assert np.max(np.abs(out)) < 1e-6


def test_the_model_rate_needs_no_resampling() -> None:
    samples = np.random.default_rng(1).uniform(-1, 1, (RATE * 2, 1)).astype(np.float32)
    out = joined(run(samples, RATE, 160))
    assert np.array_equal(out, samples[: len(out), 0])
    assert len(out) == (RATE * 2 // SIZE) * SIZE


@pytest.mark.parametrize("rate", [48_000, 44_100, 8_000])
def test_block_sizes_do_not_change_the_samples(rate: int) -> None:
    samples = np.random.default_rng(2).standard_normal((rate * 2, 2)).astype(np.float32) * 0.1
    reference = joined(run(samples, rate, rate // 100))
    for size in (1, 37, 1000, len(samples)):
        assert np.array_equal(joined(run(samples, rate, size)), reference)


def test_frame_times_follow_the_capture_times_of_the_blocks() -> None:
    samples = np.zeros((48_000, 2), dtype=np.float32)
    frames = run(samples, 48_000, 480)
    # 每帧 512 个 16 kHz 采样点即 32 ms；10 ms 的块与 20 ms 的块给出同样的时刻。
    assert [(f.start_ns, f.end_ns) for f in frames] == [
        (T0 + i * 32_000_000, T0 + (i + 1) * 32_000_000) for i in range(len(frames))
    ]
    assert [(f.start_ns, f.end_ns) for f in run(samples, 48_000, 960)] == [
        (f.start_ns, f.end_ns) for f in frames
    ]


def test_a_shift_in_capture_times_moves_the_later_frames() -> None:
    # 第二块的采集时刻比按采样率推算的晚 5 ms：之后的帧按它推算。第一帧在第二块到之前已交出，
    # 末尾按第一块推算。
    frontend = Frontend(16_000, 1, rate=RATE, frame_size=SIZE)
    silence = np.zeros(SIZE, dtype=np.float32).tobytes()
    first = frontend.push(AudioBlock(1, 0, SIZE, False, silence))
    second = frontend.push(AudioBlock(2, 32_000_000 + 5_000_000, SIZE, False, silence))
    assert [(f.start_ns, f.end_ns) for f in first] == [(0, 32_000_000)]
    assert [(f.start_ns, f.end_ns) for f in second] == [(37_000_000, 69_000_000)]


def test_non_finite_values_become_zero_and_bad_blocks_are_rejected() -> None:
    frontend = Frontend(16_000, 1, rate=RATE, frame_size=4)
    values = np.array([np.nan, np.inf, -np.inf, 0.25], dtype=np.float32)
    (frame,) = frontend.push(AudioBlock(1, 0, 4, False, values.tobytes()))
    assert frame.samples.tolist() == [0.0, 0.0, 0.0, 0.25]
    with pytest.raises(ValueError, match="样本值"):
        frontend.push(AudioBlock(2, 0, 4, False, values[:3].tobytes()))


def test_the_resampler_reports_its_ratio_and_half_width() -> None:
    resampler = Resampler(48_000, 16_000)
    assert (resampler.up, resampler.down, resampler.half) == (1, 3, 54)
    resampler = Resampler(44_100, 16_000)
    assert (resampler.up, resampler.down) == (160, 441)
    with pytest.raises(ValueError, match="采样率"):
        Resampler(0, 16_000)
