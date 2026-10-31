"""核心：块进、变化出。用假模型，不要模型文件：按音量判断的 `EnergyModel`，与按脚本给出概率的
`ScriptedModel`。缺省格式同音频环（48 kHz 立体声，每块 10 ms）。
"""

from dataclasses import replace

import numpy as np
from numpy.typing import NDArray

from game_runtime.audio_ring import AudioBlock
from game_runtime.voice_activity import (
    INITIAL_STATUS,
    UnknownReason,
    VoiceActivityDetector,
    VoiceChange,
    VoiceParams,
    VoiceState,
    VoiceStatus,
)
from game_runtime.voice_activity.fake import EnergyModel, ScriptedModel

RATE = 48_000
BLOCK = 480
MS = 1_000_000
T0 = 10_000 * MS


def signal(*parts: tuple[float, float]) -> NDArray[np.float32]:
    """依次接上（秒数, 幅度）的段：幅度为 0 是静音，否则是 300 Hz 的正弦。立体声。"""
    pieces: list[NDArray[np.float64]] = []
    for seconds, amplitude in parts:
        t = np.arange(round(seconds * RATE)) / RATE
        pieces.append(amplitude * np.sin(2 * np.pi * 300 * t))
    mono = np.concatenate(pieces)
    return np.stack([mono, mono], axis=1).astype(np.float32)


def blocks(samples: NDArray[np.float32], *, first: int = 1, start_ns: int = T0) -> list[AudioBlock]:
    result: list[AudioBlock] = []
    for index, start in enumerate(range(0, len(samples), BLOCK)):
        chunk = np.ascontiguousarray(samples[start : start + BLOCK])
        result.append(
            AudioBlock(
                first + index, start_ns + start * 10**9 // RATE, len(chunk), False, chunk.tobytes()
            )
        )
    return result


def feed(detector: VoiceActivityDetector, items: list[AudioBlock]) -> list[VoiceChange]:
    changes: list[VoiceChange] = []
    for block in items:
        changes.extend(detector.feed(block, sample_rate=RATE, channels=2))
    return changes


def test_silence_speech_silence() -> None:
    model = EnergyModel()
    detector = VoiceActivityDetector(model)
    assert detector.status == INITIAL_STATUS
    changes = feed(detector, blocks(signal((0.64, 0.0), (1.28, 0.3), (1.28, 0.0))))
    assert [c.state for c in changes] == [VoiceState.IDLE, VoiceState.BUSY, VoiceState.IDLE]
    idle, busy, idle_again = changes
    # 16 帧静音后判闲；语音帧连续 4 帧判忙，转换时刻在声音开始的那一帧（640 ms 在第 20 帧开头）。
    assert (idle.since_ns, idle.at_ns) == (T0, T0 + 16 * 32 * MS)
    assert busy.since_ns == T0 + 20 * 32 * MS
    assert busy.at_ns == busy.since_ns + 4 * 32 * MS
    # 声音在 1920 ms 结束；重采样的核跨过边界，跨着边界的那一帧仍算有声。
    assert idle_again.since_ns - (T0 + 1920 * MS) in range(0, 33 * MS)
    assert idle_again.at_ns == idle_again.since_ns + 16 * 32 * MS
    assert model.resets == 1
    status = detector.status
    assert (status.state, status.reason, status.since_ns) == (
        VoiceState.IDLE,
        None,
        idle_again.since_ns,
    )
    assert status.processed_ns is not None
    assert status.processed_ns <= T0 + 3200 * MS


def test_a_discontinuity_starts_a_new_segment() -> None:
    model = EnergyModel()
    detector = VoiceActivityDetector(model)
    items = blocks(signal((1.0, 0.3)))
    feed(detector, items)
    assert detector.status.state is VoiceState.BUSY
    later = blocks(signal((1.0, 0.3)), first=len(items) + 1, start_ns=T0 + 5000 * MS)
    later[0] = replace(later[0], discontinuity=True)
    changes = feed(detector, later)
    assert changes[0] == VoiceChange(
        VoiceState.UNKNOWN, later[0].capture_ns, later[0].capture_ns, UnknownReason.DISCONTINUITY
    )
    # 新的一段从零状态开始，帧的采集时刻从这一块推算。
    assert model.resets == 2
    assert changes[1].state is VoiceState.BUSY
    assert changes[1].since_ns == later[0].capture_ns


def test_a_gap_in_block_numbers_starts_a_new_segment() -> None:
    detector = VoiceActivityDetector(EnergyModel())
    items = blocks(signal((1.0, 0.3)))
    feed(detector, items[:50])
    changes = feed(detector, items[60:])
    assert changes[0] == VoiceChange(
        VoiceState.UNKNOWN, items[60].capture_ns, items[60].capture_ns, UnknownReason.GAP
    )
    # 块号回退（换了音频环）也一样。
    changes = feed(detector, blocks(signal((0.1, 0.3)), first=1))
    assert changes[0].reason is UnknownReason.GAP


def test_a_format_change_starts_a_new_segment() -> None:
    model = EnergyModel()
    detector = VoiceActivityDetector(model)
    items = blocks(signal((0.5, 0.3)))
    feed(detector, items)
    mono = np.zeros(160, dtype=np.float32).tobytes()
    changes = detector.feed(
        AudioBlock(len(items) + 1, T0 + 600 * MS, 160, False, mono), sample_rate=16_000, channels=1
    )
    assert changes == [
        VoiceChange(VoiceState.UNKNOWN, T0 + 600 * MS, T0 + 600 * MS, UnknownReason.DISCONTINUITY)
    ]
    assert model.resets == 2


def test_the_first_block_of_a_segment_may_carry_the_flag() -> None:
    detector = VoiceActivityDetector(EnergyModel())
    items = blocks(signal((0.7, 0.0)))
    items[0] = replace(items[0], discontinuity=True)
    changes = feed(detector, items)
    assert [c.state for c in changes] == [VoiceState.IDLE]


def test_an_interrupt_turns_unknown_and_the_next_block_starts_afresh() -> None:
    model = EnergyModel()
    detector = VoiceActivityDetector(model)
    items = blocks(signal((0.6, 0.0), (0.6, 0.3)))
    feed(detector, items[:70])
    assert detector.status.state is VoiceState.IDLE
    assert detector.interrupt(UnknownReason.STALLED, 42) == [
        VoiceChange(VoiceState.UNKNOWN, 42, 42, UnknownReason.STALLED)
    ]
    assert detector.interrupt(UnknownReason.STALLED, 43) == []
    assert detector.status == VoiceStatus(
        VoiceState.UNKNOWN, UnknownReason.STALLED, 42, detector.status.processed_ns
    )
    # 下一块的块号正好接上，也开始新的一段：模型重置，累计从零开始。
    changes = feed(detector, items[70:])
    assert model.resets == 2
    assert [c.state for c in changes] == [VoiceState.BUSY]


def test_the_same_sequence_gives_the_same_changes() -> None:
    items = blocks(signal((0.3, 0.0), (0.7, 0.3), (0.2, 0.0), (0.4, 0.2), (1.0, 0.0)))
    items[40] = replace(items[40], discontinuity=True)
    observed: list[list[tuple[float, int, int]]] = []
    results: list[list[VoiceChange]] = []
    for _ in range(2):
        frames: list[tuple[float, int, int]] = []
        detector = VoiceActivityDetector(
            EnergyModel(), on_frame=lambda p, s, e, frames=frames: frames.append((p, s, e))
        )
        results.append(feed(detector, items))
        observed.append(frames)
    assert results[0] == results[1]
    assert observed[0] == observed[1]
    assert len(observed[0]) > 50


def test_scripted_probabilities_drive_the_hysteresis() -> None:
    script = [0.9] * 4 + [0.4] * 30 + [0.1] * 2
    detector = VoiceActivityDetector(
        ScriptedModel(script), VoiceParams(min_speech=0.1, min_silence=0.064)
    )
    changes = feed(detector, blocks(signal((1.2, 0.0))))
    assert [c.state for c in changes] == [VoiceState.BUSY, VoiceState.IDLE]
    assert changes[0].at_ns == T0 + 4 * 32 * MS
    assert changes[1].since_ns == T0 + 34 * 32 * MS
