"""候选模型 Silero VAD 的真实推理。标 `vad_model`：本地数据目录里没有模型（`uv run fetch-models`）
时跳过。语音只用 SAPI 在本机合成的原创台词，不用任何游戏音频；没有 SAPI 时那一项跳过。
"""

from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from game_runtime.audio_ring import AudioBlock
from game_runtime.voice_activity import (
    ModelNotFoundError,
    SileroVad,
    VoiceActivityDetector,
    VoiceChange,
    VoiceState,
)
from gs06_eval.wav import Audio

pytestmark = pytest.mark.vad_model

RATE = 48_000
BLOCK = 480


def frames(samples: NDArray[np.float32]) -> list[NDArray[np.float32]]:
    return [samples[i : i + 512] for i in range(0, len(samples) - 511, 512)]


def detect(mono: NDArray[np.float32], model: SileroVad) -> list[VoiceChange]:
    """48 kHz 单声道复制成立体声，按音频环的样子每块 10 ms 喂给核心。"""
    stereo = np.ascontiguousarray(np.stack([mono, mono], axis=1), dtype=np.float32)
    detector = VoiceActivityDetector(model)
    changes: list[VoiceChange] = []
    for number, start in enumerate(range(0, len(stereo), BLOCK), start=1):
        chunk = stereo[start : start + BLOCK]
        block = AudioBlock(number, start * 10**9 // RATE, len(chunk), False, chunk.tobytes())
        changes.extend(detector.feed(block, sample_rate=RATE, channels=2))
    return changes


def test_a_missing_file_is_reported_with_its_path(tmp_path: Path) -> None:
    with pytest.raises(ModelNotFoundError, match=r"missing\.onnx"):
        SileroVad(tmp_path / "missing.onnx")


def test_the_model_has_the_silero_interface(silero: SileroVad) -> None:
    assert (silero.sample_rate, silero.frame_size) == (16_000, 512)


def test_silence_noise_and_tones_are_not_speech(silero: SileroVad) -> None:
    rng = np.random.default_rng(0)
    t = np.arange(16_000 * 3) / 16_000
    signals = {
        "silence": np.zeros(len(t)),
        "white noise": 0.05 * rng.standard_normal(len(t)),
        "tone": 0.3 * np.sin(2 * np.pi * 440 * t),
    }
    for name, values in signals.items():
        silero.reset()
        probabilities = [silero.probability(f) for f in frames(values.astype(np.float32))]
        assert max(probabilities) < 0.5, name


def test_the_same_frames_after_a_reset_give_the_same_probabilities(silero: SileroVad) -> None:
    values = (0.1 * np.random.default_rng(1).standard_normal(16_000)).astype(np.float32)
    first = [silero.probability(f) for f in frames(values)]
    silero.reset()
    assert [silero.probability(f) for f in frames(values)] == first


def test_synthesized_speech_is_busy_and_turns_idle_afterwards(
    silero: SileroVad, sapi_line: Audio
) -> None:
    assert sapi_line.sample_rate == RATE
    speech = sapi_line.mono().astype(np.float32)
    gap = np.zeros(RATE, dtype=np.float32)
    changes = detect(np.concatenate([gap, speech, gap]), silero)
    states = [c.state for c in changes]
    assert states[0] is VoiceState.IDLE
    assert VoiceState.BUSY in states
    assert states[-1] is VoiceState.IDLE
    first_busy = next(c for c in changes if c.state is VoiceState.BUSY)
    # 语音在 1 秒之后才开始（SAPI 的开头另有一小段静音）。
    assert 1.0 <= first_busy.since_ns / 1e9 < 1.5
    assert changes[-1].since_ns / 1e9 < 1.0 + sapi_line.duration
