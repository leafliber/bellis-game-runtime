"""迟滞与状态：只用合成的概率序列，不要模型。帧长 32 ms，第 i 帧为 [32i, 32(i+1)) ms。"""

import math

import pytest

from game_runtime.voice_activity import (
    Hysteresis,
    UnknownReason,
    VoiceChange,
    VoiceParams,
    VoiceState,
)

FRAME_NS = 32_000_000
SPEECH, SILENCE, MIDDLE = 0.9, 0.1, 0.4


def run(h: Hysteresis, probabilities: list[float], first: int = 0) -> list[tuple[int, VoiceChange]]:
    """依次推入，返回（帧序号, 变化）。"""
    changes: list[tuple[int, VoiceChange]] = []
    for i, p in enumerate(probabilities, start=first):
        change = h.push(p, i * FRAME_NS, (i + 1) * FRAME_NS)
        if change is not None:
            changes.append((i, change))
    return changes


def hysteresis(speech: int = 4, silence: int = 16) -> Hysteresis:
    return Hysteresis(onset=0.5, offset=0.35, speech_frames=speech, silence_frames=silence)


def test_durations_become_whole_frames() -> None:
    params = VoiceParams()
    assert (params.onset, params.offset, params.min_speech, params.min_silence) == (
        0.5,
        0.35,
        0.1,
        0.5,
    )
    assert params.frames(0.1, 512, 16_000) == 4
    assert params.frames(0.5, 512, 16_000) == 16
    assert params.frames(0.064, 512, 16_000) == 2
    assert params.frames(0.001, 512, 16_000) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"onset": 0.3, "offset": 0.4},
        {"offset": 0.0},
        {"onset": 1.1},
        {"min_speech": 0.0},
        {"min_silence": -1.0},
        {"min_silence": math.inf},
    ],
)
def test_invalid_params_are_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="须"):
        VoiceParams(**kwargs)


def test_hysteresis_rejects_invalid_arguments() -> None:
    with pytest.raises(ValueError, match="须"):
        Hysteresis(onset=0.3, offset=0.4, speech_frames=1, silence_frames=1)
    with pytest.raises(ValueError, match="须"):
        Hysteresis(onset=0.5, offset=0.4, speech_frames=0, silence_frames=1)


def test_starts_unknown_and_turns_busy_after_min_speech() -> None:
    h = hysteresis()
    assert (h.state, h.reason, h.since_ns) == (VoiceState.UNKNOWN, UnknownReason.STARTING, None)
    changes = run(h, [SPEECH] * 6)
    # 第 4 帧末尾判为忙，转换时刻是第 1 帧的开头。
    assert changes == [(3, VoiceChange(VoiceState.BUSY, 4 * FRAME_NS, 0))]
    assert (h.state, h.reason, h.since_ns) == (VoiceState.BUSY, None, 0)


def test_unknown_turns_idle_after_min_silence() -> None:
    h = hysteresis()
    changes = run(h, [SILENCE] * 20)
    assert changes == [(15, VoiceChange(VoiceState.IDLE, 16 * FRAME_NS, 0))]


def test_middle_frames_interrupt_both_runs() -> None:
    h = hysteresis()
    # 三帧语音、一帧中间、四帧语音：前三帧不算，忙从第 5 帧算起。
    changes = run(h, [SPEECH] * 3 + [MIDDLE] + [SPEECH] * 4)
    assert changes == [(7, VoiceChange(VoiceState.BUSY, 8 * FRAME_NS, 4 * FRAME_NS))]
    # 忙的时候：中间帧打断静音的累计，状态不变。
    probabilities = [SILENCE] * 15 + [MIDDLE] + [SILENCE] * 15
    assert run(h, probabilities, first=8) == []
    changes = run(h, [SILENCE], first=8 + len(probabilities))
    start = 8 + 16
    assert changes == [
        (8 + 31, VoiceChange(VoiceState.IDLE, (8 + 32) * FRAME_NS, start * FRAME_NS))
    ]


def test_the_band_between_offset_and_onset_keeps_the_state() -> None:
    h = hysteresis()
    run(h, [SPEECH] * 4)
    assert run(h, [MIDDLE] * 100, first=4) == []
    assert h.state is VoiceState.BUSY
    # 闲的时候中间帧也不转忙。
    h = hysteresis()
    run(h, [SILENCE] * 16)
    assert run(h, [MIDDLE] * 100, first=16) == []
    assert h.state is VoiceState.IDLE


def test_nan_counts_as_a_middle_frame() -> None:
    h = hysteresis(speech=2)
    assert run(h, [SPEECH, math.nan, SPEECH]) == []
    assert run(h, [SPEECH], first=3) == [
        (3, VoiceChange(VoiceState.BUSY, 4 * FRAME_NS, 2 * FRAME_NS))
    ]


def test_equal_thresholds_leave_no_band() -> None:
    h = Hysteresis(onset=0.5, offset=0.5, speech_frames=1, silence_frames=1)
    changes = run(h, [0.5, 0.4999, 0.5])
    assert [c.state for _, c in changes] == [VoiceState.BUSY, VoiceState.IDLE, VoiceState.BUSY]


def test_busy_idle_busy_with_timestamps() -> None:
    h = hysteresis(speech=2, silence=3)
    changes = run(h, [SILENCE] * 3 + [SPEECH] * 5 + [SILENCE] * 3 + [SPEECH] * 2)
    assert changes == [
        (2, VoiceChange(VoiceState.IDLE, 3 * FRAME_NS, 0)),
        (4, VoiceChange(VoiceState.BUSY, 5 * FRAME_NS, 3 * FRAME_NS)),
        (10, VoiceChange(VoiceState.IDLE, 11 * FRAME_NS, 8 * FRAME_NS)),
        (12, VoiceChange(VoiceState.BUSY, 13 * FRAME_NS, 11 * FRAME_NS)),
    ]


def test_reset_turns_unknown_and_clears_the_runs() -> None:
    h = hysteresis()
    run(h, [SPEECH] * 4)
    change = h.reset(UnknownReason.GAP, 123)
    assert change == VoiceChange(VoiceState.UNKNOWN, 123, 123, UnknownReason.GAP)
    assert (h.state, h.reason, h.since_ns) == (VoiceState.UNKNOWN, UnknownReason.GAP, 123)
    # 同一原因再来一次不算变化；换了原因算。
    assert h.reset(UnknownReason.GAP, 456) is None
    assert h.since_ns == 123
    assert h.reset(UnknownReason.STALLED, 789) == VoiceChange(
        VoiceState.UNKNOWN, 789, 789, UnknownReason.STALLED
    )
    # 累计已清掉：重置前的三帧语音不算。
    run(h, [SPEECH] * 3, first=10)
    h.reset(UnknownReason.STALLED, 1000)
    assert run(h, [SPEECH] * 3, first=20) == []
    assert run(h, [SPEECH], first=23)[0][1].since_ns == 20 * FRAME_NS
