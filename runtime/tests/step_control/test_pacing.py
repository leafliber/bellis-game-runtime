"""阅读等待使用完整句的采集时刻与配音处理进度。"""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from game_runtime.adapter.schema import DialogueBox
from game_runtime.frame_ring import RingFrame
from game_runtime.narrative import Completeness, NarrativeRecord, NarrativeService
from game_runtime.scene import SceneState, SceneUpdate, Stability, Verdict
from game_runtime.step_control.pacing import DEFAULT_PACING, ReadingPacing, ReadingWaitError
from game_runtime.voice_activity import VoiceHub, VoiceState, VoiceStatus

NS = 1_000_000_000
BOX = DialogueBox(screen="dialogue", text="dialogue")


def scene(capture_ns: int = NS) -> SceneUpdate:
    number = 20
    frame = RingFrame(number, capture_ns, 1, 8, 8, 32, bytes(256))
    return SceneUpdate(
        frame,
        SceneState(
            number,
            frame.capture_ns,
            1,
            8,
            8,
            Stability.STABLE,
            NS,
            0,
            False,
            None,
            None,
            Verdict.SCREEN,
            "dialogue",
            (),
            (),
            None,
        ),
    )


def line() -> NarrativeRecord:
    return NarrativeRecord(
        session=1,
        occurrence=2,
        scope=2,
        screen="dialogue",
        cutscene=None,
        speaker=None,
        text="甲 乙",
        revision=2,
        completeness=Completeness.COMPLETE,
        first_frame=1,
        first_ns=100,
        last_frame=10,
        last_ns=NS,
        revision_frame=9,
        complete_frame=10,
        closed_frame=None,
        low_confidence=(),
    )


def narrative(*records: NarrativeRecord) -> Mock:
    return Mock(
        spec=NarrativeService,
        latest=records[-1],
        processed_frame=20,
        records=Mock(return_value=records),
    )


@pytest.mark.parametrize(
    ("status", "seconds", "reason"),
    [
        (VoiceStatus(VoiceState.IDLE, None, 0, NS - 1), 2.0, "dialogue-voice-unknown"),
        (VoiceStatus(VoiceState.IDLE, None, NS, NS + 500_000_000), 2.0, "dialogue-voice-unknown"),
        (None, 2.0, "dialogue-voice-unknown"),
        (VoiceStatus(VoiceState.BUSY, None, NS, NS), 8.0, "dialogue-voice-busy"),
        (VoiceStatus(VoiceState.BUSY, None, 0, NS - 1), 8.0, "dialogue-voice-busy"),
    ],
)
def test_voice_fallback_is_counted_from_the_complete_frame(
    status: VoiceStatus | None, seconds: float, reason: str
) -> None:
    record = line()
    source = narrative(record)
    voice = None if status is None else VoiceHub(status)
    pacing = ReadingPacing(DEFAULT_PACING, lambda: source, lambda: voice)
    pacing.follow()
    source.latest = replace(record, last_frame=19, last_ns=NS + 650_000_000)
    end = NS + round(seconds * NS)
    latest = scene(end - 100_000_000)
    with pytest.raises(ReadingWaitError, match=reason):
        pacing.require_ready(latest, BOX, end - 1)
    assert pacing.require_ready(latest, BOX, end) == source.latest


def test_new_complete_revision_restarts_reading_and_requires_current_narrative() -> None:
    record = line()
    source = narrative(record)
    voice = VoiceHub(VoiceStatus(VoiceState.IDLE, None, NS, 12 * NS))
    pacing = ReadingPacing(DEFAULT_PACING, lambda: source, lambda: voice)
    ready_at = NS + 540_000_000  # 空格不计入两字的阅读时间
    with pytest.raises(ReadingWaitError, match="dialogue-reading"):
        pacing.require_ready(scene(), BOX, ready_at - 1)
    assert pacing.require_ready(scene(), BOX, ready_at) == record
    changed = replace(
        record, revision=3, revision_frame=15, complete_frame=16, last_frame=16, last_ns=2 * NS
    )
    source.latest = changed
    with pytest.raises(ReadingWaitError, match="dialogue-reading"):
        pacing.require_ready(scene(2 * NS), BOX, ready_at + NS - 1)
    assert pacing.require_ready(scene(2 * NS), BOX, ready_at + NS) == changed
    source.processed_frame = 19
    with pytest.raises(ReadingWaitError, match="dialogue-awaiting-record"):
        pacing.require_ready(scene(), BOX, 8 * NS)
    source.processed_frame = 20
    source.latest = replace(changed, completeness=Completeness.PARTIAL, complete_frame=None)
    with pytest.raises(ReadingWaitError, match="dialogue-awaiting-complete"):
        pacing.require_ready(scene(2 * NS), BOX, 8 * NS)


def test_missing_narrative_holds_even_past_the_voice_wait() -> None:
    pacing = ReadingPacing(DEFAULT_PACING, lambda: None, lambda: None)
    with pytest.raises(ReadingWaitError, match="dialogue-narrative-unavailable"):
        pacing.require_ready(scene(), BOX, 12 * NS)


@pytest.mark.parametrize("same_scope", [False, True])
def test_rereading_uses_a_complete_sentence_from_an_earlier_scope(same_scope: bool) -> None:
    current = line()
    prior = replace(
        current, occurrence=1, scope=current.scope if same_scope else 1, closed_frame=11
    )
    source = narrative(prior, current)
    voice = VoiceHub(VoiceStatus(VoiceState.IDLE, None, NS, NS))
    params = DEFAULT_PACING.model_copy(update={"reread_base": 0.1, "reread_per_char": 0.01})
    pacing = ReadingPacing(params, lambda: source, lambda: voice)
    now = NS + 120_000_000
    if same_scope:
        with pytest.raises(ReadingWaitError, match="dialogue-reading"):
            pacing.require_ready(scene(), BOX, now)
    else:
        assert pacing.require_ready(scene(), BOX, now) == current
